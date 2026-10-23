"""Focused regression tests: real Git repositories and a local status HTTP API."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from queue_gate import Gate, VALIDATION_IMAGE
from queue_status import CommitStatuses, StatusError


PRODUCT = Path(__file__).resolve().parents[1]


class StatusAPI(ThreadingHTTPServer):
    def __init__(self):
        self.items = {}
        self.posts = []
        self.fail_states = set()
        self.fail_posts = 0
        self.fail_gets = 0
        self.gets = 0
        self.lose_next_response = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value):
                data = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                owner.gets += 1
                if owner.fail_gets:
                    owner.fail_gets -= 1
                    self.reply(502, {"message": "Injected transient read failure"})
                    return
                sha = urlsplit(self.path).path.split("/")[5]
                self.reply(200, list(reversed(owner.items.get(sha, []))))

            def do_POST(self):
                sha = urlsplit(self.path).path.split("/")[5]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if owner.fail_posts:
                    owner.fail_posts -= 1
                    self.reply(500, {"message": "Injected transient write failure"})
                    return
                if body["state"] in owner.fail_states:
                    self.reply(503, {"message": "Injected unavailable status service"})
                    return
                body["id"] = len(owner.posts) + 1
                owner.items.setdefault(sha, []).append(body)
                owner.posts.append((sha, body))
                if owner.lose_next_response:
                    owner.lose_next_response = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(201, body)

        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server_port)

    def finish(self):
        self.shutdown()
        self.server_close()
        self.thread.join()


class QueueStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="council-status-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / "seed"
        self.seed.mkdir()
        self.remote = self.root / "remote.git"
        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(self.seed, "init", "-b", "main")
        self.git(self.seed, "config", "user.name", "Council tests")
        self.git(self.seed, "config", "user.email", "tests@localhost")
        for name in ("queue_gate.py", "queue_status.py", "queue_repair.py", "queue_pr.py", "queue_structural.py", "queue_budget.py", "acceptance.py", "money.py", "receipt.py",
                     "email_receipt.py", "feature_a.py", "feature_b.py", "feature_c.py"):
            shutil.copy2(PRODUCT / name, self.seed / name)
        (self.seed / ".queue").mkdir()
        shutil.copy2(PRODUCT / ".queue/requirements.json", self.seed / ".queue/requirements.json")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "test: создана исходная модель")
        self.git(self.seed, "branch", "feature/team")
        self.git(self.seed, "checkout", "-b", "feature/source")
        (self.seed / "feature_a.py").write_text("ENABLED = True\n")
        self.git(self.seed, "add", "feature_a.py")
        self.git(self.seed, "commit", "-m", "test: включена первая возможность")
        self.source = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "remote", "add", "origin", str(self.remote))
        self.git(self.seed, "push", "origin", "main", "feature/team", "feature/source")
        self.trusted = self.root / "trusted"
        self.git(self.root, "clone", "--branch", "main", str(self.remote), str(self.trusted))
        self.base = self.remote_head()
        self.api = StatusAPI()
        self.addCleanup(self.api.finish)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GITHUB_", "QUEUE_")) and key != "VALIDATION_IMAGE"}
        self.env.update(GITHUB_ACTIONS="false", GITHUB_REPOSITORY="fixture/repository", GITHUB_RUN_ID="42",
                        GITHUB_TOKEN="local-fixture-token", GITHUB_API_URL=self.api.url,
                        GITHUB_SERVER_URL="https://github.com", PYTHONDONTWRITEBYTECODE="1")

    @staticmethod
    def git(path, *args):
        result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def remote_head(self):
        return self.git(self.seed, "ls-remote", "origin", "refs/heads/feature/team").split()[0]

    def phase(self, phase, directory="state", source="feature/source", wrapper=None):
        state = self.root / directory
        arguments = [phase, "--repo", str(self.trusted), "--state-dir", str(state)]
        if phase == "prepare":
            arguments += ["--base-branch", "feature/team", "--source-branch", source]
        command = [sys.executable, str(self.trusted / "queue_gate.py")]
        if wrapper:
            command = [sys.executable, "-c", wrapper]
        process = subprocess.run(command + arguments, env=self.env, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", cwd=self.trusted)
        result = json.loads((state / "result.json").read_text(encoding="utf-8"))
        return process.returncode, result

    def bad_source(self):
        for name in ("b", "c"):
            (self.seed / ("feature_" + name + ".py")).write_text("ENABLED = True\n")
        self.git(self.seed, "add", "feature_b.py", "feature_c.py")
        self.git(self.seed, "commit", "-m", "test: добавлено несовместимое сочетание")
        self.git(self.seed, "push", "origin", "feature/source")
        return self.git(self.seed, "rev-parse", "HEAD")

    def test_pending_then_confirmed_success_and_honest_already_integrated(self):
        code, prepared = self.phase("prepare")
        self.assertEqual((code, prepared["decision"]), (0, "prepared"))
        self.assertEqual(self.remote_head(), self.base)
        self.assertTrue(all(body["state"] == "pending" for _, body in self.api.posts))
        code, accepted = self.phase("publish")
        self.assertEqual((code, accepted["decision"]), (0, "accepted"))
        self.assertEqual(accepted["published_candidate"], accepted["validation"]["tested_commit"])
        self.assertEqual(self.remote_head(), accepted["candidate"])
        self.assertEqual(self.api.items[self.source][-1]["state"], "success")
        self.assertTrue(self.api.items[self.source][-1]["description"].startswith("Принято:"))
        count = len(self.api.posts)
        self.assertEqual(self.phase("publish")[0], 0)
        self.assertEqual(len(self.api.posts), count)
        code, repeated = self.phase("prepare", "already")
        self.assertEqual((code, repeated["decision"]), (0, "already_integrated"))
        self.assertTrue(self.api.items[self.source][-1]["description"].startswith("Принято ранее:"))

    def test_failed_checks_make_exit_and_source_status_fail(self):
        bad = self.bad_source()
        code, result = self.phase("prepare")
        self.assertEqual((code, result["decision"], result["reason"]), (1, "rejected", "validation_failed"))
        self.assertEqual(self.api.items[bad][-1]["state"], "failure")
        self.assertEqual(self.remote_head(), self.base)

    def test_source_change_finishes_old_pending_without_approving_new_sha(self):
        self.assertEqual(self.phase("prepare")[0], 0)
        new = self.bad_source()
        code, result = self.phase("publish")
        self.assertEqual((code, result["decision"], len(result["attempts"])), (1, "rejected", 2))
        self.assertEqual(self.api.items[self.source][-1]["state"], "failure")
        self.assertIn("версия изменена", self.api.items[self.source][-1]["description"])
        self.assertEqual(self.api.items[new][-1]["state"], "failure")
        self.assertFalse(any(body["state"] == "success" for _, body in self.api.posts))
        self.assertEqual(self.remote_head(), self.base)

    def test_status_outage_after_publication_keeps_the_fact_and_finalize_redelivers(self):
        self.assertEqual(self.phase("prepare")[0], 0)
        self.api.fail_states.add("success")
        code, result = self.phase("publish")
        self.assertEqual((code, result["decision"], result["status_delivery"]), (0, "accepted", "failed"))
        self.assertTrue(result["publication_confirmed"])
        self.assertEqual(self.remote_head(), result["published_candidate"])
        self.assertFalse(any(body["state"] in ("success", "error", "failure") for _, body in self.api.posts))
        self.api.fail_states.clear()
        code, result = self.phase("finalize")
        self.assertEqual((code, result["decision"], result["status_delivery"]), (0, "accepted", "confirmed_by_finalize"))
        self.assertEqual(self.api.items[self.source][-1]["state"], "success")
        self.assertTrue(self.api.items[self.source][-1]["description"].startswith("Принято:"))

    def test_unknown_push_outcome_never_announces_acceptance(self):
        self.assertEqual(self.phase("prepare")[0], 0)
        wrapper = """import queue_gate, sys
