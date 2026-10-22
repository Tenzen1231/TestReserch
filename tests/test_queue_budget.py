"""The budget contract against real local bare Git repositories, without HTTP."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from queue_budget import BudgetError, LEDGER_PATH, LEDGER_REF, MAX_LEDGER_BYTES, reservation_key, reserve


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="council-budget-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.raw(self.root, "init", "--bare", str(self.remote))
        self.raw(self.repo, "init", "-b", "main")
        (self.repo / LEDGER_PATH).write_text('{"version":1,"reservations":{}}\n', encoding="utf-8")
        (self.repo / "unrelated.txt").write_text("preserved\n", encoding="utf-8")
        self.raw(self.repo, "add", ".")
        self.raw(self.repo, "commit", "-m", "test: создан журнал")
        self.raw(self.repo, "remote", "add", "origin", str(self.remote))
        self.raw(self.repo, "push", "origin", "HEAD:" + LEDGER_REF)
        self.initial_head = self.raw(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.initial_index = (self.repo / ".git/index").read_bytes()
        self.initial_file = (self.repo / LEDGER_PATH).read_bytes()
        self.pushes = []
        self.fetches = []
        self.before_push = None
        self.after_push = None
        self.after_fetch = None

    def raw(self, cwd, *args, check=True):
        result = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
                                 "-c", "user.name=Budget tests", "-c", "user.email=budget@localhost", *args],
                                cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30, env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never"))
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def git(self, *args, cwd=None, check=True):
        if args[0] == "push":
            self.pushes.append(args)
            if self.before_push:
                hook, self.before_push = self.before_push, None
                hook()
        if args[0] == "fetch":
            self.fetches.append(args)
        result = self.raw(cwd or self.repo, *args, check=check)
        if args[0] == "push" and self.after_push:
            hook, self.after_push = self.after_push, None
            return hook(result)
        if args[0] == "fetch" and self.after_fetch:
            hook, self.after_fetch = self.after_fetch, None
            return hook(result)
        return result

    def request(self, source="a" * 40, run="100", attempt=1, git=None):
        return reserve(git or self.git, "Example/Training", "feature/team-shared", source, run, attempt)

    def ledger(self):
        result = self.raw(self.remote, "show", LEDGER_REF + ":" + LEDGER_PATH)
        return json.loads(result.stdout)

    def assert_checkout_unchanged(self):
        self.assertEqual(self.initial_head, self.raw(self.repo, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(self.initial_index, (self.repo / ".git/index").read_bytes())
        self.assertEqual(self.initial_file, (self.repo / LEDGER_PATH).read_bytes())
        self.assertEqual("", self.raw(self.repo, "for-each-ref", "--format=%(refname)", "refs/council-budget").stdout)
        self.assertEqual(1, self.raw(self.repo, "worktree", "list", "--porcelain").stdout.count("worktree "))

    def replace_remote_file(self, content):
        # Ordinary forward publication simulates a corrupt/oversized administrator
        # edit; neither the test nor the implementation rewrites remote history.
        editor = self.root / "editor"
        self.raw(self.repo, "worktree", "add", "--detach", str(editor), self.initial_head)
        (editor / LEDGER_PATH).write_bytes(content)
        self.raw(editor, "add", LEDGER_PATH)
        self.raw(editor, "commit", "-m", "test: изменён журнал")
        self.raw(editor, "push", "origin", "HEAD:" + LEDGER_REF)
        self.raw(self.repo, "worktree", "remove", str(editor))

    def test_two_slots_survive_new_runs_and_repo_case(self):
        first = self.request()
        second = reserve(self.git, "EXAMPLE/training", "feature/team-shared", "a" * 40, "200", 7)
        self.assertEqual((1, 2), (first["ordinal"], second["ordinal"]))
        self.assertEqual(first["key"], second["key"])
        self.assertNotEqual(first["reservation_id"], second["reservation_id"])
        with self.assertRaises(BudgetError) as caught:
            self.request(run="300", attempt=9)
        self.assertEqual("budget_exhausted", caught.exception.reason)
        self.assertEqual(2, caught.exception.metadata["consumed"])
        rows = self.ledger()["reservations"][first["key"]]
        self.assertEqual(["100", "200"], [row["run_id"] for row in rows])
        self.assertEqual(2, len(self.pushes))
        self.assert_checkout_unchanged()

    def test_independent_source_has_new_budget_and_preserves_first(self):
        first = self.request()
        second = self.request(source="b" * 40)
        self.assertEqual((1, 1), (first["ordinal"], second["ordinal"]))
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(2, len(self.ledger()["reservations"]))
        self.assertEqual("preserved\n", self.raw(self.remote, "show", LEDGER_REF + ":unrelated.txt").stdout)
        self.assert_checkout_unchanged()

    def test_real_push_collision_refetches_and_preserves_other_writer(self):
        winner = []
        self.before_push = lambda: winner.append(self.request(source="b" * 40, run="101"))
        result = self.request()
        self.assertEqual(2, result["push_attempts"])
        self.assertEqual(3, len(self.pushes))
        self.assertEqual(2, len(self.ledger()["reservations"]))
        self.assertEqual(winner[0]["reservation_id"], self.ledger()["reservations"][winner[0]["key"]][0]["id"])
        refs = [call[-1].split(":", 1)[1] for call in self.fetches]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertTrue(all(not argument.startswith("+") and argument not in ("-f", "--force", "--force-with-lease")
                            for call in self.pushes for argument in call))
        self.assert_checkout_unchanged()

    def test_same_source_collision_uses_remaining_slot(self):
        self.before_push = lambda: self.request(run="101")
        result = self.request()
        self.assertEqual(2, result["ordinal"])
        self.assertEqual(2, result["push_attempts"])
        with self.assertRaises(BudgetError) as caught:
            self.request(run="102")
        self.assertEqual("budget_exhausted", caught.exception.reason)
        self.assert_checkout_unchanged()

    def test_collision_for_last_slot_cannot_create_a_third(self):
        first = self.request()
        self.before_push = lambda: self.request(run="101")
        with self.assertRaises(BudgetError) as caught:
            self.request(run="102")
        self.assertEqual("budget_exhausted", caught.exception.reason)
        self.assertEqual(1, caught.exception.metadata["push_attempts"])
        self.assertEqual(2, len(self.ledger()["reservations"][first["key"]]))
        self.assertEqual(3, len(self.pushes))
        self.assert_checkout_unchanged()

    def test_successful_push_with_lost_response_burns_slot(self):
        def lose_response(result):
            self.assertEqual(0, result.returncode)
            raise subprocess.TimeoutExpired(["git", "push"], 30, stderr="fake-sensitive-text")
        self.after_push = lose_response
        with self.assertRaises(BudgetError) as caught:
            self.request()
        self.assertEqual("budget_ambiguous", caught.exception.reason)
        self.assertNotIn("fake-sensitive-text", str(caught.exception))
        self.assertEqual(1, len(self.pushes))
        self.assertEqual(1, len(next(iter(self.ledger()["reservations"].values()))))
        second = self.request(run="100", attempt=2)
        self.assertEqual(2, second["ordinal"])
        with self.assertRaises(BudgetError):
            self.request(run="300")
        self.assert_checkout_unchanged()

    def test_successful_push_still_requires_readback(self):
        def fail_readback(result):
            self.assertEqual(0, result.returncode)
            self.after_fetch = lambda _: subprocess.CompletedProcess(["git", "fetch"], 1, "", "unavailable")
            return result
        self.after_push = fail_readback
        with self.assertRaises(BudgetError) as caught:
            self.request()
        self.assertEqual("budget_unavailable", caught.exception.reason)
        self.assertEqual(1, len(self.pushes))
        self.assertEqual(2, self.request(run="200")["ordinal"])
        self.assert_checkout_unchanged()

    def test_missing_ledger_is_not_initialized(self):
        empty = self.root / "empty.git"
        self.raw(self.root, "init", "--bare", str(empty))
        self.raw(self.repo, "remote", "set-url", "origin", str(empty))
        with self.assertRaises(BudgetError) as caught:
            self.request()
        self.assertEqual("budget_unavailable", caught.exception.reason)
        self.assertEqual([], self.pushes)
        self.assertEqual("", self.raw(empty, "show-ref", check=False).stdout)
        self.assert_checkout_unchanged()

    def test_corrupt_duplicate_json_fails_closed(self):
        self.replace_remote_file(b'{"version":1,"version":1,"reservations":{}}')
        with self.assertRaises(BudgetError) as caught:
            self.request()
        self.assertEqual("budget_corrupt", caught.exception.reason)
        self.assertEqual([], self.pushes)
        self.assert_checkout_unchanged()

    def test_oversized_ledger_fails_before_loading_content(self):
        self.replace_remote_file(b" " * (MAX_LEDGER_BYTES + 1))
        with self.assertRaises(BudgetError) as caught:
            self.request()
        self.assertEqual("budget_oversize", caught.exception.reason)
        self.assertEqual([], self.pushes)
        self.assert_checkout_unchanged()

    def test_invalid_identity_has_no_git_effect(self):
        with self.assertRaises(BudgetError) as caught:
            self.request(source="not-a-sha")
        self.assertEqual("budget_invalid_input", caught.exception.reason)
        self.assertEqual([], self.fetches)
        self.assertEqual([], self.pushes)
        self.assert_checkout_unchanged()


if __name__ == "__main__":
    unittest.main()
