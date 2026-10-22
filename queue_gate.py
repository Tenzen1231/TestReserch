"""Small CI integration gate. Only ordinary forward pushes publish candidates.

Remote source/target observations are snapshots, not an atomic transaction
across both GitHub references. Informational source statuses are not publisher
authorization; only the exact merged and validated candidate may be pushed.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import traceback
import uuid

from queue_status import CommitStatuses, StatusError
from queue_repair import RouterRepairClient, RepairError, MODEL, strict_json
from queue_pr import guard as guard_pr, PRError, EVENTS, TARGET
from queue_structural import StructuralRepairClient, StructuralError
from queue_budget import reserve as reserve_budget, BudgetError
from acceptance import CONTRACTS, REGISTRY_PATH, contract_cases


ALLOWED = {"money.py", "receipt.py", "email_receipt.py", "feature_a.py", "feature_b.py", "feature_c.py"}
MAX_ATTEMPTS = 3
MAX_REPAIRS = 2
VALIDATION_IMAGE = "python@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2"
INFRASTRUCTURE_ERRORS = {"internal_error", "git_error", "status_delivery_failed", "trusted_control_changed",
                         "publication_unconfirmed", "validation_infrastructure_error", "candidate_not_observed",
                         "structural_infrastructure_error", "budget_unavailable"}
REASONS_RU = {
    "validation_failed": "проверки общего результата не пройдены",
    "merge_conflict": "изменения конфликтуют с общей веткой",
    "scope_violation": "изменены защищённые или необъявленные файлы",
    "unsupported_file_type": "ссылки и подмодули не поддерживаются",
    "retry_limit": "ветки менялись во время повторных проверок",
    "push_rejected": "сервер не принял публикацию после повторных проверок",
    "invalid_input": "неверные параметры запуска",
    "status_delivery_failed": "не удалось подтвердить статус в GitHub",
    "publication_unconfirmed": "исход публикации не подтверждён; проверьте запуск",
    "candidate_not_observed": "проверенный commit не найден в общей ветке",
    "validation_infrastructure_error": "среда проверки недоступна",
    "git_error": "не удалось выполнить операцию Git",
    "trusted_control_changed": "код проверки изменился во время запуска",
    "interrupted": "запуск прерван до подтверждения результата",
    "repair_exhausted": "две попытки ремонта не помогли; нужен человек",
    "repair_rerun_blocked": "повтор запуска не возобновляет ремонт; нужен человек",
    "invalid_requirements": "не удалось прочитать сохранённые требования",
    "requirement_not_recorded": "для принятой версии не зарегистрировано это требование",
    "pr_superseded": "есть более новый push; он проверяется отдельной заявкой",
    "pr_changed": "параметры PR изменились; нужна проверка нового события",
    "pr_not_ready": "PR закрыт или ещё является черновиком",
    "invalid_pr": "заявка не соответствует открытому PR команды",
    "invalid_pr_requirement": "в описании PR нужно указать одно известное требование",
    "structural_infrastructure_error": "не удалось запустить проверенный Mergiraf",
    "budget_unavailable": "не удалось подтвердить сохранённый лимит ремонта",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def complete_validation_report(stdout, returncode, requirements=()):
    """Fail closed if imported code exits before the fixed oracle finishes."""
    expected = {case[0]: case[-1] for case in contract_cases(requirements)}
    try:
        report = json.loads(stdout)
        checks = report["checks"]
        identifiers = [check["check"] for check in checks]
        if (returncode != 0 or report.get("passed") is not True or len(checks) != len(expected) + 1
                or set(identifiers) != set(expected) | {"three_way_interaction"}
                or any(check.get("passed") is not True for check in checks)):
            return False, report
        for check in checks:
            if check["check"] in expected:
                value = expected[check["check"]]
                if (type(check.get("actual")) is not type(value)
                        or check.get("expected") != value or check.get("actual") != value):
                    return False, report
            else:
                flags = check.get("flags")
                if not isinstance(flags, list) or len(flags) != 3 or any(type(flag) is not bool for flag in flags) or all(flags):
                    return False, report
        return True, report
    except (ValueError, TypeError, KeyError, AttributeError):
        return False, None


class Rejected(RuntimeError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


class Gate:
    def __init__(self, repo, state_dir):
        self.repo = Path(repo).resolve()
        self.directory = Path(state_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.result_path = self.directory / "result.json"
        self.state_path = self.directory / "state.json"
        self.log_path = self.directory / "gate.log"
        self.oracle = self.repo / "acceptance.py"
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.auth = base64.b64encode(("x-access-token:" + self.token).encode()).decode() if self.token else ""
        self.statuses = None
        self.state = {}
        self.result = {"version": 1, "decision": "rejected", "reason": "not_prepared", "attempts": [],
                       "observations": [], "created_at": now(), "run_id": os.environ.get("GITHUB_RUN_ID"),
                       "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                       "repository": os.environ.get("GITHUB_REPOSITORY"), "status_events": [], "status_errors": []}

    def status(self, sha, value, description):
        if self.statuses is None:
            self.statuses = CommitStatuses.from_environment()
        if self.statuses is None:
            self.result["status_delivery"] = "disabled_local"
            self.save()
            return
        event = self.statuses.send(sha, self.state["base_branch"], value, description)
        event.update(base=self.result.get("base"), candidate=self.result.get("candidate"),
                     target_branch=self.state["base_branch"], at=now())
        if not any(item.get("id") == event.get("id") for item in self.result.setdefault("status_events", [])):
            self.result["status_events"].append(event)
        self.result["status_context"] = event["context"]
        self.result["status_delivery"] = "confirmed"
        self.save()

    def select_source(self, source):
        previous = self.state.get("active_source")
        if previous and previous != source:
            self.status(previous, "failure", "Отклонено: версия изменена; проверяется " + source[:8])
        self.state["active_source"] = source
        self.save()

    def clean(self, value):
        value = str(value)
        for secret in (self.token, self.auth, os.environ.get("ROUTERAI_API_KEY")):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def log(self, text):
        message = self.clean(text)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(now() + " " + message + "\n")
        print(message, flush=True)

    def save(self):
        self.result["updated_at"] = now()
        for path, value in ((self.state_path, self.state), (self.result_path, self.result)):
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)

    def git(self, *args, cwd=None, check=True):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        # No credential is written to a URL, command line, log or Git config.
        if self.auth:
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + self.auth)
        result = subprocess.run(["git", "-c", "commit.gpgsign=false",
                                 "-c", "user.name=Council CI gate", "-c", "user.email=ci-gate@localhost",
                                 *args], cwd=cwd or self.repo, env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=180)
        if result.stdout.strip():
            self.log(result.stdout.strip())
        if result.stderr.strip():
            self.log(result.stderr.strip())
        if check and result.returncode:
            raise Rejected("git_error", self.clean(result.stderr.strip() or result.stdout.strip()))
        return result

    def text(self, *args, **kwargs):
        return self.git(*args, **kwargs).stdout.strip()

    def ancestor(self, older, newer):
        result = self.git("merge-base", "--is-ancestor", older, newer, check=False)
        if result.returncode not in (0, 1):
            raise Rejected("invalid_history", self.clean(result.stderr))
        return result.returncode == 0

    def observe(self, stage):
        if stage != "after_publication":
            self.check_pr(stage)
        # Fresh local refs avoid forced refspecs, even for observations after a
        # rejected push. The checked-out trusted main branch never moves.
        prefix = "refs/council-observations/" + uuid.uuid4().hex
        base_ref = "refs/heads/" + self.state["base_branch"]
        source_ref = "refs/heads/" + self.state["source_branch"]
        self.log("Observing remote source and target: " + stage)
        self.git("fetch", "--quiet", "--no-tags", "origin", base_ref + ":" + prefix + "/base",
                 source_ref + ":" + prefix + "/source")
        observation = {"stage": stage, "at": now(), "base": self.text("rev-parse", prefix + "/base"),
                       "source": self.text("rev-parse", prefix + "/source")}
        if stage != "after_publication" and self.state.get("pr_request"):
            if observation["source"] != self.state["pr_request"]["expected_head"]:
                raise Rejected("pr_superseded", "Fetched source differs from the PR event version")
        self.result["observations"].append(observation)
        self.save()
        return observation

    def check_pr(self, stage):
        request = self.state.get("pr_request")
        if request is None:
            return
        if self.statuses is None:
            self.statuses = CommitStatuses.from_environment()
        try:
            observed = guard_pr(self.statuses, request)
        except PRError as error:
            raise Rejected(error.reason, str(error)) from None
        self.result.setdefault("pr_observations", []).append({"stage": stage, "at": now(), **observed})
        self.save()

    def reject(self, reason, detail=None):
        infrastructure = reason in INFRASTRUCTURE_ERRORS
        label = "Ошибка" if infrastructure else "Отклонено"
        description = label + ": " + REASONS_RU.get(reason, reason)
        self.result.update(decision="error" if infrastructure else "rejected", reason=reason,
                           decision_label=label, description=description)
        if reason in ("repair_exhausted", "repair_rerun_blocked"):
            self.result["needs_human"] = True
        if detail:
            self.result["detail"] = self.clean(detail)
        self.log(description)
        self.save()

        if self.state.get("active_source"):
            try:
                self.status(self.state["active_source"], "error" if infrastructure else "failure", description)
            except StatusError as error:
                self.result.setdefault("status_errors", []).append(str(error))
                self.result["status_delivery"] = "failed"
                self.save()

    def validate_inputs(self, base, source, hold):
        for branch in (base, source):
            if not branch.startswith("feature/") or self.git(
                    "check-ref-format", "refs/heads/" + branch, check=False).returncode:
                raise Rejected("invalid_input", "Only feature/* branches are allowed")
        if base == source:
            raise Rejected("invalid_input", "Source and target must be different branches")
        try:
            seconds = int(hold)
        except ValueError as error:
            raise Rejected("invalid_input", "hold_seconds must be an integer") from error
        if not 0 <= seconds <= 90:
            raise Rejected("invalid_input", "hold_seconds must be between 0 and 90")
        return seconds

    def paths(self, older, newer):
        return [p for p in self.git("diff", "--name-only", "--no-renames", "-z", older, newer).stdout.split("\0") if p]

    def ordinary_files(self, commit):
        for entry in self.git("ls-tree", "-rz", commit).stdout.split("\0"):
            if entry and entry.split(" ", 1)[0] not in ("100644", "100755"):
                raise Rejected("unsupported_file_type", "Symlinks and submodules are not accepted")

    def trusted_unchanged(self):
        if (self.text("rev-parse", "HEAD") != self.state["trusted_head"]
                or digest(self.oracle) != self.state["validator_sha256"]
                or digest(__file__) != self.state["gate_sha256"]
                or digest(Path(__file__).with_name("queue_repair.py")) != self.state["repair_module_sha256"]
                or digest(Path(__file__).with_name("queue_status.py")) != self.state["status_module_sha256"]):
            raise Rejected("trusted_control_changed", "Trusted gate/oracle checkout changed during the run")
        for name, expected in self.state.get("extra_control_hashes", {}).items():
            if digest(self.repo / name) != expected:
                raise Rejected("trusted_control_changed", "Trusted queue module changed during the run")

    def validation_command(self, tested_commit):
        github = os.environ.get("GITHUB_ACTIONS") == "true"
        if not github and os.environ.get("QUEUE_VALIDATION_DOCKER") != "true":
            return None, None
        image = VALIDATION_IMAGE if github else os.environ.get("VALIDATION_IMAGE", VALIDATION_IMAGE)
        if not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", image):
            raise Rejected("validation_infrastructure_error", "Validator image must be pinned by digest")
        export = self.directory / ("validation-tree-" + uuid.uuid4().hex)
        export.mkdir()
        archive = subprocess.run(["git", "archive", "--format=tar", tested_commit], cwd=self.repo,
                                 capture_output=True, check=True, timeout=60).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
            for member in stream.getmembers():
                parts = PurePosixPath(member.name).parts
                if member.name.startswith("/") or any(p in ("..", ".git") for p in parts):
                    raise Rejected("validation_infrastructure_error", "Unsafe archive path")
                path = export.joinpath(*parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(stream.extractfile(member).read())
                    path.chmod(0o444)
                else:
                    raise Rejected("unsupported_file_type", "Only regular files may enter validation")
        container = "council-validation-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--pull=never", "--name", container, "--network=none", "--read-only",
                   "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64",
                   "--memory=256m", "--cpus=1", "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m",
                   "--mount", "type=bind,src=" + str(export) + ",dst=/candidate,readonly",
                   "--mount", "type=bind,src=" + str(self.oracle) + ",dst=/oracle/acceptance.py,readonly",
                   "--workdir=/candidate", image, "python", "-I", "-B", "/oracle/acceptance.py", "/candidate",
                   json.dumps(self.state.get("requirements", []))]
        return command, {"container": container, "image": image, "network": "none", "exported_commit": tested_commit}

    def run_validation(self, directory, tested_commit, scope):
        before_hash = digest(self.oracle)
        # Keep credentials out of the code-under-test process environment.
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        self.log("Validating " + scope + " commit " + tested_commit)
        command, sandbox = self.validation_command(tested_commit)
        try:
            process = subprocess.run(command or [sys.executable, "-I", "-B", str(self.oracle), str(directory),
                                                  json.dumps(self.state.get("requirements", []))],
                                     cwd=self.repo, env=env, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=120)
            if sandbox and process.returncode in (125, 126, 127):
                raise Rejected("validation_infrastructure_error", self.clean(process.stderr[-2000:]))
            passed, report = complete_validation_report(process.stdout, process.returncode, self.state.get("requirements", []))
            validation = {"passed": passed, "returncode": process.returncode, "report": report,
                          "stdout": self.clean(process.stdout[-16000:]), "stderr": self.clean(process.stderr[-16000:]),
                          "tested_commit": tested_commit, "scope": scope, "validator_sha256": before_hash}
        except subprocess.TimeoutExpired:
            if sandbox:
                subprocess.run(["docker", "kill", sandbox["container"]], capture_output=True, timeout=20)
            validation = {"passed": False, "returncode": None, "stdout": "", "stderr": "Validator timed out",
                          "tested_commit": tested_commit, "scope": scope, "validator_sha256": before_hash}
        validation["sandbox"] = sandbox or {"kind": "local_process"}
        if validation["stdout"]:
            self.log(validation["stdout"])
        if validation["stderr"]:
            self.log(validation["stderr"])
        dirty = self.text("status", "--porcelain", "--untracked-files=all", "--ignored", cwd=directory)
        if (dirty or self.text("rev-parse", "HEAD", cwd=directory) != tested_commit
                or digest(self.oracle) != before_hash):
            validation.update(passed=False, integrity_error="Tested checkout or trusted oracle changed during validation")
        self.trusted_unchanged()
        return validation

    def read_requirements(self, commit):
        value = self.git("show", commit + ":" + REGISTRY_PATH, check=False)
        try:
            if value.returncode or len(value.stdout) > 4096:
                raise ValueError()
            registry = strict_json(value.stdout)
            accepted = registry["accepted"]
            if (set(registry) != {"version", "accepted"} or type(registry["version"]) is not int
                    or registry["version"] != 1 or not isinstance(accepted, list)
                    or any(not isinstance(item, str) for item in accepted)
                    or len(accepted) != len(set(accepted)) or set(accepted) - set(CONTRACTS)):
                raise ValueError()
            return sorted(accepted)
        except (ValueError, TypeError, KeyError):
            raise Rejected("invalid_requirements", "Missing or invalid requirement registry at " + commit) from None

    def candidate_commit(self, directory, source, base, attempt):
        if self.text("diff", "--name-only", "--diff-filter=U", cwd=directory):
            raise Rejected("merge_conflict", "Unresolved file conflicts remain")
        registry = directory / REGISTRY_PATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"version": 1, "accepted": self.state["requirements"]},
                                       sort_keys=True) + "\n", encoding="utf-8")
        self.git("add", "--", REGISTRY_PATH, cwd=directory)
        changed = self.text("diff", "--cached", "--name-only", cwd=directory)
        merge_head = self.git("rev-parse", "--verify", "MERGE_HEAD", cwd=directory, check=False)
        if changed or merge_head.returncode == 0:
            self.git("commit", "-m", "feat: согласованы изменения и сохранённые требования", cwd=directory)
        candidate = self.text("rev-parse", "HEAD", cwd=directory)
        tree = self.text("rev-parse", "HEAD^{tree}", cwd=directory)
        if not self.ancestor(base, candidate) or not self.ancestor(source, candidate):
            raise Rejected("non_forward_candidate", "Candidate must preserve both source and target history")
        paths = self.paths(base, candidate)
        if set(paths) - ALLOWED - {REGISTRY_PATH}:
            raise Rejected("scope_violation", "Candidate changes trusted control files")
        if self.read_requirements(candidate) != self.state["requirements"]:
            raise Rejected("invalid_requirements", "Candidate lost accepted requirements")
        self.ordinary_files(candidate)
        attempt.update(candidate=candidate, tree=tree, candidate_changed_paths=paths)
        self.result.update(candidate=candidate, tree=tree)
        validation = self.run_validation(directory, candidate, "merged_candidate")
        attempt.update(validation=validation, finished_at=now(), decision="prepared" if validation["passed"] else "rejected")
        self.result["validation"] = validation
        self.save()
        if not validation["passed"]:
            raise Rejected("validation_failed", "Trusted acceptance checks rejected the tested snapshot")
        return candidate, tree

    def repair_context(self, directory, common, source, base, refusal):
        def snapshot(commit):
            files = {}
            for name in sorted(ALLOWED):
                result = self.git("show", commit + ":" + name, check=False)
                if result.returncode == 0:
                    files[name] = result.stdout
            return files
        return {
            "allowed_paths": sorted(ALLOWED),
            "requirements": {key: CONTRACTS[key] for key in self.state["requirements"]},
            "baseline_requirement": "receipt.format_total and email_receipt.format_email_total return original ruble amounts with two decimals. Preserve unrelated enabled feature flags; all three flags simultaneously enabled are incompatible.",
            "new_requirement": self.state["requirement"],
            "refusal": refusal,
            "base_sha": base, "source_sha": source,
            "common_ancestor": snapshot(common), "current_target": snapshot(base), "submission": snapshot(source),
            "current_candidate": {name: (directory / name).read_text(encoding="utf-8")
                                  for name in sorted(ALLOWED) if (directory / name).is_file()},
            "previous_repairs": [{"reason": item.get("reason"), "explanation": item.get("explanation")}
                                 for item in self.result.get("repairs", [])],
            "conflict_paths": sorted(self.text("diff", "--name-only", "--diff-filter=U", cwd=directory).splitlines()),
        }

    def apply_proposal(self, directory, proposal):
        self.trusted_unchanged()
        for name, content in proposal["files"].items():
            if name not in ALLOWED or (directory / name).is_symlink():
                raise Rejected("scope_violation", "Repair attempted to change a protected path")
            (directory / name).write_text(content, encoding="utf-8")
        self.git("add", "--", *sorted(proposal["files"]), cwd=directory)

    def structural_candidate(self, directory, common, source, base, attempt):
        record = {"started_at": now(), "source": source, "base": base, "state": "started"}
        self.result.setdefault("structural_repairs", []).append(record)
        self.save()
        self.status(source, "pending", "Соединяем структуру кода через Mergiraf")
        try:
            client = StructuralRepairClient.from_environment(allowed_paths=ALLOWED)
            proposal = client.propose(self.repair_context(directory, common, source, base,
                                                         {"reason": "merge_conflict"}))
            record.update(metadata=proposal["metadata"], explanation=proposal["explanation"],
                          changed_paths=sorted(proposal["files"]),
                          patch_sha256=hashlib.sha256(json.dumps(proposal["files"], sort_keys=True).encode()).hexdigest())
            self.apply_proposal(directory, proposal)
            candidate, tree = self.candidate_commit(directory, source, base, attempt)
            record.update(state="validated", candidate=candidate, tree=tree,
                          validation=self.result["validation"], finished_at=now())
            self.save()
            return candidate, tree
        except StructuralError as error:
            record.update(state="unresolved", reason=error.reason, detail=self.clean(error.detail),
                          metadata=error.metadata, finished_at=now())
            self.save()
            if error.reason not in {"structural_unresolved", "structural_invalid_input", "structural_timeout",
                                    "structural_invalid_output", "structural_input_too_large", "structural_output_too_large"}:
                raise Rejected("structural_infrastructure_error", error.reason) from None
            raise Rejected("merge_conflict", "Mergiraf did not produce a complete resolution") from None
        except Rejected as error:
            record.update(state="rejected", reason=error.reason, finished_at=now())
            self.save()
            raise

    def repair_candidate(self, directory, common, source, base, attempt, error):
        if not self.state["auto_repair"]:
            raise error
        if int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")) > 1:
            raise Rejected("repair_rerun_blocked", "A job rerun cannot reset the automatic repair budget")
        refusal = {"reason": error.reason, "validation": self.result.get("validation")}
        while self.state["repair_calls_reserved"] < MAX_REPAIRS:
            self.check_pr("before_repair_reservation")
            reservation = None
            if self.state["durable_budget"]:
                try:
                    reservation = reserve_budget(self.git, os.environ.get("GITHUB_REPOSITORY", ""),
                                                 self.state["base_branch"], source,
                                                 os.environ.get("GITHUB_RUN_ID", ""),
                                                 int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
                                                 remote=self.publisher_remote())
                except BudgetError as failure:
                    self.result["budget_refusal"] = {"reason": failure.reason, "detail": failure.detail,
                                                     "metadata": failure.metadata}
                    self.save()
                    reason = "repair_exhausted" if failure.reason == "budget_exhausted" else "budget_unavailable"
                    raise Rejected(reason, failure.detail) from None
            # Reserve durably BEFORE constructing the HTTP client or making a
            # request. Unknown outcomes and invalid responses consume budget.
            self.state["repair_calls_reserved"] += 1
            record = {"number": self.state["repair_calls_reserved"], "started_at": now(),
                      "source": source, "base": base, "refusal": refusal, "state": "reserved"}
            if reservation:
                record["budget_reservation"] = reservation
            self.result.setdefault("repairs", []).append(record)
            self.save()
            ordinal = reservation["ordinal"] if reservation else record["number"]
            self.status(source, "pending", "Исправляем автоматически: попытка " + str(ordinal) + "/2")
            try:
                self.check_pr("before_router_post")
                client = RouterRepairClient.from_environment(allowed_paths=ALLOWED)
                proposal = client.propose(self.repair_context(directory, common, source, base, refusal))
                record.update(metadata=proposal["metadata"], explanation=proposal["explanation"],
                              patch_sha256=hashlib.sha256(json.dumps(proposal["files"], sort_keys=True).encode()).hexdigest(),
                              changed_paths=sorted(proposal["files"]), state="proposed")
                self.save()
                self.apply_proposal(directory, proposal)
                candidate, tree = self.candidate_commit(directory, source, base, attempt)
                record.update(state="validated", candidate=candidate, tree=tree,
                              validation=self.result["validation"], finished_at=now())
                self.save()
                return candidate, tree
            except RepairError as failure:
                record.update(state="failed", reason=failure.reason, metadata=failure.metadata,
                              detail=self.clean(failure.detail), finished_at=now())
                refusal = {"reason": failure.reason, "detail": failure.detail}
            except Rejected as failure:
                if failure.reason not in ("validation_failed", "merge_conflict"):
                    raise
                record.update(state="failed", reason=failure.reason, candidate=self.result.get("candidate"),
                              validation=self.result.get("validation"), finished_at=now())
                refusal = {"reason": failure.reason, "validation": self.result.get("validation")}
            self.save()
        raise Rejected("repair_exhausted", "No safe repair after two reserved API attempts; preserve the branches for a human")

    def build(self, observation, trigger):
        source, base = observation["source"], observation["base"]
        self.select_source(source)
        number = len(self.result["attempts"]) + 1
        attempt = {"number": number, "trigger": trigger, "started_at": now(), "source": source,
                   "base": base, "candidate": None, "tree": None, "validator_sha256": self.state["validator_sha256"]}
        self.result["attempts"].append(attempt)
        self.result.update(source=source, base=base, candidate=None, tree=None, validation=None)
        accepted = self.read_requirements(base)
        incoming = self.state["requirement"]
        self.state["requirements"] = sorted(set(accepted) | ({incoming} if incoming != "baseline" else set()))
        attempt.update(accepted_requirements=accepted, required_contracts=self.state["requirements"])
        self.save()
        if self.ancestor(source, base):
            if incoming != "baseline" and incoming not in accepted:
                raise Rejected("requirement_not_recorded", "Integrated source was not accepted under this requirement")
            attempt["decision"] = "already_integrated"
            self.result.update(decision="already_integrated", reason="source_is_already_in_target",
                               observed_remote_head=base, observed_source_head=source,
                               decision_label="Принято ранее", integration_confirmed=True)
            self.status(source, "success", "Принято ранее: " + source[:8] + " уже входит в " + base[:8])
            self.save()
            return False
        self.status(source, "pending", "Проверяем " + source[:8] + ": база " + base[:8])
        common = self.text("merge-base", base, source)
        changed = self.paths(common, source)
        attempt["source_changed_paths"] = changed
        if set(changed) - ALLOWED:
            raise Rejected("scope_violation", "Submission changes protected or undeclared files: "
                           + ", ".join(sorted(set(changed) - ALLOWED)))
        self.ordinary_files(source)
        self.ordinary_files(base)
        directory = self.directory / ("candidate-" + str(number))
        self.git("worktree", "add", "--quiet", "--detach", str(directory), base)
        merge = self.git("merge", "--no-ff", "--no-commit", source,
                         cwd=directory, check=False)
        try:
            if merge.returncode:
                conflicts = set(self.text("diff", "--name-only", "--diff-filter=U", cwd=directory).splitlines())
                if not conflicts or conflicts - ALLOWED:
                    raise Rejected("git_error", "Merge failed outside repairable product paths")
                attempt["merge_output"] = self.clean(merge.stdout + merge.stderr)
                raise Rejected("merge_conflict", "Cannot construct a clean candidate from current source and target")
            candidate, tree = self.candidate_commit(directory, source, base, attempt)
        except Rejected as error:
            if error.reason not in ("merge_conflict", "validation_failed"):
                raise
            attempt["initial_rejection"] = error.reason
            self.save()
            if error.reason == "merge_conflict" and self.state["structural_repair"]:
                try:
                    candidate, tree = self.structural_candidate(directory, common, source, base, attempt)
                except Rejected as structural_error:
                    if structural_error.reason not in ("merge_conflict", "validation_failed"):
                        raise
                    candidate, tree = self.repair_candidate(directory, common, source, base, attempt, structural_error)
            else:
                candidate, tree = self.repair_candidate(directory, common, source, base, attempt, error)
        self.state["prepared"] = {"source": source, "base": base, "candidate": candidate,
                                  "tree": tree, "attempt": number, "directory": str(directory)}
        self.result.update(decision="prepared", reason="checks_passed", decision_label="Ожидает публикации")
        self.status(source, "pending", "Ожидает публикации: " + source[:8] + ", база " + base[:8] + ", кандидат " + candidate[:8])
        self.save()
        return True

    def prepare(self, args):
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.result = json.loads(self.result_path.read_text(encoding="utf-8"))
            raise Rejected("invalid_input", "Prepare cannot reset an existing request state")
        seconds = self.validate_inputs(args.base_branch, args.source_branch, args.hold_seconds)
        if args.requirement not in {"baseline", *CONTRACTS}:
            raise Rejected("invalid_input", "Unknown trusted requirement")
        self.state.update(base_branch=args.base_branch, source_branch=args.source_branch,
                          requirement=args.requirement, auto_repair=os.environ.get("QUEUE_AUTO_REPAIR") == "true",
                          structural_repair=os.environ.get("QUEUE_STRUCTURAL_REPAIR") == "true",
                          durable_budget=os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("QUEUE_DURABLE_BUDGET") == "true",
                          repair_calls_reserved=0,
                          hold_seconds=seconds, repo=str(self.repo), trusted_head=self.text("rev-parse", "HEAD"),
                          validator_sha256=digest(self.oracle), gate_sha256=digest(__file__),
                          repair_module_sha256=digest(Path(__file__).with_name("queue_repair.py")),
                          status_module_sha256=digest(Path(__file__).with_name("queue_status.py")))
        self.state["extra_control_hashes"] = {name: digest(self.repo / name) for name in
                                              ("queue_pr.py", "queue_structural.py", "queue_budget.py")}
        if os.environ.get("GITHUB_ACTIONS") == "true":
            number, expected, dispatcher = (os.environ.get(key, "") for key in
                                            ("PR_NUMBER", "EXPECTED_HEAD", "DISPATCHER_RUN_ID"))
            action = os.environ.get("PR_EVENT_ACTION", "")
            if (args.base_branch != TARGET or not number.isdigit() or not dispatcher.isdigit()
                    or not re.fullmatch(r"[0-9a-f]{40}", expected) or action not in EVENTS):
                raise Rejected("invalid_pr", "A PR event number, exact head and dispatcher trace are required")
            self.state["pr_request"] = {"pr_number": int(number), "expected_head": expected,
                                        "source_branch": args.source_branch, "requirement": args.requirement}
            self.result.update(pr_number=int(number), expected_head=expected,
                               dispatcher_run_id=dispatcher, event_action=action)
        self.result.update(base_branch=args.base_branch, source_branch=args.source_branch,
                           validator_sha256=self.state["validator_sha256"],
                           gate_sha256=self.state["gate_sha256"], trusted_head=self.state["trusted_head"],
                           max_attempts=MAX_ATTEMPTS)
        self.result.update(max_repair_calls=MAX_REPAIRS, repair_model=MODEL, repairs=[],
                           structural_repairs=[], structural_repair=self.state["structural_repair"],
                           requirement=args.requirement, auto_repair=self.state["auto_repair"],
                           repair_budget_scope="repository + target branch + immutable source SHA" if self.state["durable_budget"]
                           else "local test run; job reruns cannot call the repair API")
        self.save()
        self.build(self.observe("prepare"), "initial_prepare")

    def accepted(self, reason):
        candidate = self.state["prepared"]["candidate"]
        self.result.update(publication_attempted=True, attempted_candidate=candidate)
        try:
            observed = self.observe("after_publication")
            self.result.update(observed_remote_head=observed["base"], observed_source_head=observed["source"],
                               candidate_in_observed_history=self.ancestor(candidate, observed["base"]),
                               source_advanced_after_validation=observed["source"] != self.state["prepared"]["source"])
        except (Rejected, subprocess.TimeoutExpired) as error:
            self.result["observation_error"] = self.clean(error)
            raise Rejected("publication_unconfirmed", "Push outcome requires a later remote observation") from error
        if not self.result["candidate_in_observed_history"]:
            raise Rejected("candidate_not_observed", "Remote target does not contain the exact tested candidate")
        self.result.update(publication_confirmed=True, published_candidate=candidate)
        self.status(self.state["prepared"]["source"], "success", "Принято: " + self.state["prepared"]["source"][:8]
                    + ", база " + self.state["prepared"]["base"][:8] + ", опубликован " + candidate[:8])
        self.result.update(decision="accepted", reason=reason, decision_label="Принято")
        self.result["attempts"][-1]["decision"] = "accepted"
        self.log("Принято: " + candidate)
        self.save()

    def publisher_remote(self):
        if os.environ.get("QUEUE_SSH_PUBLISH") != "true":
            return "origin"
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise Rejected("invalid_input", "SSH publication requires the GitHub repository identity")
        return "git@github.com:" + repository + ".git"

    def publish(self):
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.result = json.loads(self.result_path.read_text(encoding="utf-8"))
        if self.result["decision"] != "prepared":
            self.log("Publication skipped: " + self.result["decision"])
            return
        self.trusted_unchanged()
        while True:
            observed = self.observe("before_publication")
            prepared = self.state["prepared"]
            if observed["source"] != prepared["source"] or observed["base"] != prepared["base"]:
                if len(self.result["attempts"]) >= MAX_ATTEMPTS:
                    raise Rejected("retry_limit", "Source or target kept changing; no stale candidate was published")
                if not self.build(observed, "source_or_target_changed"):
                    return
                continue  # Observe again after validation, before the push.
            self.trusted_unchanged()
            if self.text("rev-parse", prepared["candidate"] + "^{tree}") != prepared["tree"]:
                raise Rejected("candidate_changed", "Immutable candidate no longer matches its recorded tree")
            if not self.ancestor(prepared["base"], prepared["candidate"]):
                raise Rejected("non_forward_candidate", "Publication must preserve the target history")
            if self.result["validation"]["tested_commit"] != prepared["candidate"]:
                raise Rejected("untested_candidate", "Published SHA must equal the tested SHA")
            remote = self.publisher_remote()
            self.result.update(publication_attempted=True, attempted_candidate=prepared["candidate"])
            self.save()
            try:
                push = self.git("push", "--porcelain", remote,
                                prepared["candidate"] + ":refs/heads/" + self.state["base_branch"], check=False)
            except (subprocess.TimeoutExpired, OSError) as error:
                raise Rejected("publication_unconfirmed", "The push response is unavailable") from error
            self.result["attempts"][-1]["push"] = {"returncode": push.returncode,
                                                    "stdout": self.clean(push.stdout), "stderr": self.clean(push.stderr)}
            self.save()
            if push.returncode == 0:
                self.accepted("published_tested_candidate")
                return
            try:
                latest = self.observe("after_rejected_push")
            except (Rejected, subprocess.TimeoutExpired) as error:
                raise Rejected("publication_unconfirmed", "Cannot determine whether the attempted push was applied") from error
            if self.ancestor(prepared["candidate"], latest["base"]):
                self.accepted("accepted_after_ambiguous_push_response")
                return
            if len(self.result["attempts"]) >= MAX_ATTEMPTS:
                raise Rejected("push_rejected", "Ordinary push failed; retry limit reached without rewriting remote history")
            if not self.build(latest, "push_rejected_revalidate"):
                return

    def finalize(self):
        if not self.state_path.exists() or not self.result_path.exists():
            return
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.result = json.loads(self.result_path.read_text(encoding="utf-8"))
        if self.result["decision"] == "prepared":
            self.reject("publication_unconfirmed" if self.result.get("publication_attempted") else "interrupted")
        elif self.result["decision"] in ("error", "rejected") and self.result.get("status_delivery") == "failed":
            self.status(self.state["active_source"], "error" if self.result["decision"] == "error" else "failure",
                        self.result.get("description", "Ошибка: итог запуска не доставлен"))

    def output(self):
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                stream.write("ready=" + str(self.result["decision"] == "prepared").lower() + "\n")
                stream.write("decision=" + self.result["decision"] + "\n")
        self.log("FINAL " + self.result["decision"].upper() + ": " + self.result["reason"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "publish", "finalize"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--base-branch", default="")
    parser.add_argument("--source-branch", default="")
    parser.add_argument("--hold-seconds", default="0")
    parser.add_argument("--requirement", default="baseline")
    args = parser.parse_args()
    gate = Gate(args.repo, args.state_dir)
    exit_code = 0
    try:
        if args.phase == "prepare":
            gate.prepare(args)
        elif args.phase == "publish":
            gate.publish()
        else:
            gate.finalize()
    except Rejected as error:
        gate.reject(error.reason, str(error))
        exit_code = 1
    except StatusError as error:
        gate.reject("status_delivery_failed", str(error))
        exit_code = 1
    except Exception as error:
        gate.reject("internal_error", str(error))
        gate.log(traceback.format_exc())
        exit_code = 1
    gate.output()
    if gate.result["decision"] not in ("accepted", "already_integrated", "prepared"):
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
