"""Temporary parameter measurement on recorded conflicts; never publishes or reserves queue budget.

Each call rebuilds the real gate repair context for a past rejected PR, sends it
with one fixed parameter variant and validates the proposal with the trusted
oracle, hidden checks included, in the same sandbox the queue uses. Only
metadata and outcomes are kept.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

import queue_repair
from queue_gate import ALLOWED, Gate, Rejected, digest
from queue_repair import RepairError, RouterRepairClient

SHARED_R2 = "3f65e5dca253b2f1ddb18af399a49223fb79cf27"
THREE_WAY = "af8d058ca92145d8af4c3828b7074d33f91a5dd1"  # Alice and Bob already merged into the function.
CASES = {
    "bob-r2": ("38ab165591c7b1bf18443493381a2cf2df04810a", SHARED_R2, "grouping-pattern"),
    "carol-r2": ("ce92e5b54ef8471c3dc7f13ee39e91d45d1a6a3d", SHARED_R2, "negative-parentheses"),
    "carol-3way": ("ce92e5b54ef8471c3dc7f13ee39e91d45d1a6a3d", THREE_WAY, "negative-parentheses"),
}
BASE = {"stream": False, "temperature": 0, "response_format": {"type": "json_object"}}
PINNED = {"provider": {"order": ["byteplus", "deepinfra"], "allow_fallbacks": True}}
VARIANTS = {
    "baseline-8k": (dict(BASE, max_tokens=8192), 90, 1),
    "default-low-16k": (*queue_repair.REPAIR_PROFILES[0], 4),
    "default-off-8k": (*queue_repair.REPAIR_PROFILES[1], 4),
    "pinned-low-16k": (dict(BASE, **PINNED, max_tokens=16384, reasoning={"effort": "low"}), 180, 4),
    "pinned-off-16k": (dict(BASE, **PINNED, max_tokens=16384, reasoning={"enabled": False}), 180, 4),
    "pinned-think-32k": (dict(BASE, **PINNED, max_tokens=32768), 300, 4),
}


def main():
    variant, case = sys.argv[1], sys.argv[2]
    parameters, timeout, repetitions = VARIANTS[variant]
    source, target, requirement = CASES[case]
    repo = Path(os.environ["GITHUB_WORKSPACE"])
    out = Path(os.environ["RUNNER_TEMP"]) / "probe"
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for repetition in range(1, repetitions + 1):
        gate = Gate(repo, tempfile.mkdtemp(prefix="probe-", dir=out))
        gate.state = {"base_branch": "feature/team-shared", "requirement": requirement,
                      "trusted_head": gate.text("rev-parse", "HEAD"), "validator_sha256": digest(gate.oracle),
                      "gate_sha256": digest(repo / "queue_gate.py"),
                      "repair_module_sha256": digest(repo / "queue_repair.py"),
                      "status_module_sha256": digest(repo / "queue_status.py")}
        gate.state["accepted_requirements"] = gate.read_requirements(target)
        gate.state["requirements"] = sorted(set(gate.state["accepted_requirements"]) | {requirement})
        directory = gate.directory / "candidate"
        gate.git("worktree", "add", "--quiet", "--detach", str(directory), target)
        merge = gate.git("-c", "merge.conflictStyle=zdiff3", "merge", "--no-ff", "--no-commit", source,
                         cwd=directory, check=False)
        common = gate.text("merge-base", target, source)
        context = gate.repair_context(directory, common, source, target, {"reason": "merge_conflict"})
        record = {"variant": variant, "case": case, "repetition": repetition, "parameters": parameters,
                  "timeout": timeout, "merge_conflict": merge.returncode != 0,
                  "context_bytes": len(json.dumps(context, ensure_ascii=False).encode()),
                  "shown_examples": len(context["new_requirement_examples"])}
        try:
            client = RouterRepairClient(os.environ.get("ROUTERAI_API_KEY"), ALLOWED, timeout)
            client.parameters = parameters
            proposal = client.propose(context)
            record.update(metadata=proposal["metadata"], explanation=proposal["explanation"][:400])
            gate.apply_proposal(directory, proposal)
            attempt = {}
            gate.candidate_commit(directory, source, target, attempt)
            record.update(outcome="validated", checks=len(attempt["validation"]["report"]["checks"]))
        except RepairError as error:
            record.update(outcome=error.reason, metadata=error.metadata)
        except Rejected as error:
            validation = gate.result.get("validation") or {}
            failed = [check["check"] for check in (validation.get("report") or {}).get("checks", [])
                      if not check.get("passed")]
            record.update(outcome=error.reason, failed_checks=failed[:40],
                          failed_hidden=sum(1 for name in failed if Gate.hidden(name)))
        records.append(record)
        metadata = record.get("metadata") or {}
        print(json.dumps({key: record.get(key) for key in ("variant", "case", "repetition", "outcome")}
                         | {"provider": metadata.get("provider"), "usage": metadata.get("usage"),
                            "shape": metadata.get("response_shape"), "elapsed": metadata.get("elapsed_seconds"),
                            "http": metadata.get("http_status")}, ensure_ascii=False), flush=True)
    (out / ("probe-" + variant + "-" + case + ".json")).write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
