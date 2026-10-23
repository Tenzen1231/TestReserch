"""Final integration agent for a shared branch, platform-neutral core.

For one exact PR version it merges the current shared branch INTO the PR branch
(Mergiraf is the git merge driver), lets a model resolve what remains, requires
every test of both sides to exist unchanged and pass, pushes the verified merge
to the PR branch with an ordinary push and fast-forwards the shared branch to it.
Anything it cannot prove goes to a human with the failing tests of each side.

Usage:
  python fixbot.py fix   --repo DIR --state-dir DIR --pr N --expected-head SHA
  python fixbot.py sweep --state-dir DIR
"""
import argparse
import ast
from datetime import datetime, timezone
import difflib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import uuid

from queue_budget import BudgetError, reserve as reserve_budget
from queue_reconcile import decide, paged
from queue_repair import RepairError, RouterRepairClient
from queue_status import CommitStatuses, StatusError


TARGET = os.environ.get("FIXBOT_TARGET", "feature/lab-main")
PRODUCT_PREFIXES = ("shop/",)
TEST_PREFIX = "tests/"
FREE_SUFFIXES = (".md",)  # Documentation may change freely and is never given to the model.
MAX_REPAIRS = 2
MAX_PUBLISH_ATTEMPTS = 3
IMAGE = "python@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2"
RUNNER = Path(__file__).with_name("fixbot_runner.py")
SYSTEM_PROMPT = """You are the final integration agent of a shared repository.
A pull request branch was merged with the current shared branch. Resolve what remains so that BOTH
sides keep working: the shared branch already contains other developers' accepted features, the pull
request adds its own feature. Each side's intent is its delta from common_ancestor; the PR description and
target_history describe them. Keep every behaviour of both sides; never delete or weaken another side's
logic to make tests pass. Apply each side's change literally: keep each side's formulas, the base each
computation uses, rounding, messages and output lines exactly as that side wrote them (two discounts written
against the same subtotal both stay computed from that subtotal). Keep every existing positional parameter at
its position; when two sides add a parameter at the same position, make the newly added ones keyword-only.
In "explanation" list every behaviour decision that neither side's code nor tests dictate.
You may only return files listed in allowed_paths. Test files appear in
allowed_paths only when they are conflicted; then you must keep every test function of both sides exactly
as written and only remove conflict markers. Return one JSON object with exactly two keys: "files" (allowed
relative path -> complete new UTF-8 file text, changed files only) and "explanation" (short, Russian).
If both sides' tests demand contradictory behaviour, return an empty "files" object and explain the
contradiction. Return only JSON, without Markdown fences."""


def now():
    return datetime.now(timezone.utc).isoformat()


class Stop(RuntimeError):
    """A terminal outcome for this PR version."""

    def __init__(self, decision, reason, detail="", needs_human=False):
        super().__init__(detail or reason)
        self.decision, self.reason, self.detail, self.needs_human = decision, reason, detail, needs_human


def is_test(path):
    return path.startswith(TEST_PREFIX) and path.endswith(".py")


def is_test_module(path):
    return is_test(path) and PurePosixPath(path).name.startswith("test")


def is_product(path):
    return path.startswith(PRODUCT_PREFIXES) and path.endswith(".py")


