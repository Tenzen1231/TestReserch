"""Stranded PR versions return to the queue; settled ones and old evidence never do."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from queue_pr import TARGET
from queue_reconcile import MAX_RUNS_PER_SOURCE, reconcile


REPOSITORY = "fixture/repository"
NOW = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)


def stamp(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def pr(number, sha, updated=10):
    return {"number": number, "state": "open", "draft": False, "body": "Queue-Requirement: prefix\n",
            "updated_at": stamp(updated),
            "head": {"ref": "feature/w" + str(number), "sha": sha, "repo": {"full_name": REPOSITORY}},
            "base": {"ref": TARGET, "repo": {"full_name": REPOSITORY}}}


def run(item, status="completed"):
    return {"display_title": "PR #" + str(item["number"]) + " — " + item["head"]["ref"] + " — " + item["head"]["sha"],
            "status": status, "head_branch": "main", "conclusion": "failure" if status == "completed" else None}


class FakeGitHub:
    def __init__(self, pulls, runs, statuses):
        self.repository = REPOSITORY
        self.pulls = {item["number"]: item for item in pulls}
        self.runs = runs
        self.statuses = statuses
        self.dispatched = []

    def request(self, method, path, payload=None):
        if method == "POST":
            self.dispatched.append(payload["inputs"])
            return None
        if "/actions/workflows/queue.yml/runs" in path:
            return {"workflow_runs": self.runs}
        if "/pulls?" in path:
            return list(self.pulls.values())
        return self.pulls[int(path.rsplit("/", 1)[1])]

    def latest(self, sha, context):
        return self.statuses.get(sha)


class ReconcileTests(unittest.TestCase):
    def decisions(self, pulls, runs=(), statuses=None):
        api = FakeGitHub(pulls, list(runs), statuses or {})
        report = reconcile(api, "777", NOW)
        return {entry["pr_number"]: entry["decision"] for entry in report}, api.dispatched

    def test_lost_event_dead_run_and_infrastructure_error_are_requeued_once(self):
        lost, dead, broken = pr(50, "a" * 40), pr(51, "b" * 40), pr(52, "c" * 40)
        decisions, dispatched = self.decisions(
            [lost, dead, broken], [run(dead), run(broken)],
            {"b" * 40: {"state": "pending", "updated_at": stamp(20)},
             "c" * 40: {"state": "error", "updated_at": stamp(1)}})
        self.assertEqual(decisions, {50: "lost_event", 51: "stalled", 52: "infrastructure_error"})
        self.assertEqual([item["expected_head"] for item in dispatched], ["a" * 40, "b" * 40, "c" * 40])
        self.assertTrue(all(item["event_action"] == "reconcile" and item["dispatcher_run_id"] == "777"
                            for item in dispatched))

    def test_settled_active_fresh_and_historical_versions_are_left_alone(self):
        accepted, rejected, running, fresh, old = (pr(50, "a" * 40), pr(51, "b" * 40), pr(52, "c" * 40),
                                                   pr(53, "d" * 40, updated=1), pr(49, "e" * 40))
        decisions, dispatched = self.decisions(
            [accepted, rejected, running, fresh, old], [run(running, "queued")],
            {"a" * 40: {"state": "success", "updated_at": stamp(30)},
             "b" * 40: {"state": "failure", "updated_at": stamp(30)},
             "c" * 40: {"state": "error", "updated_at": stamp(30)}})
        self.assertEqual(decisions, {50: "settled", 51: "settled", 52: "active", 53: "grace"})
        self.assertEqual(dispatched, [])

    def test_repeated_infrastructure_failures_stop_at_the_run_limit(self):
        item = pr(60, "f" * 40)
        decisions, dispatched = self.decisions([item], [run(item)] * MAX_RUNS_PER_SOURCE,
                                               {"f" * 40: {"state": "error", "updated_at": stamp(30)}})
        self.assertEqual((decisions, dispatched), ({60: "run_limit"}, []))

    def test_race_rejections_are_retried_but_merit_rejections_are_final(self):
        from queue_gate import REASONS_RU
        from queue_reconcile import RACE_DESCRIPTIONS
        self.assertEqual(RACE_DESCRIPTIONS, tuple("Отклонено: " + REASONS_RU[key] for key in ("retry_limit", "push_rejected")))
        race, merit = pr(80, "6" * 40), pr(81, "7" * 40)
        decisions, dispatched = self.decisions([race, merit], [run(race), run(merit)], {
            "6" * 40: {"state": "failure", "updated_at": stamp(5), "description": RACE_DESCRIPTIONS[0]},
            "7" * 40: {"state": "failure", "updated_at": stamp(5),
                       "description": "Отклонено: " + REASONS_RU["repair_exhausted"]}})
        self.assertEqual(decisions, {80: "race", 81: "settled"})
        self.assertEqual([item["expected_head"] for item in dispatched], ["6" * 40])

    def test_foreign_or_skipped_runs_do_not_exhaust_the_limit(self):
        item = pr(62, "3" * 40)
        foreign = [dict(run(item), display_title="PR #62 — feature/other — " + "3" * 40)] * 4
        skipped = [dict(run(item), conclusion="skipped")] * 4
        branch = [dict(run(item), head_branch="feature/attacker")] * 4
        decisions, dispatched = self.decisions([item], foreign + skipped + branch,
                                               {"3" * 40: {"state": "error", "updated_at": stamp(30)}})
        self.assertEqual(decisions, {62: "infrastructure_error"})
        self.assertEqual(len(dispatched), 1)

    def test_one_pr_api_failure_does_not_starve_the_others(self):
        first, second = pr(70, "4" * 40), pr(71, "5" * 40)
        api = FakeGitHub([first, second], [], {})
        from queue_status import StatusError
        original = api.latest
        def latest(sha, context):
            if sha == "4" * 40:
                raise StatusError("GitHub status API returned HTTP 403")
            return original(sha, context)
        api.latest = latest
        report = reconcile(api, "777", NOW)
        self.assertEqual([entry["decision"] for entry in report], ["github_api_error", "lost_event"])
        self.assertEqual([item["expected_head"] for item in api.dispatched], ["5" * 40])

    def test_changed_head_or_requirement_is_not_dispatched_from_stale_listing(self):
        item = pr(61, "1" * 40)
        api = FakeGitHub([item], [], {})
        listing = dict(item, head=dict(item["head"], sha="2" * 40))
        api.request = Mock(side_effect=lambda method, path, payload=None: (
            {"workflow_runs": []} if "runs" in path else [listing] if "/pulls?" in path else item))
        report = reconcile(api, "777", NOW)
        self.assertEqual((report[0]["decision"], report[0]["submission_skipped"]), ("lost_event", "pr_superseded"))
        self.assertFalse(any(call.args[0] == "POST" for call in api.request.call_args_list))


if __name__ == "__main__":
    unittest.main()
