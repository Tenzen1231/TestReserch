"""Small CI integration gate. Only ordinary forward pushes publish candidates.

The pr_only mode is an intentionally unsafe negative control, never an
alternative production policy. Remote source/target observations are snapshots,
not an atomic transaction across both GitHub references.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
import uuid


ALLOWED = {"money.py", "receipt.py", "email_receipt.py", "feature_a.py", "feature_b.py", "feature_c.py"}
MAX_ATTEMPTS = 3


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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
        self.state = {}
        self.result = {"version": 1, "decision": "rejected", "reason": "not_prepared", "attempts": [],
                       "observations": [], "created_at": now(), "run_id": os.environ.get("GITHUB_RUN_ID"),
                       "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                       "repository": os.environ.get("GITHUB_REPOSITORY")}

    def clean(self, value):
        value = str(value)
        for secret in (self.token, self.auth):
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
        self.result["observations"].append(observation)
        self.save()
        return observation

    def reject(self, reason, detail=None):
        self.result.update(decision="rejected", reason=reason)
        if detail:
            self.result["detail"] = self.clean(detail)
        self.log("REJECTED: " + reason)
        self.save()

    def validate_inputs(self, base, source, mode, hold):
        if mode not in ("enforce", "pr_only"):
            raise Rejected("invalid_input", "mode must be enforce or pr_only")
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
                or digest(__file__) != self.state["gate_sha256"]):
            raise Rejected("trusted_control_changed", "Trusted gate/oracle checkout changed during the run")

    def run_validation(self, directory, tested_commit, scope):
        before_hash = digest(self.oracle)
        # Keep credentials out of the code-under-test process environment.
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        self.log("Validating " + scope + " commit " + tested_commit)
        try:
            process = subprocess.run([sys.executable, "-I", "-B", str(self.oracle), str(directory)],
                                     cwd=self.repo, env=env, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=120)
            validation = {"passed": process.returncode == 0, "returncode": process.returncode,
                          "stdout": self.clean(process.stdout[-16000:]), "stderr": self.clean(process.stderr[-16000:]),
                          "tested_commit": tested_commit, "scope": scope, "validator_sha256": before_hash}
        except subprocess.TimeoutExpired:
            validation = {"passed": False, "returncode": None, "stdout": "", "stderr": "Validator timed out",
                          "tested_commit": tested_commit, "scope": scope, "validator_sha256": before_hash}
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

    def build(self, observation, trigger):
        source, base = observation["source"], observation["base"]
        number = len(self.result["attempts"]) + 1
        attempt = {"number": number, "trigger": trigger, "started_at": now(), "source": source,
                   "base": base, "candidate": None, "tree": None, "validator_sha256": self.state["validator_sha256"]}
        self.result["attempts"].append(attempt)
        self.result.update(source=source, base=base, candidate=None, tree=None, validation=None)
        self.save()
        if self.ancestor(source, base):
            attempt["decision"] = "already_integrated"
            self.result.update(decision="already_integrated", reason="source_is_already_in_target",
                               observed_remote_head=base, observed_source_head=source)
            self.save()
            return False
        common = self.text("merge-base", base, source)
        changed = self.paths(common, source)
        attempt["source_changed_paths"] = changed
        if set(changed) - ALLOWED:
            raise Rejected("scope_violation", "Submission changes protected or undeclared files: "
                           + ", ".join(sorted(set(changed) - ALLOWED)))
        self.ordinary_files(source)
        directory = self.directory / ("candidate-" + str(number))
        self.git("worktree", "add", "--quiet", "--detach", str(directory), base)
        merge = self.git("merge", "--no-ff", "--no-edit", "-m", "test: объединено проверенное изменение", source,
                         cwd=directory, check=False)
        if merge.returncode:
            attempt.update(decision="rejected", reason="merge_conflict", merge_output=self.clean(merge.stdout + merge.stderr))
            raise Rejected("merge_conflict", "Cannot construct a clean candidate from current source and target")
        candidate = self.text("rev-parse", "HEAD", cwd=directory)
        tree = self.text("rev-parse", "HEAD^{tree}", cwd=directory)
        attempt.update(candidate=candidate, tree=tree)
        self.result.update(candidate=candidate, tree=tree)
        candidate_changed = self.paths(base, candidate)
        attempt["candidate_changed_paths"] = candidate_changed
        if set(candidate_changed) - ALLOWED:
            raise Rejected("scope_violation", "Merged candidate changes the control mechanism")
        self.ordinary_files(candidate)
        tested_directory, tested_commit, scope = directory, candidate, "merged_candidate"
        if self.state["mode"] == "pr_only":
            tested_directory = self.directory / ("source-check-" + str(number))
            self.git("worktree", "add", "--quiet", "--detach", str(tested_directory), source)
            tested_commit, scope = source, "source_only_negative_control"
        validation = self.run_validation(tested_directory, tested_commit, scope)
        attempt.update(validation=validation, finished_at=now(), decision="prepared" if validation["passed"] else "rejected")
        self.result["validation"] = validation
        if not validation["passed"]:
            raise Rejected("validation_failed", "Trusted acceptance checks rejected the tested snapshot")
        self.state["prepared"] = {"source": source, "base": base, "candidate": candidate,
                                  "tree": tree, "attempt": number, "directory": str(directory)}
        self.result.update(decision="prepared", reason="checks_passed")
        self.save()
        return True

    def prepare(self, args):
        seconds = self.validate_inputs(args.base_branch, args.source_branch, args.mode, args.hold_seconds)
        self.state.update(base_branch=args.base_branch, source_branch=args.source_branch, mode=args.mode,
                          hold_seconds=seconds, repo=str(self.repo), trusted_head=self.text("rev-parse", "HEAD"),
                          validator_sha256=digest(self.oracle), gate_sha256=digest(__file__))
        self.result.update(base_branch=args.base_branch, source_branch=args.source_branch, mode=args.mode,
                           negative_control=args.mode == "pr_only", validator_sha256=self.state["validator_sha256"],
                           gate_sha256=self.state["gate_sha256"], trusted_head=self.state["trusted_head"],
                           max_attempts=MAX_ATTEMPTS)
        self.save()
        self.build(self.observe("prepare"), "initial_prepare")

    def accepted(self, reason):
        self.result.update(decision="accepted", reason=reason, published_candidate=self.state["prepared"]["candidate"])
        self.result["attempts"][-1]["decision"] = "accepted"
        try:
            observed = self.observe("after_publication")
            self.result.update(observed_remote_head=observed["base"], observed_source_head=observed["source"],
                               candidate_in_observed_history=self.ancestor(self.state["prepared"]["candidate"], observed["base"]),
                               source_advanced_after_validation=observed["source"] != self.state["prepared"]["source"])
        except (Rejected, subprocess.TimeoutExpired) as error:
            self.result["observation_error"] = self.clean(error)
        self.log("ACCEPTED: " + self.state["prepared"]["candidate"])
        self.save()

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
            if self.state["mode"] == "enforce" and self.result["validation"]["tested_commit"] != prepared["candidate"]:
                raise Rejected("untested_candidate", "Published SHA must equal the tested SHA")
            push = self.git("push", "--porcelain", "origin",
                            prepared["candidate"] + ":refs/heads/" + self.state["base_branch"], check=False)
            self.result["attempts"][-1]["push"] = {"returncode": push.returncode,
                                                    "stdout": self.clean(push.stdout), "stderr": self.clean(push.stderr)}
            self.save()
            if push.returncode == 0:
                self.accepted("published_tested_candidate" if self.state["mode"] == "enforce"
                              else "negative_control_published_merged_candidate_without_merged_validation")
                return
            latest = self.observe("after_rejected_push")
            if self.ancestor(prepared["candidate"], latest["base"]):
                self.accepted("accepted_after_ambiguous_push_response")
                return
            if len(self.result["attempts"]) >= MAX_ATTEMPTS:
                raise Rejected("push_rejected", "Ordinary push failed; retry limit reached without rewriting remote history")
            if not self.build(latest, "push_rejected_revalidate"):
                return

    def output(self):
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                stream.write("ready=" + str(self.result["decision"] == "prepared").lower() + "\n")
                stream.write("decision=" + self.result["decision"] + "\n")
        self.log("FINAL " + self.result["decision"].upper() + ": " + self.result["reason"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "publish"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--base-branch", default="")
    parser.add_argument("--source-branch", default="")
    parser.add_argument("--mode", default="enforce")
    parser.add_argument("--hold-seconds", default="0")
    args = parser.parse_args()
    gate = Gate(args.repo, args.state_dir)
    exit_code = 0
    try:
        if args.phase == "prepare":
            gate.prepare(args)
        else:
            gate.publish()
    except Rejected as error:
        gate.reject(error.reason, str(error))
    except Exception as error:
        gate.reject("internal_error", str(error))
        gate.log(traceback.format_exc())
        exit_code = 1
    gate.output()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
