"""Temporary parameter measurement on recorded conflicts; never publishes or reserves queue budget.

Each call rebuilds the real gate repair context for a past rejected PR, sends it
with one fixed parameter variant and validates the proposal with the trusted
oracle in the same sandbox the queue uses. Only metadata and outcomes are kept.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

import queue_repair
from queue_gate import ALLOWED, Gate, Rejected, digest
from queue_repair import RepairError, RouterRepairClient

TARGET = "3f65e5dca253b2f1ddb18af399a49223fb79cf27"
CASES = {
    "bob-r2-grouping-pattern": ("38ab165591c7b1bf18443493381a2cf2df04810a", "grouping-pattern"),
    "carol-r2-negative-parentheses": ("ce92e5b54ef8471c3dc7f13ee39e91d45d1a6a3d", "negative-parentheses"),
}
BASE = {"stream": False, "temperature": 0, "response_format": {"type": "json_object"}}
VARIANTS = {
    "baseline-8k": (dict(BASE, max_tokens=8192), 90, 1),
    "queue-profile-1": (*queue_repair.REPAIR_PROFILES[0], 2),
    "queue-profile-2": (*queue_repair.REPAIR_PROFILES[1], 2),
    "effort-none-8k": (dict(BASE, max_tokens=8192, reasoning_effort="none"), 90, 2),
    "thinking-32k": (dict(BASE, max_tokens=32768), 300, 2),
}


def main():
    variant = sys.argv[1]
    parameters, timeout, repetitions = VARIANTS[variant]
    repo = Path(os.environ["GITHUB_WORKSPACE"])
    out = Path(os.environ["RUNNER_TEMP"]) / "probe"
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for case, (source, requirement) in CASES.items():
        for repetition in range(1, repetitions + 1):
            gate = Gate(repo, tempfile.mkdtemp(prefix="probe-", dir=out))
            gate.state = {"base_branch": "feature/team-shared", "requirement": requirement,
                          "trusted_head": gate.text("rev-parse", "HEAD"), "validator_sha256": digest(gate.oracle),
                          "gate_sha256": digest(repo / "queue_gate.py"),
                          "repair_module_sha256": digest(repo / "queue_repair.py"),
                          "status_module_sha256": digest(repo / "queue_status.py")}
            gate.state["requirements"] = sorted(set(gate.read_requirements(TARGET)) | {requirement})
            directory = gate.directory / "candidate"
            gate.git("worktree", "add", "--quiet", "--detach", str(directory), TARGET)
            merge = gate.git("merge", "--no-ff", "--no-commit", source, cwd=directory, check=False)
            common = gate.text("merge-base", TARGET, source)
            context = gate.repair_context(directory, common, source, TARGET, {"reason": "merge_conflict"})
            record = {"variant": variant, "case": case, "repetition": repetition, "parameters": parameters,
                      "timeout": timeout, "merge_conflict": merge.returncode != 0,
                      "context_bytes": len(json.dumps(context, ensure_ascii=False).encode())}
            try:
                client = RouterRepairClient(os.environ.get("ROUTERAI_API_KEY"), ALLOWED, timeout)
                client.parameters = parameters
                proposal = client.propose(context)
                record.update(metadata=proposal["metadata"], explanation=proposal["explanation"][:400])
                gate.apply_proposal(directory, proposal)
                attempt = {}
                gate.candidate_commit(directory, source, TARGET, attempt)
                record.update(outcome="validated", checks=len(attempt["validation"]["report"]["checks"]))
            except RepairError as error:
                record.update(outcome=error.reason, metadata=error.metadata)
            except Rejected as error:
                validation = gate.result.get("validation") or {}
                failed = [check["check"] for check in (validation.get("report") or {}).get("checks", [])
                          if not check.get("passed")]
                record.update(outcome=error.reason, failed_checks=failed[:40])
            records.append(record)
            print(json.dumps({key: record.get(key) for key in ("variant", "case", "repetition", "outcome")}
                             | {"usage": (record.get("metadata") or {}).get("usage"),
                                "shape": (record.get("metadata") or {}).get("response_shape"),
                                "elapsed": (record.get("metadata") or {}).get("elapsed_seconds")},
                             ensure_ascii=False), flush=True)
    (out / ("probe-" + variant + ".json")).write_text(json.dumps(records, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")


if __name__ == "__main__":
    main()
