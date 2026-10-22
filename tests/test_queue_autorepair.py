"""Real local Git histories, trusted contracts, deterministic repair doubles.

These prove gate control flow, not the quality of the real external model.
"""
import argparse
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from acceptance import REGISTRY_PATH
from queue_gate import Gate, Rejected
from queue_repair import RepairError
import test_queue_status as fixture


DISCOUNT = "def price_units(amount, discount_percent=0):\n    return round(float(amount)*100*(100-discount_percent)/100)\n"
FEE = "def price_units(amount, fee_kopeks=0):\n    return round(float(amount)*100)+fee_kopeks\n"
COMBINED = "def price_units(amount, discount_percent=0, fee_kopeks=0):\n    return round(float(amount)*100*(100-discount_percent)/100)+fee_kopeks\n"


class QueueAutorepairTests(unittest.TestCase):
    git = staticmethod(fixture.QueueStatusTests.git)
    remote_head = fixture.QueueStatusTests.remote_head

    def setUp(self):
        fixture.QueueStatusTests.setUp(self)
        self.env.update(QUEUE_AUTO_REPAIR="true", GITHUB_RUN_ATTEMPT="1")
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git(self.seed, "checkout", "feature/team")
        (self.seed / "money.py").write_text("def price_units(amount):\n    return round(float(amount)*100)\n")
        (self.seed / "receipt.py").write_text('from money import price_units\n\ndef format_total(amount):\n    return f"{price_units(amount)/100:.2f}"\n')
        self.git(self.seed, "add", "money.py", "receipt.py")
        self.git(self.seed, "commit", "-m", "test: цена в копейках")
        self.git(self.seed, "push", "origin", "feature/team")
        self.start = self.remote_head()

    def branch(self, name, base, files):
        self.git(self.seed, "fetch", "origin", base)
        self.git(self.seed, "checkout", "-b", name, base)
        for path, value in files.items():
            (self.seed / path).write_text(value, encoding="utf-8")
        self.git(self.seed, "add", "--", *files)
        self.git(self.seed, "commit", "-m", "test: независимая правка")
        self.git(self.seed, "push", "origin", name)
        return self.git(self.seed, "rev-parse", "HEAD")

    def gate(self, name, branch, requirement):
        gate = Gate(self.trusted, self.root / name)
        gate.args = argparse.Namespace(base_branch="feature/team", source_branch=branch,
                                       hold_seconds="0", requirement=requirement)
        return gate

    @staticmethod
    def proposal(text):
        return {"files": {"money.py": text}, "explanation": "Сохранены обе фичи", "metadata": {"mock": True}}

    def accept_discount(self):
        a = self.branch("feature/discount", self.start, {"money.py": DISCOUNT})
        gate = self.gate("discount", "feature/discount", "discount")
        gate.prepare(gate.args)
        gate.publish()
        self.assertEqual(gate.result["decision"], "accepted")
        return a

    def test_conflict_repaired_with_both_features_and_source_ancestry_dedup(self):
        self.accept_discount()
        source = self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("fee", "feature/fee", "fee")
        client = Mock()
        client.propose.return_value = self.proposal(COMBINED)
        with patch("queue_gate.RouterRepairClient.from_environment", return_value=client):
            gate.prepare(gate.args)
            gate.publish()
            repeated = self.gate("repeat", "feature/fee", "fee")
            repeated.prepare(repeated.args)
        self.assertEqual(client.propose.call_count, 1)
        self.assertEqual(gate.result["attempts"][0]["initial_rejection"], "merge_conflict")
        self.assertEqual(gate.read_requirements(self.remote_head()), ["discount", "fee"])
        self.assertTrue(gate.ancestor(source, self.remote_head()))
        self.assertEqual(self.git(self.seed, "ls-remote", "origin", "refs/heads/feature/fee").split()[0], source)
        self.assertEqual(repeated.result["decision"], "already_integrated")
        self.assertEqual(gate.result["published_candidate"], gate.result["validation"]["tested_commit"])
        self.assertIn("combined:0", [x["check"] for x in gate.result["validation"]["report"]["checks"]])

    def test_semantic_rejection_without_text_conflict_is_repaired(self):
        self.accept_discount()
        wrong = "def price_units(amount, discount_percent=0, fee_kopeks=0):\n    return round((float(amount)*100+fee_kopeks)*(100-discount_percent)/100)\n"
        self.branch("feature/fee", self.remote_head(), {"money.py": wrong})
        gate = self.gate("semantic", "feature/fee", "fee")
        client = Mock()
        client.propose.return_value = self.proposal(COMBINED)
        with patch("queue_gate.RouterRepairClient.from_environment", return_value=client):
            gate.prepare(gate.args)
            gate.publish()
        self.assertEqual(gate.result["attempts"][0]["initial_rejection"], "validation_failed")
        self.assertEqual(gate.result["decision"], "accepted")
        self.assertEqual(client.propose.call_count, 1)

    def test_budget_survives_publish_target_rebuild_and_never_calls_third_time(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("budget", "feature/fee", "fee")
        client = Mock()
        client.propose.side_effect = [self.proposal(COMBINED), self.proposal(DISCOUNT)]
        with patch("queue_gate.RouterRepairClient.from_environment", return_value=client):
            gate.prepare(gate.args)
            self.assertEqual(gate.state["repair_calls_reserved"], 1)
            moved = self.branch("feature/advance", self.remote_head(), {"feature_c.py": "ENABLED = True\n"})
            self.git(self.seed, "push", "origin", moved + ":refs/heads/feature/team")
            publishing = Gate(self.trusted, gate.directory)
            with self.assertRaises(Rejected) as rejected:
                publishing.publish()
            self.assertEqual(rejected.exception.reason, "repair_exhausted")
            publishing.reject(rejected.exception.reason)
        self.assertEqual(client.propose.call_count, 2)
        self.assertEqual(publishing.state["repair_calls_reserved"], 2)
        self.assertTrue(publishing.result["needs_human"])
        self.assertEqual(self.remote_head(), moved)

    def test_transport_and_bad_response_consume_two_reservations_before_call(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("errors", "feature/fee", "fee")
        old = self.remote_head()
        observed = []
        def fail(context):
            observed.append(json.loads(gate.state_path.read_text())["repair_calls_reserved"])
            raise RepairError("repair_transport_error", "Injected lost response")
        client = Mock()
        client.propose.side_effect = fail
        with patch("queue_gate.RouterRepairClient.from_environment", return_value=client):
            with self.assertRaises(Rejected) as error:
                gate.prepare(gate.args)
        self.assertEqual(error.exception.reason, "repair_exhausted")
        self.assertEqual(observed, [1, 2])
        self.assertEqual(self.remote_head(), old)

    def test_job_rerun_does_not_reset_budget(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("rerun", "feature/fee", "fee")
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}), patch("queue_gate.RouterRepairClient.from_environment") as client:
            with self.assertRaises(Rejected) as error:
                gate.prepare(gate.args)
        self.assertEqual(error.exception.reason, "repair_rerun_blocked")
        client.assert_not_called()

    def test_registry_is_strict_and_source_cannot_edit_requirements(self):
        gate = self.gate("registry", "feature/source", "baseline")
        for value in ('{}', '{"version":1,"accepted":["unknown"]}',
                      '{"version":1,"accepted":["fee","fee"]}', '{"version":true,"accepted":[]}'):
            with patch.object(gate, "git", return_value=argparse.Namespace(returncode=0, stdout=value)):
                with self.assertRaises(Rejected) as error:
                    gate.read_requirements(self.start)
                self.assertEqual(error.exception.reason, "invalid_requirements")
        with patch.object(gate, "git", return_value=argparse.Namespace(returncode=1, stdout="")):
            with self.assertRaises(Rejected):
                gate.read_requirements(self.start)
        self.branch("feature/ledger", self.start, {REGISTRY_PATH: '{"version":1,"accepted":["discount"]}\n'})
        gate = self.gate("tamper", "feature/ledger", "discount")
        with patch("queue_gate.RouterRepairClient.from_environment") as client:
            with self.assertRaises(Rejected) as error:
                gate.prepare(gate.args)
        self.assertEqual(error.exception.reason, "scope_violation")
        client.assert_not_called()

    def test_existing_source_without_recorded_contract_is_not_accepted(self):
        self.accept_discount()
        gate = self.gate("wrong-contract", "feature/discount", "fee")
        with self.assertRaises(Rejected) as error:
            gate.prepare(gate.args)
        self.assertEqual(error.exception.reason, "requirement_not_recorded")


if __name__ == "__main__":
    unittest.main()
