"""Reserve a repair HTTP call in a protected, append-only Git ledger.

The caller must reserve immediately before each outgoing request. A lost push
response fails closed: the slot may be consumed, but no HTTP call is authorized.
This module never initializes a missing ledger or resets an existing budget.
"""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from queue_repair import strict_json


LEDGER_REF = "refs/heads/feature/queue-state"
LEDGER_PATH = "repair-budget.json"
MAX_LEDGER_BYTES = 1024 * 1024
MAX_PUSH_ATTEMPTS = 3
MAX_RESERVATIONS = 2
SHA = re.compile(r"[0-9a-f]{40}")
ENTRY_FIELDS = {"id", "ordinal", "repository", "target", "source", "run_id",
                "run_attempt", "created_at"}


class BudgetError(RuntimeError):
    def __init__(self, reason, detail, metadata=None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.metadata = dict(metadata or {})


def _identity(repository, target, source):
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository)):
        raise ValueError("Invalid repository")
    if (not isinstance(target, str) or not target.startswith("feature/") or len(target) > 240
            or any(c in target for c in ("..", "@{", "\\", "~", "^", ":", "?", "*", "["))
            or any(ord(c) <= 32 or ord(c) == 127 for c in target)
            or any(not part or part.startswith(".") or part.endswith((".", ".lock"))
                   for part in target.split("/"))):
        raise ValueError("Invalid target")
    if not isinstance(source, str) or not SHA.fullmatch(source):
        raise ValueError("Invalid immutable source")
    return repository.lower(), target, source


