"""Final integration agent on real Git histories, real Mergiraf when available and a scripted model.

These prove the agent's control flow and safety rules, not the quality of a real model.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fixbot
from fixbot import Bot, Stop, preserved_tests


SEED = Path(__file__).resolve().parents[2] / "fixbot-lab" / "seed"
MERGIRAF = Path(__file__).resolve().parents[2] / "github-ci-evidence" / "mergiraf-0.20.0" / "mergiraf.exe"
TARGET = "feature/lab-main"
PRICING = (SEED / "shop" / "pricing.py").read_text(encoding="utf-8") if SEED.exists() else ""

PROMO = PRICING.replace("def calculate_total(items, customer=None):", "def calculate_total(items, customer=None, promo_code=None):").replace(
    "    return subtotal(items)\n",
    "    total = subtotal(items)\n    if promo_code == 'SAVE10':\n        total -= total // 10\n    return total\n")
GOLD = PRICING.replace("    return subtotal(items)\n",
                       "    total = subtotal(items)\n    if customer is not None and customer.tier == 'gold':\n"
                       "        total -= total * 5 // 100\n    return total\n")
BOTH = PRICING.replace("def calculate_total(items, customer=None):", "def calculate_total(items, customer=None, promo_code=None):").replace(
    "    return subtotal(items)\n",
    "    total = subtotal(items)\n    discount = 0\n    if promo_code == 'SAVE10':\n        discount += total // 10\n"
    "    if customer is not None and customer.tier == 'gold':\n        discount += total * 5 // 100\n    return total - discount\n")
PROMO_TEST = ("\n\nclass PromoTests(unittest.TestCase):\n    def test_save10(self):\n"
              "        self.assertEqual(calculate_total([Item('a', 'Чай', 10000)], promo_code='SAVE10'), 9000)\n")
GOLD_TEST = ("\n\nclass GoldTests(unittest.TestCase):\n    def test_gold(self):\n"
             "        self.assertEqual(calculate_total([Item('a', 'Чай', 10000)], Customer('g', 'gold')), 9500)\n")


class FakeGitHub:
    repository = "fixture/repository"

    def __init__(self, pr):
        self.pr, self.statuses, self.comments, self.dispatches, self.open = pr, [], [], [], [pr]

    def request(self, method, path, payload=None):
        if method == "GET" and "/pulls?" in path:
            return list(self.open)
        if method == "GET":
            return self.pr
        if path.endswith("/comments"):
            self.comments.append(payload["body"])
        elif path.endswith("/dispatches"):
            self.dispatches.append(payload["inputs"])
        return None

    def send(self, sha, target, state, description):
        self.statuses.append((sha, state, description))
        return {}


class ScriptedModel:
    system_prompt = ""

    def __init__(self, answers):
        self.answers, self.contexts = list(answers), []

    def payload(self, context):
        return b"", set(context["allowed_paths"])

    def propose(self, context):
        self.contexts.append(context)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if not getattr(self, "unruly", False):
            answer = {path: text for path, text in answer.items() if path in context["allowed_paths"]}
        return {"files": answer, "explanation": "Совмещено", "metadata": {"request_sent": True}}


@unittest.skipUnless(SEED.exists(), "lab seed project is required")
class FixbotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixbot-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.git(self.root, "init", "--bare", "-b", "main", str(self.remote))
        self.seed = self.root / "seed"
        shutil.copytree(SEED, self.seed)
        self.git(self.seed, "init", "-b", TARGET)
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "seed")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "origin", TARGET)
        self.bot_repo = self.root / "bot"
        self.git(self.root, "clone", "-q", str(self.remote), str(self.bot_repo))
        env = {"FIXBOT_SANDBOX": "local", "FIXBOT_TARGET": TARGET}
        if MERGIRAF.exists():
            env["MERGIRAF_BINARY"] = str(MERGIRAF)
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def git(cwd, *args):
        result = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@localhost", *args], cwd=cwd,
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise AssertionError(result.stderr + result.stdout)
        return result.stdout.strip()

    def head(self, ref):
        return self.git(self.seed, "ls-remote", "origin", "refs/heads/" + ref).split()[0]

    def branch(self, name, start, files, message="feature"):
        self.git(self.seed, "fetch", "-q", "origin")
        self.git(self.seed, "checkout", "-q", "-B", name, start)
        for path, text in files.items():
            (self.seed / path).parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                (self.seed / path).unlink()
            else:
                (self.seed / path).write_text(text, encoding="utf-8", newline="\n")
        self.git(self.seed, "add", "-A")
        self.git(self.seed, "commit", "-m", message)
        self.git(self.seed, "push", "-q", "origin", name)
        return self.git(self.seed, "rev-parse", "HEAD")

    def merge_into_target(self, name):
        """A previously accepted feature: the shared branch fast-forwards to it."""
        self.git(self.seed, "push", "-q", "origin", "origin/" + name + ":refs/heads/" + TARGET)

    def run_bot(self, ref, sha, model=None):
        api = FakeGitHub({"number": 7, "state": "open", "draft": False, "title": "PR", "body": "feature",
                          "head": {"ref": ref, "sha": sha, "repo": {"full_name": FakeGitHub.repository}},
                          "base": {"ref": TARGET, "repo": {"full_name": FakeGitHub.repository}}})
        model = model or ScriptedModel([])
        bot = Bot(self.bot_repo, self.root / ("state-" + sha[:8]), api=api,
                  client_factory=lambda paths, profile: model)
        try:
            bot.fix(7, sha)
        except Stop as stop:
            bot.finish(stop)
        return bot.result, api, model

    def pricing_test(self, extra):
        return (SEED / "tests" / "test_pricing.py").read_text(encoding="utf-8") + extra

    # -- scenarios ------------------------------------------------------------------------------
    def test_independent_features_merge_without_model(self):
        start = self.head(TARGET)
        self.branch("feature/a", start, {"shop/promo.py": "CODES = {'SAVE10': 10}\n"})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/gift.py": "WRAP_CENTS = 9900\n"})
        result, api, model = self.run_bot("feature/b", sha)
        self.assertEqual((result["decision"], result["repairs"]), ("merged", []))
        self.assertEqual(self.head(TARGET), result["published"])
        self.assertEqual(self.head("feature/b"), result["published"])  # Fix lives in the PR branch.
        self.git(self.seed, "fetch", "-q", "origin")
        self.git(self.seed, "merge-base", "--is-ancestor", sha, "origin/" + TARGET)
        self.assertTrue(any(state == "success" for _, state, _ in api.statuses))

    @unittest.skipUnless(MERGIRAF.exists(), "Mergiraf binary is required")
    def test_both_sides_add_tests_to_one_file_mergiraf_or_union_keeps_every_test(self):
        start = self.head(TARGET)
        self.branch("feature/a", start, {"shop/pricing.py": PROMO, "tests/test_pricing.py": self.pricing_test(PROMO_TEST)})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/pricing.py": GOLD, "tests/test_pricing.py": self.pricing_test(GOLD_TEST)})
        both_tests = self.pricing_test(PROMO_TEST + GOLD_TEST)
        model = ScriptedModel([{"shop/pricing.py": BOTH, "tests/test_pricing.py": both_tests},
                               {"shop/pricing.py": BOTH, "tests/test_pricing.py": both_tests}])
        result, api, model = self.run_bot("feature/b", sha, model)
        self.assertEqual(result["decision"], "merged", result.get("detail"))
        merge_step = next(step for step in result["steps"] if step["step"] == "merge")
        self.assertNotIn("tests/test_pricing.py", merge_step["conflicts"])  # Mergiraf merged both test classes.
        self.git(self.seed, "fetch", "-q", "origin")
        merged_tests = self.git(self.seed, "show", "origin/" + TARGET + ":tests/test_pricing.py")
        self.assertIn("test_save10", merged_tests)
        self.assertIn("test_gold", merged_tests)
        for context in model.contexts:
            self.assertNotIn("tests/test_receipt.py", context["allowed_paths"])

    def test_same_function_conflict_is_repaired_by_model_and_both_features_pass(self):
        start = self.head(TARGET)
        self.branch("feature/a", start, {"shop/pricing.py": PROMO, "tests/test_promo.py":
                                         "import unittest\nfrom shop.models import Item\nfrom shop.pricing import calculate_total\n" + PROMO_TEST})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/pricing.py": GOLD, "tests/test_gold.py":
                                               "import unittest\nfrom shop.models import Customer, Item\nfrom shop.pricing import calculate_total\n" + GOLD_TEST})
        result, api, model = self.run_bot("feature/b", sha, ScriptedModel([{"shop/pricing.py": BOTH}]))
        self.assertEqual(result["decision"], "merged", result.get("detail"))
        self.assertEqual(len(result["repairs"]), 1)
        self.assertEqual(model.contexts[0]["allowed_paths"], ["shop/pricing.py"])
        self.assertIn("<<<<<<<", model.contexts[0]["current_merge"]["shop/pricing.py"])
        self.assertIn("|||||||", model.contexts[0]["current_merge"]["shop/pricing.py"])

    def test_dropping_the_other_feature_is_caught_and_second_attempt_fixes_it(self):
        start = self.head(TARGET)
        self.branch("feature/a", start, {"shop/pricing.py": PROMO, "tests/test_promo.py":
                                         "import unittest\nfrom shop.models import Item\nfrom shop.pricing import calculate_total\n" + PROMO_TEST})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/pricing.py": GOLD, "tests/test_gold.py":
                                               "import unittest\nfrom shop.models import Customer, Item\nfrom shop.pricing import calculate_total\n" + GOLD_TEST})
        result, api, model = self.run_bot("feature/b", sha, ScriptedModel([{"shop/pricing.py": GOLD}, {"shop/pricing.py": BOTH}]))
        self.assertEqual(result["decision"], "merged", result.get("detail"))
        self.assertEqual(len(result["repairs"]), 2)
        self.assertIn("tests.test_promo.PromoTests.test_save10", model.contexts[1]["failure"]["tests"]["failed"])

    def test_contradictory_requirements_go_to_a_human_with_both_sides_named(self):
        start = self.head(TARGET)
        minimum = PRICING.replace("    return subtotal(items)\n",
                                  "    total = subtotal(items)\n    if total < 50000:\n        raise ValueError('минимум')\n    return total\n")
        gift = PRICING.replace("        raise ValueError(\"пустой заказ\")\n", "        return 0\n")
        self.branch("feature/a", start, {"shop/pricing.py": minimum, "tests/test_minimum.py":
                                         "import unittest\nfrom shop.models import Item\nfrom shop.pricing import calculate_total\n\n\n"
                                         "class Minimum(unittest.TestCase):\n    def test_small_order_rejected(self):\n"
                                         "        with self.assertRaises(ValueError):\n            calculate_total([Item('a', 'Чай', 100)])\n"})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/pricing.py": gift.replace("    return subtotal(items)\n", "    return subtotal(items)\n"),
                                               "tests/test_gift.py":
                                               "import unittest\nfrom shop.models import Item\nfrom shop.pricing import calculate_total\n\n\n"
                                               "class Gift(unittest.TestCase):\n    def test_small_gift_order_allowed(self):\n"
                                               "        self.assertEqual(calculate_total([Item('g', 'Открытка', 100)]), 100)\n"})
        from queue_repair import RepairError
        result, api, model = self.run_bot("feature/b", sha, ScriptedModel([
            {"shop/pricing.py": minimum}, RepairError("repair_no_changes", "contradiction")]))
        self.assertEqual((result["decision"], result["needs_human"]), ("rejected", True))
        self.assertEqual(result["reason"], "conflicting_requirements")
        self.assertEqual(self.head(TARGET), self.head("feature/a"))  # Nothing published.
        self.assertTrue(api.comments and "противоречие требований" in api.comments[-1])
        self.assertIn("test_small_gift_order_allowed", api.comments[-1])

    def test_model_may_not_edit_a_test_assertion(self):
        start = self.head(TARGET)
        self.branch("feature/a", start, {"shop/pricing.py": PROMO, "tests/test_pricing.py": self.pricing_test(PROMO_TEST)})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/pricing.py": GOLD, "tests/test_pricing.py": self.pricing_test(GOLD_TEST)})
        weakened = self.pricing_test(PROMO_TEST.replace("9000", "10000") + GOLD_TEST)
        with patch.dict(os.environ, {"MERGIRAF_BINARY": ""}):  # Plain git: the test file conflicts.
            result, api, model = self.run_bot("feature/b", sha, ScriptedModel([
                {"shop/pricing.py": GOLD, "tests/test_pricing.py": weakened},
                {"shop/pricing.py": GOLD, "tests/test_pricing.py": weakened}]))
        self.assertEqual((result["decision"], result["needs_human"]), ("rejected", True))
        altered = result["escalation"]["failure"]["tests"]["altered_tests"]
        self.assertTrue(any("PromoTests.test_save10" in item for item in altered))

    def test_deleting_another_features_test_needs_a_human(self):
        start = self.head(TARGET)
        sha = self.branch("feature/b", start, {"tests/test_receipt.py": None, "shop/gift.py": "X = 1\n"})
        result, api, model = self.run_bot("feature/b", sha)
        self.assertEqual((result["decision"], result["needs_human"]), ("rejected", True))
        self.assertEqual(model.contexts, [])

    def test_model_answer_outside_allowed_files_is_rejected(self):
        start = self.head(TARGET)
        self.branch("feature/a", start, {"shop/pricing.py": PROMO})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/pricing.py": GOLD})
        model = ScriptedModel([{"shop/pricing.py": BOTH, "tests/test_receipt.py": "import unittest" + chr(10)}])
        model.unruly = True
        result, api, model = self.run_bot("feature/b", sha, model)
        self.assertEqual((result["decision"], result["reason"]), ("rejected", "repair_scope_violation"))
        self.assertEqual(self.head(TARGET), self.head("feature/a"))

    def test_protected_paths_are_never_merged(self):
        sha = self.branch("feature/b", self.head(TARGET), {".github/workflows/evil.yml": "on: push\n"})
        result, api, model = self.run_bot("feature/b", sha)
        self.assertEqual((result["decision"], result["reason"]), ("rejected", "protected_paths"))

    def test_author_push_during_run_is_never_overwritten(self):
        start = self.head(TARGET)
        self.branch("feature/a", start, {"shop/pricing.py": PROMO})
        self.merge_into_target("feature/a")
        sha = self.branch("feature/b", start, {"shop/pricing.py": GOLD})

        class Racing(ScriptedModel):
            def propose(inner, context):
                answer = ScriptedModel.propose(inner, context)
                self.branch("feature/b", sha, {"shop/note.py": "Y = 2\n"}, "author keeps working")
                return answer
        newer = None
        result, api, model = self.run_bot("feature/b", sha, Racing([{"shop/pricing.py": BOTH}]))
        newer = self.head("feature/b")
        self.assertEqual((result["decision"], result["reason"]), ("skipped", "superseded"))
        self.assertNotEqual(newer, sha)
        self.assertEqual(self.git(self.seed, "log", "-1", "--format=%s", newer), "author keeps working")
        self.assertEqual(self.head(TARGET), self.head("feature/a"))

    def test_preserved_tests_rule(self):
        base = "def test_a():\n    assert 1\n"
        self.assertEqual(preserved_tests(base, base + "def test_b():\n    assert 2\n", base + "def test_c():\n    assert 3\n",
                                         base + "def test_b():\n    assert 2\n\ndef test_c():\n    assert 3\n"), [])
        self.assertTrue(preserved_tests(base, base, "def test_a():\n    assert 0\n", base))
        self.assertTrue(preserved_tests(base, "", base, ""))  # Deleting another side's test is not silent.


if __name__ == "__main__":
    unittest.main()
