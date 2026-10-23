"""Local HTTP protocol tests only; these do not call or emulate real DeepSeek."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

import queue_repair
from queue_repair import RepairError, RouterRepairClient


FIXTURE_KEY = "fake-local-router-key-only"


def envelope(proposal=None, **extra):
    value = {"id": "fixture-response-1", "model": queue_repair.MODEL,
             "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                       "completion_tokens_details": {"reasoning_tokens": 5}},
             "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(
                 proposal if proposal is not None else {"files": {"receipt.py": "value = 1\n"},
                                                       "explanation": "Исправлено преобразование."},
                 ensure_ascii=False)}}]}
    value.update(extra)
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class RouterFixture(ThreadingHTTPServer):
    def __init__(self):
        self.code = 200
        self.body = envelope()
        self.requests = []
        self.delay = 0
        self.drop_response = False
        self.redirect = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append({"method": "POST", "path": self.path, "body": body,
                                       "authorization": self.headers.get("Authorization")})
                if owner.drop_response:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                time.sleep(owner.delay)
                try:
                    self.send_response(owner.code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(owner.body)))
                    if owner.redirect:
                        self.send_header("Location", owner.redirect)
                    self.end_headers()
                    self.wfile.write(owner.body)
                except (OSError, ConnectionError):
                    pass  # The timeout fixture may have already closed the socket.

            def do_GET(self):
                owner.requests.append({"method": "GET", "path": self.path})
                self.send_response(500)
                self.end_headers()

        super().__init__(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server_port) + "/api/v1/chat/completions"

    def finish(self):
        self.shutdown()
        self.server_close()
        self.thread.join()


class QueueRepairTests(unittest.TestCase):
    def setUp(self):
        self.api = RouterFixture()
        self.addCleanup(self.api.finish)
        self.endpoint = patch.object(queue_repair, "API_URL", self.api.url)
        self.endpoint.start()
        self.addCleanup(self.endpoint.stop)
        self.client = RouterRepairClient(FIXTURE_KEY, ["receipt.py", "email_receipt.py"])
        self.context = {"task": "Preserve both receipt formats", "rejection": {"reason": "validation_failed"},
                        "files": {"receipt.py": "value = 100\n"}}

    def fails(self, reason, context=None, client=None):
        before = len(self.api.requests)
        with self.assertRaises(RepairError) as caught:
            (client or self.client).propose(self.context if context is None else context)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), caught.exception.detail)
        self.assertNotIn(FIXTURE_KEY, str(caught.exception))
        self.assertNotIn(FIXTURE_KEY, json.dumps(caught.exception.metadata))
        self.assertLessEqual(len(self.api.requests) - before, 1)
        return caught.exception

    def test_success_is_exact_model_one_request_and_safe_metadata(self):
        result = self.client.propose(self.context)
        self.assertEqual(result["files"], {"receipt.py": "value = 1\n"})
        self.assertEqual(set(result), {"files", "explanation", "metadata"})
        self.assertEqual(len(self.api.requests), 1)
        request = self.api.requests[0]
        self.assertEqual(request["authorization"], "Bearer " + FIXTURE_KEY)
        body = json.loads(request["body"])
        self.assertEqual(body["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn(FIXTURE_KEY.encode(), request["body"])
        metadata = result["metadata"]
        self.assertEqual(metadata["request_sha256"], hashlib.sha256(request["body"]).hexdigest())
        self.assertEqual(metadata["response_sha256"], hashlib.sha256(self.api.body).hexdigest())
        self.assertEqual(metadata["requested_model"], metadata["returned_model"])
        self.assertEqual(metadata["id"], "fixture-response-1")
        self.assertEqual(metadata["usage"]["total_tokens"], 130)
        self.assertGreaterEqual(metadata["elapsed_seconds"], 0)
        self.assertNotIn(FIXTURE_KEY, json.dumps(result))

    def test_environment_reads_only_named_key_and_never_changes_model_or_endpoint(self):
        with patch.dict(os.environ, {"ROUTERAI_API_KEY": FIXTURE_KEY,
                                     "ROUTERAI_MODEL": "other/model", "ROUTERAI_API_URL": "http://untrusted.invalid"}):
            client = RouterRepairClient.from_environment(["receipt.py"], timeout=2)
        self.assertEqual(client.timeout, 2)
        self.assertEqual(client.propose(self.context)["metadata"]["requested_model"], queue_repair.MODEL)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RepairError) as caught:
                RouterRepairClient.from_environment(["receipt.py"])
        self.assertEqual(caught.exception.reason, "repair_missing_credentials")
        self.assertEqual(len(self.api.requests), 1)

    def test_context_allowlist_can_only_narrow_constructor_scope(self):
        self.client.propose(dict(self.context, allowed_paths=["receipt.py"]))
        self.fails("repair_invalid_paths", dict(self.context, allowed_paths=["money.py"]))
        contextual = RouterRepairClient(FIXTURE_KEY)
        self.assertEqual(contextual.propose(dict(self.context, allowed_paths=["receipt.py"]))["files"],
                         {"receipt.py": "value = 1\n"})
        self.fails("repair_invalid_paths", client=contextual)
        self.assertEqual(len(self.api.requests), 2)

    def test_unknown_hidden_absolute_and_alias_paths_are_rejected(self):
        for path in ("money.py", ".github/workflows/queue.yml", "../receipt.py", "./receipt.py", "/receipt.py",
                     "a/../receipt.py", "a//receipt.py", "a/.private.py", "C:/receipt.py", "receipt.py ",
                     "receipt.py.", "RECEIPT.py", "a\\receipt.py", "CON.py", "receipt.py:stream"):
            with self.subTest(path=path):
                self.api.body = envelope({"files": {path: "value = 1\n"}, "explanation": "Исправлено."})
                self.fails("repair_invalid_paths")
        self.assertEqual(len(self.api.requests), 15)

    def test_proposal_schema_rejects_extra_keys_deletion_and_empty_changes(self):
        cases = [({"files": {"receipt.py": "value = 1\n"}, "explanation": "ok", "commands": ["anything"]},
                  "repair_invalid_response"),
                 ({"files": [], "explanation": "ok"}, "repair_invalid_response"),
                 ({"files": {"receipt.py": None}, "explanation": "ok"}, "repair_invalid_file"),
                 ({"files": {"receipt.py": ""}, "explanation": "ok"}, "repair_invalid_file"),
                 ({"files": {"receipt.py": "\0"}, "explanation": "ok"}, "repair_invalid_file"),
                 ({"files": {}, "explanation": "Cannot repair safely"}, "repair_no_changes")]
        for proposal, reason in cases:
            with self.subTest(reason=reason, proposal=proposal):
                self.api.body = envelope(proposal)
                self.fails(reason)
        self.assertEqual(len(self.api.requests), len(cases))

    def test_non_json_duplicate_keys_and_incomplete_responses_fail_closed(self):
        invalid = [b"not JSON", b"[]", b'{"choices":[],"choices":[]}',
                   envelope(choices=[{"finish_reason": "stop", "message": {"content": "```json\n{}\n```"}}]),
                   envelope(choices=[{"finish_reason": "stop", "message": {"content":
                       '{"files":{},"files":{"receipt.py":"v=1"},"explanation":"ok"}'}}])]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.api.body = raw
                self.fails("repair_invalid_response")
        self.api.body = envelope(choices=[{"finish_reason": "length", "message": {"content": "{}"}}])
        self.fails("repair_incomplete_response")
        self.api.body = envelope(choices=[{"finish_reason": "stop", "message": {"content": "{}",
                                                                                    "tool_calls": [{"id": "no"}]}}])
        self.fails("repair_incomplete_response")

    def test_incomplete_response_keeps_finish_reason_and_shape_but_no_provider_text(self):
        thinking = "private chain of thought " * 40
        self.api.body = envelope(choices=[{"finish_reason": "length", "message": {
            "role": "assistant", "content": "", "reasoning": thinking}}],
            usage={"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8192}})
        metadata = self.fails("repair_incomplete_response").metadata
        self.assertEqual(metadata["response_shape"], {
            "finish_reason": "length", "message_keys": ["content", "reasoning", "role"],
            "content_type": "str", "content_chars": 0, "reasoning_chars": len(thinking)})
        self.assertEqual(metadata["usage"]["completion_tokens_details"]["reasoning_tokens"], 8192)
        self.assertNotIn("private chain", json.dumps(metadata))
        self.api.body = envelope(choices=[{"finish_reason": "x" * 80, "message": {"content": None}}])
        shape = self.fails("repair_incomplete_response").metadata["response_shape"]
        self.assertEqual((shape["finish_reason"], shape["content_type"]), ("unrecognized", "NoneType"))

    def test_each_attempt_profile_sends_its_recorded_parameters_and_they_differ(self):
        bodies = []
        for profile, (parameters, timeout) in enumerate(queue_repair.REPAIR_PROFILES, 1):
            client = RouterRepairClient(FIXTURE_KEY, ["receipt.py"], profile=profile)
            metadata = client.propose(self.context)["metadata"]
            body = json.loads(self.api.requests[-1]["body"])
            self.assertEqual(body["model"], queue_repair.MODEL)
            self.assertEqual(set(body), {"model", "messages", *parameters})
            self.assertEqual({key: body[key] for key in parameters}, parameters)
            self.assertEqual((metadata["request_parameters"], metadata["profile"], metadata["timeout_seconds"]),
                             (parameters, profile, timeout))
            self.assertLess(body["max_tokens"] * 0.007, timeout)  # ~150 tokens/s measured on RouterAI.
            bodies.append({key: body[key] for key in parameters})
        self.assertNotEqual(bodies[0], bodies[1])
        for profile in (0, 3, True):
            with self.subTest(profile=profile), self.assertRaises(RepairError) as caught:
                RouterRepairClient(FIXTURE_KEY, ["receipt.py"], profile=profile)
            self.assertEqual(caught.exception.reason, "repair_invalid_profile")

    def test_model_mismatch_is_rejected_and_missing_identity_is_explicit(self):
        self.api.body = envelope(model="deepseek/different-model")
        self.fails("repair_model_mismatch")
        omitted = json.loads(envelope())
        del omitted["model"]
        self.api.body = json.dumps(omitted).encode()
        self.assertIsNone(self.client.propose(self.context)["metadata"]["returned_model"])
        self.assertEqual(len(self.api.requests), 2)

    def test_key_in_context_or_response_never_leaves_client_as_data(self):
        self.fails("repair_sensitive_data", dict(self.context, accidental=FIXTURE_KEY))
        self.assertEqual(len(self.api.requests), 0)
        for proposal in ({"files": {"receipt.py": FIXTURE_KEY}, "explanation": "ok"},
                         {"files": {"receipt.py": "value = 1"}, "explanation": FIXTURE_KEY}):
            self.api.body = envelope(proposal)
            self.fails("repair_sensitive_data")
        self.api.body = envelope(id=FIXTURE_KEY)
        self.fails("repair_sensitive_data")
        self.api.body = envelope(usage={"provider_message": FIXTURE_KEY})
        self.fails("repair_invalid_response")

    def test_http_failure_redirect_and_lost_response_never_retry(self):
        for status in (400, 401, 429, 503):
            self.api.code = status
            self.api.body = ("provider echoed credential: " + FIXTURE_KEY).encode()
            error = self.fails("repair_http_error")
            self.assertEqual(error.detail, "RouterAI returned HTTP " + str(status))
        self.api.code = 307
        self.api.redirect = self.api.url + "/redirected"
        self.fails("repair_http_error")
        self.assertEqual(len(self.api.requests), 5)
        self.api.drop_response = True
        self.fails("repair_transport_error")
        self.assertEqual(len(self.api.requests), 6)
        self.assertTrue(all(request["method"] == "POST" and request["path"] == "/api/v1/chat/completions"
                            for request in self.api.requests))

    def test_timeout_is_bounded_and_not_retried(self):
        self.api.delay = 0.3
        client = RouterRepairClient(FIXTURE_KEY, ["receipt.py"], timeout=0.05)
        started = time.monotonic()
        self.fails("repair_transport_error", client=client)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.api.requests), 1)
        for timeout in (0, -1, queue_repair.MAX_TIMEOUT + 1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(RepairError) as caught:
                RouterRepairClient(FIXTURE_KEY, ["receipt.py"], timeout=timeout)
            self.assertEqual(caught.exception.reason, "repair_invalid_timeout")

    def test_rejected_proposals_keep_safe_evidence_of_each_http_response(self):
        cases = [({"files": {}, "explanation": "Cannot repair safely"}, "repair_no_changes"),
                 ({"files": {"receipt.py": "v = 1"}, "explanation": "ok", "unexpected": FIXTURE_KEY},
                  "repair_invalid_response")]
        for number, (proposal, reason) in enumerate(cases, 1):
            self.api.body = envelope(proposal, id="fixture-rejection-" + str(number))
            metadata = self.fails(reason).metadata
            self.assertEqual(metadata["request_sha256"], hashlib.sha256(self.api.requests[-1]["body"]).hexdigest())
            self.assertEqual(metadata["response_sha256"], hashlib.sha256(self.api.body).hexdigest())
            self.assertEqual(metadata["id"], "fixture-rejection-" + str(number))
            self.assertEqual(metadata["returned_model"], queue_repair.MODEL)
            self.assertEqual(metadata["usage"]["total_tokens"], 130)
            self.assertGreaterEqual(metadata["elapsed_seconds"], 0)
            self.assertEqual(set(metadata), {"request_sha256", "response_sha256", "id", "requested_model",
                                              "returned_model", "usage", "elapsed_seconds", "request_parameters", "provider",
                                              "profile", "timeout_seconds", "request_sent", "response_received", "http_status",
                                              "response_bytes", "response_shape"})
            self.assertEqual((metadata["request_sent"], metadata["response_received"], metadata["http_status"]),
                             (True, True, 200))
            self.assertEqual(metadata["response_shape"]["finish_reason"], "stop")
        self.assertEqual(len(self.api.requests), 2)

    def test_failed_transport_and_untrusted_envelope_keep_only_valid_metadata(self):
        self.api.drop_response = True
        metadata = self.fails("repair_transport_error").metadata
        self.assertEqual(set(metadata), {"request_sha256", "requested_model", "elapsed_seconds", "profile",
                                          "timeout_seconds", "request_parameters", "request_sent",
                                          "response_received"})
        self.assertEqual((metadata["request_sent"], metadata["response_received"]), (True, False))
        self.assertEqual(metadata["request_sha256"], hashlib.sha256(self.api.requests[-1]["body"]).hexdigest())
        self.api.drop_response = False
        for body, reason in ((envelope(id=FIXTURE_KEY), "repair_sensitive_data"),
                             (envelope(model="untrusted/model"), "repair_model_mismatch"),
                             (b"invalid provider body", "repair_invalid_response")):
            self.api.body = body
            metadata = self.fails(reason).metadata
            self.assertEqual(set(metadata), {"request_sha256", "response_sha256", "requested_model", "elapsed_seconds",
                                              "request_parameters", "profile", "timeout_seconds",
                                              "request_sent", "response_received",
                                              "http_status", "response_bytes"})
            self.assertTrue(metadata["response_received"])
            self.assertEqual(metadata["response_sha256"], hashlib.sha256(body).hexdigest())
        self.api.code = 503
        self.api.body = FIXTURE_KEY.encode()
        metadata = self.fails("repair_http_error").metadata
        self.assertEqual(metadata["response_sha256"], hashlib.sha256(self.api.body).hexdigest())
        self.assertNotIn("id", metadata)

    def test_context_response_and_proposal_size_limits(self):
        self.fails("repair_context_too_large", dict(self.context, oversized="x" * queue_repair.MAX_CONTEXT_BYTES))
        self.assertEqual(len(self.api.requests), 0)
        self.api.body = b" " * (queue_repair.MAX_RESPONSE_BYTES + 1)
        self.fails("repair_response_too_large")
        self.api.body = envelope({"files": {"receipt.py": "x" * (queue_repair.MAX_FILE_BYTES + 1)},
                                  "explanation": "ok"})
        self.fails("repair_proposal_too_large")
        self.api.body = envelope({"files": {"receipt.py": "v = 1"},
                                  "explanation": "x" * (queue_repair.MAX_EXPLANATION_BYTES + 1)})
        self.fails("repair_proposal_too_large")
        self.assertEqual(len(self.api.requests), 3)

    def test_context_malformed_json_data_does_not_make_request(self):
        for context in (None, [], {"number": float("nan")}, {"not_json": object()}):
            with self.subTest(context_type=type(context).__name__):
                self.fails("repair_invalid_context", context=context if context is not None else [])
        self.assertEqual(len(self.api.requests), 0)


if __name__ == "__main__":
    unittest.main()