def reservation_key(repository, target, source):
    identity = _identity(repository, target, source)
    return hashlib.sha256(json.dumps(identity, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _ledger(raw):
    """Validate every entry, including unrelated budgets, before preserving it."""
    try:
        data = strict_json(raw)
        if (not isinstance(data, dict) or set(data) != {"version", "reservations"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["reservations"], dict) or len(data["reservations"]) > 10000):
            raise ValueError()
        seen = set()
        for key, entries in data["reservations"].items():
            if (not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(entries, list)
                    or not 1 <= len(entries) <= MAX_RESERVATIONS):
                raise ValueError()
            for ordinal, entry in enumerate(entries, 1):
                if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
                    raise ValueError()
                identity = _identity(entry["repository"], entry["target"], entry["source"])
                if entry["repository"] != identity[0] or reservation_key(*identity) != key:
                    raise ValueError()
                identifier = entry["id"]
                if (not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier
                        or identifier in seen or type(entry["ordinal"]) is not int
                        or entry["ordinal"] != ordinal or not isinstance(entry["run_id"], str)
                        or not re.fullmatch(r"[0-9]{1,30}", entry["run_id"])
                        or type(entry["run_attempt"]) is not int or not 1 <= entry["run_attempt"] <= 1000000
                        or not isinstance(entry["created_at"], str)
                        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", entry["created_at"])):
                    raise ValueError()
                seen.add(identifier)
        return data
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise BudgetError("budget_corrupt", "Repair ledger has an invalid schema or identity") from None


def _extends(previous, current):
    for key, entries in previous["reservations"].items():
        if current["reservations"].get(key, [])[:len(entries)] != entries:
            raise BudgetError("budget_rewritten", "Previously observed reservations were removed or changed")


class _Reservation:
    def __init__(self, git, repository, target, source, run_id, run_attempt, remote):
        try:
            repository, target, source = _identity(repository, target, source)
            if (not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{1,30}", run_id)
                    or type(run_attempt) is not int or not 1 <= run_attempt <= 1000000
                    or not isinstance(remote, str)
                    or remote != "origin" and remote.lower() != f"git@github.com:{repository}.git"):
                raise ValueError()
        except (TypeError, ValueError):
            raise BudgetError("budget_invalid_input", "A repository, feature target, immutable SHA and run identity are required") from None
        self.git = git
        self.remote = remote
        self.key = reservation_key(repository, target, source)
        self.identifier = str(uuid.uuid4())
        self.metadata = {"key": self.key, "reservation_id": self.identifier, "push_attempts": 0}
        self.entry = {"id": self.identifier, "repository": repository, "target": target,
                      "source": source, "run_id": run_id, "run_attempt": run_attempt,
                      "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.refs = []

    def call(self, *args, cwd=None, push=False):
        try:
            return self.git(*args, cwd=cwd, check=False)
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            reason = "budget_ambiguous" if push else "budget_unavailable"
            detail = ("Reservation publication was not confirmed; its slot may already be consumed"
                      if push else "The repair ledger could not be read or prepared")
            raise BudgetError(reason, detail, self.metadata) from None

    def checked(self, *args, cwd=None):
        result = self.call(*args, cwd=cwd)
        if result.returncode:
            raise BudgetError("budget_unavailable", "A repair-ledger Git operation failed", self.metadata)
        return result.stdout.strip()

    def snapshot(self):
        # Each fetch writes a new local ref: a failed fetch cannot expose stale state.
        ref = "refs/council-budget/" + uuid.uuid4().hex
        self.refs.append(ref)
        result = self.call("fetch", "--quiet", "--no-tags", self.remote, LEDGER_REF + ":" + ref)
        if result.returncode:
            raise BudgetError("budget_unavailable", "The preinitialized repair ledger is unavailable", self.metadata)
        tip = self.checked("rev-parse", "--verify", ref + "^{commit}")
        if not SHA.fullmatch(tip):
            raise BudgetError("budget_corrupt", "Invalid repair-ledger commit identity", self.metadata)
        tree_entry = self.checked("ls-tree", tip, "--", LEDGER_PATH)
        match = re.fullmatch(r"100644 blob ([0-9a-f]{40})\trepair-budget\.json", tree_entry)
        if not match:
            raise BudgetError("budget_corrupt", "Repair ledger must be a regular tracked JSON file", self.metadata)
        blob = match.group(1)
        size = self.checked("cat-file", "-s", blob)
        if not size.isdecimal() or not 0 < int(size) <= MAX_LEDGER_BYTES:
            raise BudgetError("budget_oversize", "Repair ledger is empty or exceeds its size limit", self.metadata)
        data = _ledger(self.checked("cat-file", "blob", blob))
        return tip, data

    def commit(self, tip, ledger):
        raw = (json.dumps(ledger, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        if len(raw) > MAX_LEDGER_BYTES:
            raise BudgetError("budget_oversize", "Appending a reservation would exceed the ledger size limit", self.metadata)
        # Only this private worktree and its index are changed. The normal commit
        # retains the full parent tree; unrelated ledger entries are preserved.
        root = Path(tempfile.mkdtemp(prefix="council-budget-"))
        checkout = root / "checkout"
        added = False
        clean = False
        try:
            self.checked("worktree", "add", "--detach", "--quiet", str(checkout), tip)
            added = True
            path = checkout / LEDGER_PATH
            path.write_bytes(raw)
            blob = self.checked("hash-object", "--no-filters", "-w", str(path), cwd=checkout)
            if not SHA.fullmatch(blob):
                raise BudgetError("budget_corrupt", "Invalid reservation blob identity", self.metadata)
            self.checked("update-index", "--cacheinfo", "100644," + blob + "," + LEDGER_PATH, cwd=checkout)
            tree = self.checked("write-tree", cwd=checkout)
            candidate = self.checked("commit-tree", tree, "-p", tip,
                                     "-m", "chore: зарезервирована попытка ремонта", cwd=checkout)
            if not SHA.fullmatch(candidate):
                raise BudgetError("budget_corrupt", "Invalid reservation commit identity", self.metadata)
            # Advance only our detached HEAD, making the private worktree clean.
            self.checked("update-ref", "HEAD", candidate, tip, cwd=checkout)
            clean = True
            return candidate
        finally:
            if added:
                if not clean:
                    # This file contains only our unpublished reservation.
                    self.call("restore", "--source=HEAD", "--staged", "--worktree", "--", LEDGER_PATH, cwd=checkout)
                self.checked("worktree", "remove", str(checkout))
            if root.exists() and not any(root.iterdir()):
                root.rmdir()

    def readback(self, candidate, previous):
        tip, ledger = self.snapshot()
        _extends(previous, ledger)
        rows = ledger["reservations"].get(self.key, [])
        own = next((row for row in rows if row["id"] == self.identifier), None)
        if own is not None:
            if own != self.entry:
                raise BudgetError("budget_corrupt", "Published reservation does not match this request", self.metadata)
            if self.call("merge-base", "--is-ancestor", candidate, tip).returncode:
                raise BudgetError("budget_unconfirmed", "Observed ledger does not retain the reservation commit", self.metadata)
            return {**self.metadata, "ordinal": own["ordinal"], "ledger_commit": tip,
                    "reservation_commit": candidate}
        return tip, ledger

    def reserve(self):
        tip, ledger = self.snapshot()
        for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
            rows = ledger["reservations"].get(self.key, [])
            if len(rows) >= MAX_RESERVATIONS:
                raise BudgetError("budget_exhausted", "This immutable source already consumed both repair reservations",
                                  {**self.metadata, "ledger_commit": tip, "consumed": len(rows)})
            self.entry["ordinal"] = len(rows) + 1
            proposed = {"version": 1, "reservations": {**ledger["reservations"], self.key: rows + [dict(self.entry)]}}
            candidate = self.commit(tip, proposed)
            self.metadata["reservation_commit"] = candidate
            self.metadata["push_attempts"] = attempt
            result = self.call("push", "--porcelain", self.remote, candidate + ":" + LEDGER_REF, push=True)
            observed = self.readback(candidate, ledger)
            if isinstance(observed, dict):
                return observed
            if result.returncode == 0:
                raise BudgetError("budget_unconfirmed", "Push reported success but the reservation was not found", self.metadata)
            tip, ledger = observed
        raise BudgetError("budget_contention", "Reservation could not be confirmed after three ordinary push attempts", self.metadata)


def reserve(git, repository, target, source, run_id, run_attempt, remote="origin"):
    """Return only after an ordinary push and a fresh read confirm this slot.

    ``git`` has Gate.git's signature: (*args, cwd=None, check=True), returning
    subprocess.CompletedProcess with UTF-8 text. The ledger branch must already
    exist and be protected from reset/deletion by anyone except its publisher.
    """
    reservation = _Reservation(git, repository, target, source, run_id, run_attempt, remote)
    try:
        return reservation.reserve()
    except BudgetError as error:
        error.metadata = {**reservation.metadata, **error.metadata}
        raise
    finally:
        for ref in reservation.refs:
            # Remove only fresh refs belonging to this invocation. Cleanup cannot
            # turn an unconfirmed reservation into permission to call the model.
            try:
                reservation.git("update-ref", "-d", ref, check=False)
            except (subprocess.TimeoutExpired, TimeoutError, OSError):
                pass