original = queue_gate.Gate.observe
def observe(self, stage):
    if stage == 'after_publication':
        raise queue_gate.Rejected('git_error', 'Injected unavailable remote observation')
    return original(self, stage)
queue_gate.Gate.observe = observe
sys.exit(queue_gate.main())
"""
        code, result = self.phase("publish", wrapper=wrapper)
        self.assertEqual((code, result["decision"], result["reason"]), (1, "error", "publication_unconfirmed"))
        self.assertNotEqual(self.remote_head(), self.base)  # Publication may have happened.
        self.assertEqual(self.api.items[self.source][-1]["state"], "error")
        self.assertFalse(any(body["state"] == "success" for _, body in self.api.posts))

    def test_zero_exit_without_complete_oracle_report_is_rejected(self):
        (self.seed / "receipt.py").write_text("raise SystemExit(0)\n")
        self.git(self.seed, "add", "receipt.py")
        self.git(self.seed, "commit", "-m", "test: воспроизведён преждевременный выход")
        self.git(self.seed, "push", "origin", "feature/source")
        code, result = self.phase("prepare")
        self.assertEqual((code, result["decision"], result["reason"]), (1, "rejected", "validation_failed"))
        self.assertEqual(result["validation"]["returncode"], 0)
        self.assertIsNone(result["validation"]["report"])
        self.assertEqual(self.remote_head(), self.base)

    def test_status_dedup_and_lost_response_do_not_duplicate_posts(self):
        client = CommitStatuses("fixture/repository", "local-fixture-token", "42", self.api.url)
        self.api.lose_next_response = True
        first = client.send(self.source, "feature/team", "pending", "Проверяем")
        second = client.send(self.source, "feature/team", "pending", "Проверяем")
        self.assertTrue(first["deduplicated"] and second["deduplicated"])
        self.assertEqual(len(self.api.posts), 1)
        self.assertNotEqual(client.context("feature/A").casefold(), client.context("feature/a").casefold())
        client.send(self.source, "feature/other", "pending", "Проверяем")
        self.assertEqual(len(self.api.posts), 2)

    def test_transient_read_and_write_failures_are_retried_without_duplicates(self):
        client = CommitStatuses("fixture/repository", "local-fixture-token", "42", self.api.url)
        with patch("queue_status.RETRY_DELAYS", (0, 0)):
            self.api.fail_gets = 2
            self.api.fail_posts = 2
            event = client.send(self.source, "feature/team", "pending", "Проверяем")
            self.assertEqual((event["deduplicated"], event["post_attempts"]), (False, 3))
            self.assertEqual(len(self.api.posts), 1)
            self.api.fail_posts = 3
            with self.assertRaises(StatusError) as error:
                client.send(self.source, "feature/team", "success", "Принято")
            self.assertTrue(error.exception.transient)
            self.assertEqual(len(self.api.posts), 1)

    def test_lost_pending_status_does_not_stop_the_request(self):
        self.env["GITHUB_API_URL"] = self.api.url
        self.api.fail_states.add("pending")
        with patch.dict(os.environ, self.env, clear=True), patch("queue_status.RETRY_DELAYS", (0, 0)):
            gate = Gate(self.trusted, self.root / "lost-pending")
            import argparse
            gate.prepare(argparse.Namespace(base_branch="feature/team", source_branch="feature/source",
                                            hold_seconds="0", requirement="baseline"))
            gate.publish()
        self.assertEqual(gate.result["decision"], "accepted")
        self.assertEqual(self.remote_head(), gate.result["published_candidate"])
        self.assertTrue(gate.result["status_errors"])
        self.assertTrue(all(item["state"] == "pending" for item in gate.result["status_errors"]))
        self.assertEqual(self.api.items[self.source][-1]["state"], "success")

    def test_finalize_closes_pending_and_unsafe_mode_is_not_a_cli_option(self):
        self.assertEqual(self.phase("prepare")[0], 0)
        code, result = self.phase("finalize")
        self.assertEqual((code, result["decision"], result["reason"]), (1, "error", "interrupted"))
        self.assertEqual(self.api.items[self.source][-1]["state"], "error")
        count = len(self.api.posts)
        self.assertEqual(self.phase("finalize")[0], 1)
        self.assertEqual(len(self.api.posts), count)
        process = subprocess.run([sys.executable, str(self.trusted / "queue_gate.py"), "prepare", "--repo",
                                  str(self.trusted), "--state-dir", str(self.root / "unsafe"), "--mode", "pr_only"],
                                 env=self.env, capture_output=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.remote_head(), self.base)
        self.assertEqual(len(self.api.posts), count)

    def test_job_killed_during_prepare_does_not_leave_pending(self):
        wrapper = """import queue_gate, os, sys
