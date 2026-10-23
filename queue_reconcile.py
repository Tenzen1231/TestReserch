"""Return stranded PR versions to the trusted queue without a human.

A version is stranded when its event was lost, its queue run died without a
decision, or the decision was an infrastructure error. A rejection on the merits
stays final for that source SHA: only a new author commit is a new request, and
the durable ledger still bounds model calls per SHA however often it is queued.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from queue_pr import PRError, RECONCILE, TARGET, guard, submit, validate_pr
from queue_status import CommitStatuses, StatusError


FIRST_RECONCILED_PR = 50  # Earlier PRs are preserved evidence of past experiments.
MAX_RUNS_PER_SOURCE = 4
GRACE = timedelta(minutes=3)
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
RETRY = {"lost_event", "infrastructure_error", "stalled", "race"}
# Rejections caused by concurrent ref movement, not by the change itself; the
# gate's own wording, pinned by a test against queue_gate.REASONS_RU.
RACE_DESCRIPTIONS = ("Отклонено: ветки менялись во время повторных проверок",
                     "Отклонено: сервер не принял публикацию после повторных проверок")


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def runs_for(pr, runs):
    title = "PR #" + str(pr["number"]) + " — " + pr["head"]["ref"] + " — " + pr["head"]["sha"]
    # Only trusted main runs for this exact branch and SHA count; runs from other
    # refs are skipped jobs that must not exhaust another author's limit.
    return [run for run in runs if run.get("display_title") == title and run.get("head_branch") == "main"
            and run.get("conclusion") != "skipped"]


def decide(pr, status, runs, now):
    mine = runs_for(pr, runs)
    if any(run.get("status") in ACTIVE for run in mine):
        return "active"
    race = status is not None and status.get("state") == "failure" and status.get("description") in RACE_DESCRIPTIONS
    if status is not None and status.get("state") in ("success", "failure") and not race:
        return "settled"
    if len(mine) >= MAX_RUNS_PER_SOURCE:
        return "run_limit"
    if race:
        return "race"
    if status is None:
        return "lost_event" if now - moment(pr["updated_at"]) > GRACE else "grace"
    if status.get("state") == "error":
        return "infrastructure_error"
    if now - moment(status["updated_at"]) > GRACE:
        return "stalled"
    return "grace"


def paged(api, path, key=None):
    items = []
    for page in range(1, 6):
        value = api.request("GET", path + ("&" if "?" in path else "?") + "per_page=100&page=" + str(page))
        batch = value[key] if key else value
        if not isinstance(batch, list):
            raise StatusError("Invalid GitHub list response")
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


def reconcile(api, run_id, now=None):
    now = now or datetime.now(timezone.utc)
    runs = paged(api, "/repos/" + api.repository + "/actions/workflows/queue.yml/runs?event=workflow_dispatch",
                 "workflow_runs")
    pulls = paged(api, "/repos/" + api.repository + "/pulls?state=open&base=" + TARGET)
    report = []
    for pr in pulls:
        if type(pr.get("number")) is not int or pr["number"] < FIRST_RECONCILED_PR or pr.get("draft") is not False:
            continue
        entry = {"pr_number": pr["number"], "head": (pr.get("head") or {}).get("sha")}
        report.append(entry)
        try:
            listed = validate_pr(pr, api.repository, pr["number"])
        except PRError as error:
            entry.update(decision="not_a_queue_request", reason=error.reason)
            continue
        try:
            status = api.latest(pr["head"]["sha"], CommitStatuses.context(TARGET))
            entry.update(status=None if status is None else status.get("state"),
                         decision=decide(pr, status, runs, now))
            if entry["decision"] in RETRY:
                try:
                    # The listing may be stale: only the current exact PR version is submitted.
                    entry["submitted"] = submit(api, guard(api, listed), run_id, RECONCILE)
                except PRError as error:
                    entry["submission_skipped"] = error.reason
        except StatusError as error:
            # One PR's API failure must not starve the others; the next cycle retries it.
            entry.update(decision="github_api_error", error=str(error))
    return report


def main():
    api = CommitStatuses.from_environment()
    if api is None:
        raise SystemExit("GitHub API is required")
    report = reconcile(api, os.environ["GITHUB_RUN_ID"])
    destination = Path(os.environ["RUNNER_TEMP"], "reconcile.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write("## Возврат застрявших заявок\n\n")
        for entry in report:
            stream.write(f"- PR #{entry['pr_number']} `{str(entry.get('head'))[:8]}`: {entry.get('decision')}\n")
    return 1 if any(entry.get("decision") == "github_api_error" for entry in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