def merge3(base, left, right):
    """Character-level three-way merge of independent edits; None when edits touch or overlap.

    Two developers often change the same expected text in different places (one adds a
    line, the other renames an item). Each side's edits are kept verbatim; anything
    adjacent or overlapping is left to a human.
    """
    def edits(new):
        matcher = difflib.SequenceMatcher(None, base, new, autojunk=False)
        return [(i1, i2, new[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]
    merged = []
    for start, end, text in sorted(set(edits(left)) | set(edits(right))):
        if merged and start <= merged[-1][1]:
            return None
        merged.append((start, end, text))
    result, position = [], 0
    for start, end, text in merged:
        result += [base[position:start], text]
        position = end
    return "".join(result) + base[position:]


def test_functions(text, sources=False):
    """Qualified test functions of one file -> normalized AST (or padded source)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    found = {}

    def visit(nodes, prefix):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found[prefix + node.name] = (ast.get_source_segment(text, node, padded=True) if sources
                                             else ast.dump(node, include_attributes=False))
    visit(tree.body, "")
    return found


def function_dump(source):
    try:
        return ast.dump(ast.parse(textwrap.dedent(source)).body[0], include_attributes=False)
    except (SyntaxError, IndexError):
        return None


def preserved_tests(base, target, source, candidate):
    """Every test of both sides must survive: changed by one side -> that version; by both -> conflict."""
    problems = []
    sides = {name: test_functions(text) if text is not None else {} for name, text in
             (("base", base), ("target", target), ("source", source), ("candidate", candidate))}
    if any(value is None for value in sides.values()):
        return ["test file does not parse"]
    texts = {name: test_functions(text, sources=True) if text is not None else {} for name, text in
             (("base", base), ("target", target), ("source", source))}
    for name in sorted(set(sides["target"]) | set(sides["source"])):
        old, left, right = sides["base"].get(name), sides["target"].get(name), sides["source"].get(name)
        if left is not None and right is not None and left != right and old not in (left, right):
            # Both changed it: only independent edits may combine, each kept verbatim.
            combined = None if old is None else merge3(texts["base"][name], texts["target"][name], texts["source"][name])
            if combined is None or function_dump(combined) != sides["candidate"].get(name):
                problems.append(name + ": both sides changed this test and the edits cannot be combined verbatim")
            continue
        if left is None or right is None:
            # Deleting another side's test is a decision for a human, never for the bot.
            expected = right if left is None else left
        else:
            expected = right if left == old else left
        if sides["candidate"].get(name) != expected:
            problems.append(name + ": missing or altered in the merged result")
    return problems


def meaningful(text):
    return {line.strip() for line in (text or "").splitlines()
            if line.strip() and not line.strip().startswith("#")}


def altered_lines(base, side, candidate):
    """Lines a side added that do not survive verbatim: decisions a human should see."""
    return sorted(meaningful(side) - meaningful(base) - meaningful(candidate))


def positional(text):
    """Function name -> positional parameter names (module level and class methods)."""
    try:
        tree = ast.parse(text or "")
    except SyntaxError:
        return {}
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            found[node.name] = [arg.arg for arg in node.args.posonlyargs + node.args.args]
    return found


def moved_parameters(side, candidate):
    """Positional parameters of a side that moved or vanished: old positional calls would break."""
    merged = positional(candidate)
    problems = []
    for name, params in positional(side).items():
        if name not in merged:
            problems.append(name + ": функция исчезла")
            continue
        for index, param in enumerate(params):
            if index >= len(merged[name]) or merged[name][index] != param:
                problems.append(name + ": параметр " + param + " больше не на позиции " + str(index + 1))
    return problems


class Bot:
    def __init__(self, repo, state_dir, api=None, client_factory=None):
        self.repo = Path(repo).resolve()
        self.directory = Path(state_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.api = api if api is not None else CommitStatuses.from_environment()
        self.client_factory = client_factory or (lambda paths, profile: RouterRepairClient.from_environment(
            allowed_paths=paths, profile=profile))
        self.repository = os.environ.get("GITHUB_REPOSITORY", "")
        self.result = {"version": 1, "target": TARGET, "started_at": now(), "run_id": os.environ.get("GITHUB_RUN_ID"),
                       "repairs": [], "status_errors": [], "steps": []}

    # -- plumbing -----------------------------------------------------------------------------------
    def save(self):
        path = self.directory / "result.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def step(self, name, **data):
        self.result["steps"].append({"step": name, "at": now(), **data})
        self.save()
        print(json.dumps({"step": name, **data}, ensure_ascii=False)[:2000], flush=True)

    def git(self, *args, cwd=None, check=True, secrets=True):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if not secrets:
            # Merge drivers parse untrusted files: they get no credentials.
            for key in ("GITHUB_TOKEN", "ROUTERAI_API_KEY", "SSH_AUTH_SOCK", "QUEUE_DEPLOY_KEY"):
                env.pop(key, None)
        result = subprocess.run(["git", "-c", "user.name=Integration bot", "-c", "user.email=integration-bot@localhost",
                                 "-c", "commit.gpgsign=false", *args], cwd=cwd or self.repo, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if check and result.returncode:
            raise Stop("error", "git_error", (result.stderr or result.stdout)[-2000:])
        return result

    def text(self, *args, **kwargs):
        return self.git(*args, **kwargs).stdout.strip()

    def show(self, commit, path):
        result = self.git("show", commit + ":" + path, check=False)
        return result.stdout if result.returncode == 0 else None

    def ancestor(self, older, newer):
        return self.git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def changed(self, older, newer):
        return [p for p in self.git("diff", "--name-only", "--no-renames", "-z", older, newer).stdout.split("\0") if p]

    def status(self, sha, state, description):
        if self.api is None:
            return
        try:
            self.api.send(sha, TARGET, state, description)
        except StatusError as error:
            if state != "pending":
                raise
            self.result["status_errors"].append(str(error))

    def comment(self, number, body):
        if self.api is None:
            return
        try:
            self.api.request("POST", "/repos/" + self.api.repository + "/issues/" + str(number) + "/comments",
                             {"body": body[:60000]})
        except StatusError as error:
            self.result["status_errors"].append("comment: " + str(error))

    def remote(self):
        if os.environ.get("FIXBOT_SSH_PUBLISH") == "true":
            return "git@github.com:" + self.repository + ".git"
        return "origin"

    def configure_mergiraf(self):
        binary = os.environ.get("MERGIRAF_BINARY")
        if not binary:
            self.result["mergiraf"] = "not_configured"
            return
        # The trusted bot decides the merge driver, not the merged branches' own .gitattributes.
        self.git("config", "merge.mergiraf.name", "mergiraf")
        self.git("config", "merge.mergiraf.driver",
                 Path(binary).as_posix() + " merge --git %O %A %B -s %S -x %X -y %Y -p %P -l %L")
        info = Path(self.text("rev-parse", "--git-common-dir"))
        info = (info if info.is_absolute() else self.repo / info) / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text("*.py merge=mergiraf\n", encoding="utf-8")
        self.result["mergiraf"] = binary

    # -- tests --------------------------------------------------------------------------------------
    def export(self, commit):
        directory = self.directory / ("tree-" + uuid.uuid4().hex[:10])
        directory.mkdir()
        archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=self.repo,
                                 capture_output=True, check=True, timeout=60).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
            for member in stream.getmembers():
                parts = PurePosixPath(member.name).parts
                if member.name.startswith("/") or any(part in ("..", ".git") for part in parts):
                    raise Stop("error", "unsafe_archive", member.name)
                path = directory.joinpath(*parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(stream.extractfile(member).read())
                else:
                    raise Stop("rejected", "unsupported_file_type", member.name, needs_human=True)
        return directory

    def tests(self, commit, mode):
        tree = self.export(commit)
        try:
            if os.environ.get("FIXBOT_SANDBOX") == "local":
                command = [sys.executable, "-I", "-B", str(RUNNER), str(tree), mode]
                env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ}
                env["PYTHONUTF8"] = "1"
            else:
                command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                           "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                           "--pids-limit=128", "--memory=512m", "--cpus=1",
                           "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
                           "--mount", "type=bind,src=" + str(tree) + ",dst=/project,readonly",
                           "--mount", "type=bind,src=" + str(RUNNER) + ",dst=/runner/fixbot_runner.py,readonly",
                           IMAGE, "python", "-I", "-B", "/runner/fixbot_runner.py", "/project", mode]
                env = None
            try:
                process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                         errors="replace", timeout=600, env=env)
            except subprocess.TimeoutExpired:
                return {"ids": [], "passed": False, "loader_errors": ["test run timed out"], "outcomes": {}}
            try:
                return json.loads(process.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return {"ids": [], "passed": False, "outcomes": {},
                        "loader_errors": ["runner produced no report: " + process.stderr[-2000:]]}
        finally:
            shutil.rmtree(tree, ignore_errors=True)

    # -- main flow ----------------------------------------------------------------------------------
    def guard(self, number, expected):
        if self.api is None:
            return {"number": number, "head": {"sha": expected, "ref": os.environ["FIXBOT_HEAD_REF"]},
                    "title": "", "body": ""}
        pr = self.api.request("GET", "/repos/" + self.api.repository + "/pulls/" + str(number))
        if pr.get("state") != "open" or pr.get("draft"):
            raise Stop("skipped", "pr_not_open")
        if (pr["base"]["ref"] != TARGET or pr["head"]["repo"]["full_name"] != self.api.repository
                or pr["base"]["repo"]["full_name"] != self.api.repository):
            raise Stop("skipped", "not_a_team_pr")
        if pr["head"]["sha"] != expected:
            raise Stop("skipped", "superseded", "A newer push is processed by its own run")
        if not re.fullmatch(r"feature/[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", pr["head"]["ref"]):
            raise Stop("rejected", "invalid_branch", pr["head"]["ref"], needs_human=True)
        return pr

    def review(self, target, head, candidate):
        """What the merge changed in each author's own lines and APIs, for the PR comment."""
        base = self.result.get("base") or self.text("merge-base", target, head)
        report = {"altered": {}, "moved_parameters": {}, "required": False}
        sensitive = [prefix for prefix in os.environ.get("FIXBOT_REVIEW_PATHS", "").split(",") if prefix]
        if candidate == head:  # Nothing was merged or rewritten by the agent.
            self.result["review"] = report
            return report
        for side_name, side in (("shared", target), ("pull_request", head)):
            for path in sorted(p for p in set(self.changed(base, side)) if is_product(p)):
                lost = altered_lines(self.show(base, path), self.show(side, path), self.show(candidate, path))
                moved = moved_parameters(self.show(side, path), self.show(candidate, path))
                if lost:
                    report["altered"].setdefault(side_name, {})[path] = lost[:20]
                if moved:
                    report["moved_parameters"].setdefault(side_name, {})[path] = moved
                if (lost or moved) and any(path.startswith(prefix) for prefix in sensitive):
                    report["required"] = True
        self.result["review"] = report
        self.save()
        return report

    def fetch(self, head_ref):
        prefix = "refs/fixbot/" + uuid.uuid4().hex
        self.git("fetch", "--quiet", "--no-tags", "origin", "refs/heads/" + TARGET + ":" + prefix + "/target",
                 "refs/heads/" + head_ref + ":" + prefix + "/head")
        return self.text("rev-parse", prefix + "/target"), self.text("rev-parse", prefix + "/head")

    def fix(self, number, expected):
        self.result.update(pr=number, expected_head=expected)
        pr = self.guard(number, expected)
        head_ref = pr["head"]["ref"]
        self.result["head_ref"] = head_ref
        self.status(expected, "pending", "Итоговый агент проверяет совместимость с " + TARGET)
        self.configure_mergiraf()
        for publish_attempt in range(1, MAX_PUBLISH_ATTEMPTS + 1):
            target, head = self.fetch(head_ref)
            if head != expected:
                raise Stop("skipped", "superseded", "The PR branch moved during the run")
            self.result.update(target=target)
            candidate = self.prepare(pr, target, head)
            if candidate != head:
                push = self.git("push", "--porcelain", self.remote(), candidate + ":refs/heads/" + head_ref, check=False)
                self.step("push_pr_branch", candidate=candidate, returncode=push.returncode)
                if push.returncode:
                    raise Stop("skipped", "superseded", "The author pushed during the run; the new push is checked")
            review = self.review(target, head, candidate)
            approved = "fixbot-approved" in {label.get("name") for label in pr.get("labels", [])}
            if review["required"] and not approved:
                # Tests passed, but the model rewrote lines of a sensitive path: a human
                # confirms its decisions (label fixbot-approved) before anything is merged.
                self.status(candidate, "pending", "Проверено; решения модели ждут подтверждения человеком")
                self.comment(number, self.summary("Нужно подтверждение решений модели (метка fixbot-approved)", candidate))
                raise Stop("verified", "needs_confirmation")
            if os.environ.get("FIXBOT_AUTO_MERGE", "true") != "true":
                self.status(candidate, "success", "Проверено против " + target[:8] + "; можно вливать")
                raise Stop("verified", "ready_to_merge")
            publish = self.git("push", "--porcelain", self.remote(), candidate + ":refs/heads/" + TARGET, check=False)
            self.step("publish_target", candidate=candidate, attempt=publish_attempt, returncode=publish.returncode)
            if publish.returncode == 0:
                self.status(candidate, "success", "Влито в " + TARGET + ": " + candidate[:8])
                if candidate != expected:
                    self.status(expected, "success", "Исправлено ботом и влито: " + candidate[:8])
                self.result.update(decision="merged", reason="verified_and_published", published=candidate)
                self.comment(number, self.summary("Влито автоматически", candidate))
                self.save()
                self.requeue_others(number)
                return
            expected = candidate  # The shared branch moved: re-verify our own pushed version.
        raise Stop("error", "publish_retry_limit", "The shared branch kept moving", needs_human=False)

    def prepare(self, pr, target, head):
        base = self.text("merge-base", target, head)
        self.result.update(base=base, head=head)
        own = self.changed(base, head)
        outside = [p for p in own if not (is_product(p) or is_test(p) or p.endswith(FREE_SUFFIXES))]
        if outside:
            raise Stop("rejected", "protected_paths", ", ".join(outside), needs_human=True)
        # Shared test infrastructure (tests/__init__.py, helpers) runs inside everyone's tests.
        infrastructure = [p for p in own if is_test(p) and not is_test_module(p) and self.show(base, p) is not None]
        if infrastructure:
            raise Stop("rejected", "test_infrastructure_changed", ", ".join(infrastructure), needs_human=True)
        target_tests, head_tests = self.tests(target, "list"), self.tests(head, "list")
        required = sorted(set(target_tests["ids"]) | set(head_tests["ids"]))
        self.result["required_tests"] = len(required)
        if self.ancestor(target, head):
            problems = self.check(self.tests(head, "run"), required, base, target, head, head)
            if problems:
                raise Stop("rejected", "pr_tests_fail", json.dumps(problems, ensure_ascii=False)[:4000],
                           needs_human=True)
            self.step("already_up_to_date", tests=len(required))
            return head
        worktree = self.directory / ("work-" + uuid.uuid4().hex[:8])
        self.git("worktree", "add", "--quiet", "--detach", str(worktree), head)
        merge = self.git("-c", "merge.conflictStyle=zdiff3", "merge", "--no-ff", "--no-commit",
                         "-m", "merge: совмещено с " + TARGET + " итоговым агентом", target,
                         cwd=worktree, check=False, secrets=False)
        conflicts = sorted(self.text("diff", "--name-only", "--diff-filter=U", cwd=worktree).splitlines())
        for path in [p for p in conflicts if is_test(p)]:
            # Tests are never written by the model: independent edits of both sides combine verbatim.
            combined = merge3(self.show(base, path) or "", self.show(target, path) or "", self.show(head, path) or "")
            if combined is not None and test_functions(combined) is not None:
                (worktree / path).write_text(combined, encoding="utf-8", newline="")
                self.git("add", "--", path, cwd=worktree)
                conflicts.remove(path)
                self.result.setdefault("tests_combined", []).append(path)
        self.step("merge", returncode=merge.returncode, conflicts=conflicts,
                  mergiraf=self.result.get("mergiraf") != "not_configured")
        bad = [p for p in conflicts if not (is_product(p) or is_test(p))]
        if bad:
            raise Stop("rejected", "conflict_outside_product", ", ".join(bad), needs_human=True)
        failure = {"conflicts": conflicts}
        for attempt in range(0, MAX_REPAIRS + 1):
            if not failure.get("conflicts"):
                candidate = self.commit(worktree, amend=attempt > 0 and self.merge_committed)
                report = self.tests(candidate, "run")
                problems = self.check(report, required, base, target, head, candidate)
                if not problems:
                    self.step("verified", candidate=candidate, tests=len(report["ids"]), attempt=attempt)
                    return candidate
                failure = {"conflicts": [], "tests": problems}
                self.result.setdefault("attempt_failures", []).append(sorted(problems["failed"]) + problems["missing"])
            if attempt == MAX_REPAIRS:
                break
            self.repair(pr, worktree, base, target, head, failure, attempt + 1)
            failure = {"conflicts": sorted(p for p in self.markers(worktree, failure.get("conflicts", [])))}
        raise self.escalate(failure, base, target, head)

    merge_committed = False

    def commit(self, worktree, amend):
        self.git("add", "-A", cwd=worktree)
        if amend:
            self.git("commit", "--amend", "--no-edit", cwd=worktree)
        else:
            self.git("commit", "--no-edit", cwd=worktree)
            self.merge_committed = True
        return self.text("rev-parse", "HEAD", cwd=worktree)

    def markers(self, worktree, paths):
        for path in paths:
            text = (worktree / path).read_text(encoding="utf-8", errors="replace")
            if re.search(r"^(<{7}|={7}|>{7})( |$)", text, re.M):
                yield path

    def check(self, report, required, base, target, head, candidate):
        problems = {"failed": {}, "missing": [], "loader_errors": report.get("loader_errors", []),
                    "altered_tests": []}
        problems["missing"] = sorted(set(required) - set(report.get("ids", [])))
        for test_id, outcome in sorted((report.get("outcomes") or {}).items()):
            if not outcome.get("passed"):
                problems["failed"][test_id] = outcome.get("detail", "")[-1500:]
        for path in sorted({p for p in self.changed(base, target) + self.changed(base, head) if is_test(p)}):
            altered = preserved_tests(self.show(base, path), self.show(target, path), self.show(head, path),
                                      self.show(candidate, path))
            problems["altered_tests"].extend(path + "::" + item for item in altered)
        clean = not any(problems.values())
        return None if clean else problems

    def repair(self, pr, worktree, base, target, head, failure, ordinal):
        conflicts = failure.get("conflicts", [])
        product = sorted({p for p in self.changed(base, target) + self.changed(base, head) + conflicts if is_product(p)})
        allowed = sorted(set(product) | {p for p in conflicts if is_test(p)})
        if not allowed:
            raise self.escalate(failure, base, target, head)
        relevant = sorted(set(allowed) | {p for p in self.changed(base, target) + self.changed(base, head) if is_test(p)})
        context = {
            "allowed_paths": allowed,
            "pull_request": {"title": pr.get("title", ""), "description": (pr.get("body") or "")[:4000]},
            "target_history": self.text("log", "--format=%s", base + ".." + target)[-4000:],
            "pull_request_commits": self.text("log", "--format=%s", base + ".." + head)[-2000:],
            "failure": failure,
            "common_ancestor": {p: self.show(base, p) for p in relevant if self.show(base, p) is not None},
            "shared_branch": {p: self.show(target, p) for p in relevant if self.show(target, p) is not None},
            "pull_request_branch": {p: self.show(head, p) for p in relevant if self.show(head, p) is not None},
            "current_merge": {p: (worktree / p).read_text(encoding="utf-8") for p in allowed if (worktree / p).is_file()},
            "previous_attempts": [{"reason": r.get("reason"), "explanation": r.get("explanation")}
                                  for r in self.result["repairs"]],
        }
        try:
            client = self.client_factory(allowed, ordinal)
            client.system_prompt = SYSTEM_PROMPT
            client.payload(context)  # Size and credential checks before spending the budget.
        except RepairError as error:
            raise Stop("error", "repair_unavailable", error.reason) from None
        record = {"ordinal": ordinal, "failure_kind": "conflict" if conflicts else "tests", "at": now()}
        if os.environ.get("GITHUB_ACTIONS") == "true":
            try:
                record["reservation"] = reserve_budget(self.git, self.repository, TARGET, head,
                                                       os.environ.get("GITHUB_RUN_ID", "0"),
                                                       int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")), remote=self.remote())
            except BudgetError as error:
                raise Stop("rejected", "repair_budget_exhausted" if error.reason == "budget_exhausted"
                           else "budget_unavailable", error.detail, needs_human=error.reason == "budget_exhausted") from None
        self.result["repairs"].append(record)
        self.save()
        try:
            proposal = client.propose(context)
        except RepairError as error:
            record.update(state="failed", reason=error.reason, metadata=error.metadata)
            self.save()
            if error.reason == "repair_no_changes":
                # The model reports that both sides cannot be satisfied together.
                self.result["model_reported_contradiction"] = True
                raise self.escalate(failure, base, target, head)
            return
        record.update(state="proposed", metadata=proposal["metadata"], explanation=proposal["explanation"],
                      changed=sorted(proposal["files"]))
        for path, content in proposal["files"].items():
            if path not in allowed or (worktree / path).is_symlink():
                raise Stop("rejected", "repair_scope_violation", path, needs_human=True)
            if is_test(path):
                # In a test file the model may only drop conflict markers: every line it
                # returns must come verbatim from one of the two sides.
                sides = set((self.show(target, path) or "").splitlines()) | set((self.show(head, path) or "").splitlines())
                foreign = [line for line in content.splitlines() if line not in sides]
                if foreign:
                    record.update(state="rejected_test_edit", foreign_lines=foreign[:10])
                    self.save()
                    return
        for path, content in proposal["files"].items():
            (worktree / path).write_text(content, encoding="utf-8", newline="\n")
        self.save()

    def escalate(self, failure, base, target, head):
        tests = failure.get("tests") or {}
        target_ids = set(self.tests(target, "list")["ids"])
        head_ids = set(self.tests(head, "list")["ids"])
        # Every attempt counts: a contradiction shows up as different sides failing in turn.
        failed = sorted(set(tests.get("failed") or {}).union(*map(set, self.result.get("attempt_failures", [[]]))))
        sides = {"shared": sorted(t for t in failed if t in target_ids and t not in head_ids),
                 "pull_request": sorted(t for t in failed if t in head_ids and t not in target_ids),
                 "both": sorted(t for t in failed if t in head_ids and t in target_ids),
                 "new_in_merge": sorted(t for t in failed if t not in head_ids and t not in target_ids)}
        self.result["escalation"] = {"failure": failure, "failed_by_side": sides}
        contradiction = bool(sides["shared"] and sides["pull_request"]) or (
            self.result.get("model_reported_contradiction", False) and bool(sides["shared"] or sides["pull_request"]))
        reason = ("conflicting_requirements" if contradiction else
                  "unresolved_conflict" if failure.get("conflicts") else "tests_not_satisfied")
        return Stop("rejected", reason, json.dumps(sides, ensure_ascii=False), needs_human=True)

    def summary(self, title, candidate):
        repairs = self.result["repairs"]
        lines = ["**" + title + "** — итоговый агент", "",
                 "- Общая ветка: `" + self.result.get("target", "")[:12] + "`, результат: `" + candidate[:12] + "`",
                 "- Обязательных тестов обеих сторон: " + str(self.result.get("required_tests")),
                 "- Mergiraf: " + ("включён" if self.result.get("mergiraf") != "not_configured" else "выключен"),
                 "- Попыток модели: " + str(len(repairs)) + " из " + str(MAX_REPAIRS)]
        for item in repairs:
            lines.append("  - " + str(item.get("ordinal")) + ": " + str(item.get("state")) + " — "
                         + str(item.get("explanation") or item.get("reason") or ""))
        review = self.result.get("review") or {}
        if review.get("altered") or review.get("moved_parameters"):
            lines += ["", "**Проверьте решения модели** — строки авторов, которые не сохранились дословно:"]
            for side, title in (("shared", "общая ветка"), ("pull_request", "этот PR")):
                for path, items in (review.get("altered", {}).get(side) or {}).items():
                    lines.append("- " + title + ", `" + path + "`: " + "; ".join("`" + i[:80] + "`" for i in items[:8]))
                for path, items in (review.get("moved_parameters", {}).get(side) or {}).items():
                    lines.append("- " + title + ", `" + path + "`, API: " + "; ".join(items))
        return "\n".join(lines)

    def requeue_others(self, merged):
        if self.api is None:
            return
        try:
            pulls = paged(self.api, "/repos/" + self.api.repository + "/pulls?state=open&base=" + TARGET)
            for pr in pulls:
                if pr["number"] != merged and not pr.get("draft"):
                    dispatch(self.api, pr, "shared_branch_moved")
                    self.result.setdefault("requeued", []).append(pr["number"])
        except StatusError as error:
            self.result["status_errors"].append("requeue: " + str(error))
        self.save()

    def finish(self, stop):
        self.result.update(decision=stop.decision, reason=stop.reason, detail=stop.detail[:4000],
                           needs_human=stop.needs_human, finished_at=now())
        self.save()
        sha = self.result.get("expected_head")
        if stop.decision in ("rejected", "error") and sha:
            label = "Нужен человек: " if stop.needs_human else "Ошибка: "
            try:
                self.status(sha, "failure" if stop.decision == "rejected" else "error", label + stop.reason)
            except StatusError as error:
                self.result["status_errors"].append(str(error))
            if stop.needs_human and self.result.get("pr"):
                body = ["**Нужен человек** — итоговый агент не смог безопасно совместить изменения.", "",
                        "Причина: `" + stop.reason + "`"]
                sides = (self.result.get("escalation") or {}).get("failed_by_side")
                if sides:
                    for key, title in (("shared", "Падают тесты фич общей ветки"),
                                       ("pull_request", "Падают тесты этого PR"),
                                       ("both", "Падают общие тесты"), ("new_in_merge", "Прочее")):
                        if sides[key]:
                            body.append("- " + title + ": " + ", ".join("`" + t + "`" for t in sides[key][:15]))
                    if stop.reason == "conflicting_requirements":
                        body.append("\nПохоже на **противоречие требований**: тесты обеих сторон нельзя выполнить "
                                    "одновременно. Согласуйте поведение и обновите тесты в одной из веток.")
                elif stop.detail:
                    body.append("Подробности: " + stop.detail[:1500])
                body.append("\n" + self.summary("Сводка", self.result.get("head", sha)))
                self.comment(self.result["pr"], "\n".join(body))
        self.save()


def dispatch(api, pr, reason):
    api.request("POST", "/repos/" + api.repository + "/actions/workflows/fixbot.yml/dispatches",
                {"ref": "main", "inputs": {"pr_number": str(pr["number"]), "head_ref": pr["head"]["ref"],
                                           "expected_head": pr["head"]["sha"], "reason": reason}})


def sweep(api, run_id):
    """Requeue PR versions whose event was lost, whose run died or ended in an infrastructure error."""
    runs = paged(api, "/repos/" + api.repository + "/actions/workflows/fixbot.yml/runs?event=workflow_dispatch",
                 "workflow_runs")
    report = []
    for pr in paged(api, "/repos/" + api.repository + "/pulls?state=open&base=" + TARGET):
        if pr.get("draft") or pr["head"]["repo"]["full_name"] != api.repository:
            continue
        status = api.latest(pr["head"]["sha"], CommitStatuses.context(TARGET))
        decision = decide(pr, status, runs, datetime.now(timezone.utc))
        if decision in ("lost_event", "infrastructure_error", "stalled"):
            dispatch(api, pr, "sweep_" + decision)
        report.append({"pr": pr["number"], "head": pr["head"]["sha"], "decision": decision})
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("fix", "sweep", "guard"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--expected-head", default="")
    args = parser.parse_args()
    if args.mode == "sweep":
        report = sweep(CommitStatuses.from_environment(), os.environ.get("GITHUB_RUN_ID", "0"))
        Path(args.state_dir).mkdir(parents=True, exist_ok=True)
        Path(args.state_dir, "sweep.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return 0
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_head):
        raise SystemExit("--expected-head must be an exact commit SHA")
    bot = Bot(args.repo, args.state_dir)
    if args.mode == "guard":
        # Cheap early exit before the sandbox and Mergiraf are installed.
        try:
            bot.guard(args.pr, args.expected_head)
            proceed = "true"
        except Stop as stop:
            proceed = "false"
            print("skip:", stop.reason)
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as stream:
            stream.write("proceed=" + proceed + "\n")
        return 0
    try:
        bot.fix(args.pr, args.expected_head)
        return 0
    except Stop as stop:
        bot.finish(stop)
        return 0 if stop.decision in ("skipped", "verified") else 1
    except Exception as error:  # Unexpected: report as infrastructure error so the sweep retries.
        bot.finish(Stop("error", "internal_error", repr(error)[:2000]))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
