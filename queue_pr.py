"""Metadata-only PR dispatcher and fresh PR guard for the trusted queue."""
import json
import os
from pathlib import Path
import re
import time

from acceptance import CONTRACTS
from queue_status import CommitStatuses, StatusError, MAX_REQUEST_ATTEMPTS, RETRY_DELAYS


TARGET = "feature/team-shared"
STATE_BRANCH = "feature/queue-state"
EVENTS = {"opened", "synchronize", "reopened", "ready_for_review", "edited"}


class PRError(RuntimeError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


def requirement_from_body(body):
    body = body or ""
    if not isinstance(body, str) or len(body) > 65536:
        raise PRError("invalid_pr_requirement", "PR body is unavailable or too large")
    markers = [line.strip() for line in body.splitlines()
               if line.strip().startswith("Queue-Requirement:")]
    if len(markers) != 1:
        raise PRError("invalid_pr_requirement", "Use exactly one Queue-Requirement: <id> line")
    match = re.fullmatch(r"Queue-Requirement: ([a-z][a-z-]*)", markers[0])
    if not match or match[1] not in {"baseline", *CONTRACTS}:
        raise PRError("invalid_pr_requirement", "Requirement is not in the trusted catalogue")
    return match[1]


def validate_pr(pr, repository, number, expected_head=None, source=None, requirement=None):
    if type(number) is not int or number < 1 or pr.get("number") != number:
        raise PRError("invalid_pr", "Invalid PR identity")
    if pr.get("state") != "open" or pr.get("draft") is not False:
        raise PRError("pr_not_ready", "Only an open, non-draft PR can enter the queue")
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if any((item.get("repo") or {}).get("full_name") != repository for item in (head, base)):
        raise PRError("invalid_pr", "Only same-repository PRs are supported")
    branch, sha = head.get("ref", ""), head.get("sha", "")
    if (base.get("ref") != TARGET or not re.fullmatch(r"feature/[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or branch in {TARGET, STATE_BRANCH} or ".." in branch
            or not re.fullmatch(r"[0-9a-f]{40}", sha)):
        raise PRError("invalid_pr", "Unexpected target or source reference")
    actual_requirement = requirement_from_body(pr.get("body"))
    if expected_head is not None and sha != expected_head:
        raise PRError("pr_superseded", "A newer push replaced this queued PR version")
    if source is not None and branch != source:
        raise PRError("pr_changed", "PR source branch changed")
    if requirement is not None and actual_requirement != requirement:
        raise PRError("pr_changed", "PR requirement changed; its new event must be checked")
    return {"pr_number": number, "source_branch": branch, "base_branch": TARGET,
            "expected_head": sha, "requirement": actual_requirement}


def read_pr(api, number):
    return api.request("GET", "/repos/" + api.repository + "/pulls/" + str(number))


def guard(api, request):
    return validate_pr(read_pr(api, request["pr_number"]), api.repository,
                       request["pr_number"], request["expected_head"],
                       request["source_branch"], request["requirement"])


def dispatch(event, api, run_id):
    """Event data never becomes code or shell text; current API data wins."""
    action = event.get("action")
    payload_pr = event.get("pull_request") or {}
    if action not in EVENTS or payload_pr.get("draft") is True:
        return {"decision": "ignored", "reason": "not_ready_event"}
    if (payload_pr.get("base") or {}).get("ref") != TARGET:
        return {"decision": "ignored", "reason": "different_target"}
    request = validate_pr(payload_pr, api.repository, event.get("number"))
    request = guard(api, request)
    inputs = {key: str(value) for key, value in request.items()}
    inputs.update(dispatcher_run_id=str(run_id), event_action=action, hold_seconds="0")
    # A repeated dispatch after a lost response is harmless: queue runs are
    # serialized, an integrated source is reported as already accepted and the
    # durable ledger bounds model calls per source SHA, not per run.
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            api.request("POST", "/repos/" + api.repository + "/actions/workflows/queue.yml/dispatches",
                        {"ref": "main", "inputs": inputs})
            break
        except StatusError as error:
            if not error.transient or attempt == MAX_REQUEST_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAYS[attempt - 1])
    return {"decision": "dispatched", "event_action": action, "dispatch_attempts": attempt,
            "dispatcher_run_id": str(run_id), **request}


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request_target":
        raise SystemExit("This dispatcher only accepts pull_request_target metadata")
    api = CommitStatuses.from_environment()
    if api is None:
        raise SystemExit("GitHub API is required")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    exit_code = 0
    try:
        result = dispatch(event, api, os.environ["GITHUB_RUN_ID"])
    except (PRError, StatusError) as error:
        result = {"decision": "rejected", "reason": getattr(error, "reason", "github_api_error"),
                  "detail": str(error), "event_action": event.get("action"), "pr_number": event.get("number")}
        exit_code = 1
    destination = Path(os.environ["RUNNER_TEMP"], "pr-dispatch.json")
    result["trusted_dispatcher_sha"] = os.environ.get("TRUSTED_DISPATCHER_SHA")
    result["event_head"] = ((event.get("pull_request") or {}).get("head") or {}).get("sha")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write("PR → очередь: " + result["decision"] + "\n\n")
        stream.write(result.get("detail", "Обрабатывается конкретная версия из события PR.") + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
