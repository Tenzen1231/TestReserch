"""PR metadata cannot select arbitrary code, stale versions or new requirements."""
import copy
import unittest
from unittest.mock import Mock

from queue_pr import PRError, TARGET, dispatch, guard, requirement_from_body, validate_pr


REPOSITORY = "fixture/repository"
SHA = "a" * 40


def pr():
    return {"number": 7, "state": "open", "draft": False, "body": "Queue-Requirement: prefix\n",
            "head": {"ref": "feature/alice", "sha": SHA, "repo": {"full_name": REPOSITORY}},
            "base": {"ref": TARGET, "repo": {"full_name": REPOSITORY}}}


class PRTests(unittest.TestCase):
    def test_only_one_known_literal_requirement(self):
        self.assertEqual(requirement_from_body("Описание\nQueue-Requirement: prefix\n"), "prefix")
        for body in (None, "", "Queue-Requirement: unknown", "Queue-Requirement: $(bad)",
                     "Queue-Requirement: prefix\nQueue-Requirement: suffix", "Queue-Requirement:prefix"):
            with self.subTest(body=body), self.assertRaises(PRError):
                requirement_from_body(body)

    def test_guard_rejects_changed_sha_contract_closed_draft_fork_or_target(self):
        original = pr()
        for mutation in (
                lambda p: p.update(state="closed"), lambda p: p.update(draft=True),
                lambda p: p.update(body="Queue-Requirement: suffix"),
                lambda p: p["head"].update(sha="b" * 40),
                lambda p: p["head"]["repo"].update(full_name="outsider/repository"),
                lambda p: p["base"].update(ref="main"),
                lambda p: p["head"].update(ref="feature/queue-state")):
            changed = copy.deepcopy(original)
            mutation(changed)
            api = Mock(repository=REPOSITORY)
            api.request.return_value = changed
            with self.assertRaises(PRError):
                guard(api, validate_pr(original, REPOSITORY, 7))

    def test_synchronize_dispatches_exact_version_to_main_with_trace(self):
        item = pr()
        api = Mock(repository=REPOSITORY)
        api.request.side_effect = [item, None]
        result = dispatch({"action": "synchronize", "number": 7, "pull_request": item}, api, "123")
        self.assertEqual(result["decision"], "dispatched")
        post = api.request.call_args_list[1]
        self.assertEqual(post.args[0], "POST")
        self.assertEqual(post.args[2]["ref"], "main")
        self.assertEqual(post.args[2]["inputs"]["expected_head"], SHA)
        self.assertEqual(post.args[2]["inputs"]["dispatcher_run_id"], "123")

    def test_stale_event_never_dispatches_new_head(self):
        old, new = pr(), pr()
        new["head"]["sha"] = "b" * 40
        api = Mock(repository=REPOSITORY)
        api.request.return_value = new
        with self.assertRaises(PRError) as error:
            dispatch({"action": "synchronize", "number": 7, "pull_request": old}, api, "123")
        self.assertEqual(error.exception.reason, "pr_superseded")
        self.assertEqual(api.request.call_count, 1)

    def test_draft_closed_event_and_other_target_do_not_dispatch(self):
        for change, action in (({"draft": True}, "synchronize"), ({}, "closed")):
            item = pr()
            item.update(change)
            api = Mock(repository=REPOSITORY)
            self.assertEqual(dispatch({"action": action, "pull_request": item}, api, "123")["decision"], "ignored")
            api.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
