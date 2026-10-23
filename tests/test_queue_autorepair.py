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
from queue_structural import StructuralError
from queue_budget import BudgetError
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

    def test_pending_status_outage_neither_burns_reservation_nor_blocks_repair(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("status-outage", "feature/fee", "fee")
        self.api.fail_states.add("pending")
        order = []
        model = Mock()
        def propose(context):
            order.append("post")
            self.assertIn("combined:0", [item["check"] for item in context["acceptance_examples"]])
            return dict(self.proposal(COMBINED), metadata={"request_sent": True, "response_received": True})
        model.propose.side_effect = propose
        def reserve(*args, **kwargs):
            order.append("reserve")
            return {"ordinal": 1, "ledger_commit": "1" * 40}
        with patch.dict(os.environ, QUEUE_DURABLE_BUDGET="true"), patch("queue_status.RETRY_DELAYS", (0, 0)),                 patch("queue_gate.reserve_budget", side_effect=reserve),                 patch("queue_gate.RouterRepairClient.from_environment", return_value=model):
            gate.prepare(gate.args)
            gate.publish()
        self.assertEqual(order, ["reserve", "post"])
        self.assertEqual(gate.result["decision"], "accepted")
        self.assertTrue(any("DeepSeek" in item["description"] for item in gate.result["status_errors"]))
        gate.output()
        self.assertEqual(gate.result["repair_accounting"], {"reserved": 1, "request_sent": 1, "response_received": 1,
                                                            "proposed": 1, "validated": 1, "published": 1})

    def test_reservation_without_request_is_not_counted_as_a_model_call(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("not-sent", "feature/fee", "fee")
        with patch("queue_gate.RouterRepairClient.from_environment",
                   side_effect=RepairError("repair_missing_credentials", "No key")):
            with self.assertRaises(Rejected) as error:
                gate.prepare(gate.args)
        self.assertEqual(error.exception.reason, "repair_exhausted")
        gate.reject(error.exception.reason)
        gate.output()
        self.assertEqual(gate.result["repair_accounting"]["reserved"], 2)
        self.assertEqual(gate.result["repair_accounting"]["request_sent"], 0)

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

    def test_structural_resolution_is_validated_before_ai_and_can_avoid_ai(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("structural", "feature/fee", "fee")
        structural = Mock()
        structural.propose.return_value = self.proposal(COMBINED)
        with patch.dict(os.environ, QUEUE_STRUCTURAL_REPAIR="true"), \
                patch("queue_gate.StructuralRepairClient.from_environment", return_value=structural), \
                patch("queue_gate.RouterRepairClient.from_environment") as model:
            gate.prepare(gate.args)
            gate.publish()
        model.assert_not_called()
        self.assertEqual(gate.result["decision"], "accepted")
        self.assertEqual(gate.result["structural_repairs"][0]["state"], "validated")
        self.assertEqual(structural.propose.call_args.args[0]["conflict_paths"], ["money.py"])

    def test_structural_lost_feature_is_rejected_and_model_must_preserve_both(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("structural-loss", "feature/fee", "fee")
        structural, model = Mock(), Mock()
        structural.propose.return_value = self.proposal(DISCOUNT)
        model.propose.return_value = self.proposal(COMBINED)
        with patch.dict(os.environ, QUEUE_STRUCTURAL_REPAIR="true"), \
                patch("queue_gate.StructuralRepairClient.from_environment", return_value=structural), \
                patch("queue_gate.RouterRepairClient.from_environment", return_value=model):
            gate.prepare(gate.args)
            gate.publish()
        self.assertEqual(gate.result["structural_repairs"][0]["reason"], "validation_failed")
        self.assertEqual(gate.result["repairs"][0]["refusal"]["reason"], "validation_failed")
        self.assertEqual(gate.read_requirements(self.remote_head()), ["discount", "fee"])

    def test_unresolved_structural_conflict_falls_back_but_bad_binary_does_not(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        for reason in ("structural_unresolved", "structural_checksum_mismatch"):
            gate = self.gate(reason, "feature/fee", "fee")
            structural, model = Mock(), Mock()
            structural.propose.side_effect = StructuralError(reason, "Injected structural result")
            model.propose.return_value = self.proposal(COMBINED)
            with patch.dict(os.environ, QUEUE_STRUCTURAL_REPAIR="true"), \
                    patch("queue_gate.StructuralRepairClient.from_environment", return_value=structural), \
                    patch("queue_gate.RouterRepairClient.from_environment", return_value=model):
                if reason == "structural_unresolved":
                    gate.prepare(gate.args)
                    self.assertEqual(model.propose.call_count, 1)
                else:
                    with self.assertRaises(Rejected) as error:
                        gate.prepare(gate.args)
                    self.assertEqual(error.exception.reason, "structural_infrastructure_error")
                    model.assert_not_called()

    def test_github_request_cannot_publish_to_another_target(self):
        gate = self.gate("wrong-target", "feature/source", "baseline")
        with patch.dict(os.environ, GITHUB_ACTIONS="true", PR_NUMBER="7", EXPECTED_HEAD=self.source,
                        DISPATCHER_RUN_ID="123", PR_EVENT_ACTION="synchronize"):
            with self.assertRaises(Rejected) as error:
                gate.prepare(gate.args)
        self.assertEqual(error.exception.reason, "invalid_pr")

    def test_durable_budget_reservation_precedes_post_and_blocks_new_run(self):
        self.accept_discount()
        self.branch("feature/fee", self.start, {"money.py": FEE})
        gate = self.gate("durable-first", "feature/fee", "fee")
        model = Mock()
        def lost_response(context):
            self.assertTrue(gate.result["repairs"][-1]["budget_reservation"]["ledger_commit"])
            raise RepairError("repair_transport_error", "Lost response")
        model.propose.side_effect = lost_response
        reservations = [{"ordinal": i, "ledger_commit": str(i) * 40} for i in (1, 2)]
        with patch.dict(os.environ, QUEUE_DURABLE_BUDGET="true"), \
                patch("queue_gate.reserve_budget", side_effect=reservations) as ledger, \
                patch("queue_gate.RouterRepairClient.from_environment", return_value=model) as client:
            with self.assertRaises(Rejected) as error:
                gate.prepare(gate.args)
        self.assertEqual(error.exception.reason, "repair_exhausted")
        self.assertEqual(model.propose.call_count, 2)
        self.assertEqual(ledger.call_count, 2)
        self.assertEqual([call.kwargs["profile"] for call in client.call_args_list], [1, 2])
        self.assertEqual([item["profile"] for item in gate.result["repairs"]], [1, 2])
        repeated = self.gate("durable-new-run", "feature/fee", "fee")
        with patch.dict(os.environ, QUEUE_DURABLE_BUDGET="true", GITHUB_RUN_ID="999"), \
                patch("queue_gate.reserve_budget", side_effect=BudgetError("budget_exhausted", "Two slots used")), \
                patch("queue_gate.RouterRepairClient.from_environment") as no_model:
            with self.assertRaises(Rejected) as error:
                repeated.prepare(repeated.args)
        self.assertEqual(error.exception.reason, "repair_exhausted")
        self.assertEqual(repeated.result["repairs"], [])
        no_model.assert_not_called()

    def test_source_push_after_prepare_stops_old_request_before_publication(self):
        gate = self.gate("stale", "feature/source", "baseline")
        gate.prepare(gate.args)
        old = self.remote_head()
        gate.state["pr_request"] = {"expected_head": self.source}
        gate.save()
        from queue_pr import PRError
        with patch("queue_gate.guard_pr", side_effect=PRError("pr_superseded", "New push")), \
                patch("queue_gate.RouterRepairClient.from_environment") as model:
            with self.assertRaises(Rejected) as error:
                gate.publish()
        self.assertEqual(error.exception.reason, "pr_superseded")
        self.assertEqual(self.remote_head(), old)
        model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
