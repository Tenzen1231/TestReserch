"""Boundary tests plus opt-in tests against the real pinned Mergiraf binary."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import queue_structural as structural
from queue_structural import StructuralError, StructuralRepairClient


IMPORTS = ("from math import floor\n\ndef price_units(amount):\n    return round(float(amount) * 100)\n",
           "from math import floor, ceil\n\ndef price_units(amount):\n    return round(float(amount) * 100)\n",
           "from math import floor, trunc\n\ndef price_units(amount):\n    return round(float(amount) * 100)\n")
FUNCTIONS = ("def first(): return 1\ndef second(): return 2\n",
             "def first(): return 10\ndef second(): return 2\n",
             "def first(): return 1\ndef second(): return 20\n")
FORMULA = ("def price_units(amount):\n    return round(float(amount) * 100)\n",
           "def price_units(amount, discount_percent=0):\n    return round(float(amount) * 100 * (100 - discount_percent) / 100)\n",
           "def price_units(amount, fee_kopeks=0):\n    return round(float(amount) * 100) + fee_kopeks\n")


def context(contents=IMPORTS, path="money.py"):
    result = {"conflict_paths": [path]}
    for side, content in zip(("common_ancestor", "current_target", "submission"), contents):
        result[side] = {path: content}
    return result


class StructuralBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="structural-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "fake-pinned-binary"
        self.binary.write_bytes(b"fixture bytes, never executed")
        self.environment = patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "QUEUE_STRUCTURAL_DOCKER": "false"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pins = patch.dict(structural.PINNED_BINARIES, {key: hashlib.sha256(self.binary.read_bytes()).hexdigest()
                                                         for key in structural.PINNED_BINARIES})
        self.pins.start()
        self.addCleanup(self.pins.stop)
        self.client = StructuralRepairClient(self.binary, use_docker=False)
        self.calls = []
        self.output = "from math import floor, ceil, trunc\n\ndef price_units(amount):\n    return round(float(amount) * 100)\n"
        self.merge_code = 0

    def fake_run(self, command, **options):
        self.calls.append((command, options))
        if len(command) > 1 and command[1] in ("kill", "rm"):
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, ("mergiraf " + structural.VERSION + "\n").encode(), b"")
        if "--entrypoint" in command:
            mount = next(item for item in command if item.endswith(",dst=/output"))
            destination = Path(mount.split("src=", 1)[1].rsplit(",dst=", 1)[0]) / "merged.py"
        else:
            destination = Path(command[command.index("--output") + 1])
        destination.write_text(self.output, encoding="utf-8", newline="\n")
        return subprocess.CompletedProcess(command, self.merge_code, b"", b"fixed diagnostic")

    def error(self, reason, value=None):
        with self.assertRaises(StructuralError) as caught:
            self.client.propose(context() if value is None else value)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.detail, str(caught.exception))
        return caught.exception

    def test_success_preserves_inputs_and_returns_only_approved_content_and_hashes(self):
        original = context()
        with patch.object(subprocess, "run", side_effect=self.fake_run):
            result = self.client.propose(original)
        self.assertEqual(result["files"], {"money.py": self.output})
        self.assertEqual(original, context())
        metadata = result["metadata"]
        self.assertEqual((metadata["stage"], metadata["tool"], metadata["version"]), ("structural", "mergiraf", "0.20.0"))
        self.assertEqual(metadata["conflict_paths"], ["money.py"])
        self.assertEqual(metadata["files"][0]["output_sha256"], hashlib.sha256(self.output.encode()).hexdigest())
        self.assertNotIn("fixed diagnostic", str(metadata))
        self.assertEqual(len(self.calls), 2)

    def test_scope_aliases_missing_base_and_oversized_text_reject_before_execution(self):
        with patch.object(subprocess, "run", side_effect=self.fake_run):
            for path in (".github/workflows/queue.yml", "./money.py", "../money.py", "MONEY.py", "money.py:stream", "other.py"):
                self.error("structural_invalid_paths", context(path=path))
            value = context()
            del value["common_ancestor"]["money.py"]
            self.error("structural_invalid_input", value)
            self.error("structural_input_too_large", context(("x" * (structural.MAX_INPUT_BYTES + 1), *IMPORTS[1:])))
            self.error("structural_invalid_input", context(("<<<<<<< HEAD\n", *IMPORTS[1:])))
        self.assertEqual(self.calls, [])

    def test_checksum_mismatch_and_version_mismatch_are_not_success(self):
        with patch.object(subprocess, "run", side_effect=self.fake_run):
            self.binary.write_bytes(b"changed fixture bytes")
            self.error("structural_checksum_mismatch")
        self.assertEqual(self.calls, [])
        self.binary.write_bytes(b"fixture bytes, never executed")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"mergiraf 9.9.9\n", b"")):
            self.error("structural_version_mismatch")

    def test_nonzero_exit_and_conflict_markers_are_not_proposals(self):
        with patch.object(subprocess, "run", side_effect=self.fake_run):
            self.merge_code = 1
            error = self.error("structural_unresolved")
            self.assertEqual(error.metadata["files"][0]["exit_code"], 1)
            self.merge_code = 0
            self.output = "<<<<<<< target\nvalue = 1\n=======\nvalue = 2\n>>>>>>> source\n"
            self.error("structural_invalid_output")
            self.output = "def broken(\n"
            self.error("structural_invalid_output")

    def test_multiple_conflicts_are_all_or_nothing(self):
        value = context()
        value["conflict_paths"].append("receipt.py")
        for side in ("common_ancestor", "current_target", "submission"):
            value[side]["receipt.py"] = value[side]["money.py"]
        def run(command, **options):
            result = self.fake_run(command, **options)
            if len(self.calls) == 3:
                return subprocess.CompletedProcess(command, 1, b"", b"")
            return result
        with patch.object(subprocess, "run", side_effect=run):
            error = self.error("structural_unresolved", value)
        self.assertEqual(len(error.metadata["files"]), 2)
        self.assertFalse(hasattr(error, "files"))

    def test_unexpected_or_oversized_output_is_rejected(self):
        def extra_file(command, **options):
            result = self.fake_run(command, **options)
            if "--output" in command:
                output = Path(command[command.index("--output") + 1])
                (output.parent / "unexpected.py").write_text("value = 1\n")
            return result
        with patch.object(subprocess, "run", side_effect=extra_file):
            self.error("structural_invalid_output")
        self.output = "#" + "x" * structural.MAX_OUTPUT_BYTES
        with patch.object(subprocess, "run", side_effect=self.fake_run):
            self.error("structural_output_too_large")

    def test_mutating_an_input_cannot_return_a_proposal(self):
        def mutate_input(command, **options):
            result = self.fake_run(command, **options)
            if "merge" in command:
                base = Path(command[2])
                base.chmod(0o644)
                base.write_text("changed = True\n")
            return result
        with patch.object(subprocess, "run", side_effect=mutate_input):
            self.error("structural_input_changed")

    def test_native_process_cannot_inherit_credentials_or_repository_state(self):
        sentinel = self.root / "source-was-executed"
        source = "from pathlib import Path\nPath(" + repr(str(sentinel)) + ").write_text('bad')\n"
        self.output = source
        with patch.dict(os.environ, {"ROUTERAI_API_KEY": "fake-router", "GITHUB_TOKEN": "fake-github",
                                     "QUEUE_DEPLOY_KEY": "fake-key", "SSH_AUTH_SOCK": "fake-socket", "GIT_DIR": "fake-repo"}), \
             patch.object(subprocess, "run", side_effect=self.fake_run):
            self.client.propose(context((source, source, source)))
        for command, options in self.calls:
            self.assertFalse(options["shell"])
            self.assertFalse(any(key in options["env"] for key in ("ROUTERAI_API_KEY", "GITHUB_TOKEN", "QUEUE_DEPLOY_KEY", "SSH_AUTH_SOCK", "GIT_DIR")))
            self.assertNotEqual(options["env"]["HOME"], os.environ.get("HOME"))
            self.assertFalse((Path(options["cwd"]) / ".git").exists())
            self.assertNotIn("solve", command)
        self.assertFalse(sentinel.exists())

    def test_github_requires_fixed_docker_boundary_even_when_disabled_by_caller(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "VALIDATION_IMAGE": "untrusted"}), \
             patch.object(shutil, "which", return_value="/trusted/docker"), \
             patch.object(subprocess, "run", side_effect=self.fake_run):
            result = StructuralRepairClient(self.binary, use_docker=False).propose(context())
        self.assertEqual(result["metadata"]["sandbox"]["image"], structural.IMAGE)
        calls = [command for command, _ in self.calls if command[1] == "run"]
        self.assertEqual(len(calls), 2)
        for command in calls:
            for required in ("--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
                self.assertIn(required, command)
            self.assertFalse(any("docker.sock" in item or "SSH_AUTH_SOCK" in item or item == "--pid=host" for item in command))
            self.assertIn(structural.IMAGE, command)
        self.assertTrue(any(item.endswith(",dst=/inputs,readonly") for item in calls[1]))
        self.assertTrue(any(item.endswith(",dst=/tool/mergiraf,readonly") for item in calls[1]))

    def test_timeout_cleans_only_its_own_container(self):
        def run(command, **options):
            self.calls.append((command, options))
            if command[1] == "run":
                raise subprocess.TimeoutExpired(command, 0.1)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with patch.object(shutil, "which", return_value="/trusted/docker"), patch.object(subprocess, "run", side_effect=run):
            self.client = StructuralRepairClient(self.binary, use_docker=True)
            self.error("structural_timeout")
        invocation = self.calls[0][0]
        name = invocation[invocation.index("--name") + 1]
        self.assertEqual([command for command, _ in self.calls[1:]], [["/trusted/docker", "kill", name], ["/trusted/docker", "rm", name]])


@unittest.skipUnless(os.environ.get("MERGIRAF_BINARY"), "Set MERGIRAF_BINARY to run real pinned-binary checks")
class RealMergirafTests(unittest.TestCase):
    def real_case(self, inputs):
        # Confirm the fixture actually conflicts under Git's ordinary line merge.
        with tempfile.TemporaryDirectory(prefix="ordinary-merge-") as temporary:
            directory = Path(temporary)
            paths = [directory / (side + ".py") for side in ("base", "target", "source")]
            for path, content in zip(paths, inputs):
                path.write_text(content, encoding="utf-8", newline="\n")
            ordinary = subprocess.run([shutil.which("git"), "merge-file", "-p", str(paths[1]), str(paths[0]), str(paths[2])],
                                      cwd=directory, capture_output=True, timeout=5)
            self.assertEqual(ordinary.returncode, 1)
        return StructuralRepairClient.from_environment().propose(context(inputs))

    def test_independent_imports_really_merge_without_ai(self):
        result = self.real_case(IMPORTS)
        self.assertIn("from math import floor, ceil, trunc", result["files"]["money.py"])
        self.assertEqual(result["metadata"]["files"][0]["exit_code"], 0)

    def test_independent_existing_functions_really_merge_without_ai(self):
        result = self.real_case(FUNCTIONS)
        self.assertEqual(result["files"]["money.py"], "def first(): return 10\ndef second(): return 20\n")

    def test_conflicting_formula_really_remains_unresolved(self):
        with self.assertRaises(StructuralError) as caught:
            self.real_case(FORMULA)
        self.assertEqual(caught.exception.reason, "structural_unresolved")
        self.assertEqual(caught.exception.metadata["files"][0]["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