def killed(self, observation, trigger):
    self.select_source(observation['source'])
    self.status(observation['source'], 'pending', 'Проверяем')
    os._exit(137)
queue_gate.Gate.build = killed
sys.exit(queue_gate.main())
"""
        state = self.root / "killed"
        process = subprocess.run([sys.executable, "-c", wrapper, "prepare", "--repo", str(self.trusted),
                                  "--state-dir", str(state), "--base-branch", "feature/team",
                                  "--source-branch", "feature/source"], env=self.env, capture_output=True,
                                 cwd=self.trusted)
        self.assertEqual(process.returncode, 137)
        self.assertEqual(self.api.items[self.source][-1]["state"], "pending")
        code, result = self.phase("finalize", "killed")
        self.assertEqual((code, result["decision"], result["reason"]), (1, "error", "interrupted"))
        self.assertEqual(self.api.items[self.source][-1]["state"], "error")

    def test_setup_failure_before_gate_reports_error_for_the_exact_head(self):
        self.env.update(GITHUB_ACTIONS="true", EXPECTED_HEAD=self.source)
        code, result = self.phase("finalize", "never-started")
        self.assertEqual((code, result["decision"], result["reason"]), (1, "error", "setup_failed"))
        self.assertEqual(self.api.items[self.source][-1]["state"], "error")
        self.assertEqual(self.api.items[self.source][-1]["context"], CommitStatuses.context("feature/team-shared"))
        self.assertEqual(self.remote_head(), self.base)

    def test_github_sandbox_uses_fixed_image_and_only_readonly_exports(self):
        gate = Gate(self.trusted, self.root / "sandbox-state")
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "VALIDATION_IMAGE": "attacker/image"}):
            command, sandbox = gate.validation_command(self.source)
        self.assertEqual(sandbox["image"], VALIDATION_IMAGE)
        for required in ("--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges"):
            self.assertIn(required, command)
        mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertFalse(any("docker.sock" in argument or "SSH_AUTH_SOCK" in argument
                             or argument == "--pid" or argument.startswith("--pid=") for argument in command))
        export = next((self.root / "sandbox-state").glob("validation-tree-*"))
        self.assertFalse((export / ".git").exists())


if __name__ == "__main__":
    unittest.main()
