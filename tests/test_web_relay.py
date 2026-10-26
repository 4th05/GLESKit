"""Synthetic credential-boundary tests; no real network or credential stores."""
from __future__ import annotations

import base64
import json
import unittest

try:
    import httpx
    from starlette.testclient import TestClient
except ImportError:
    raise unittest.SkipTest("Install web extras for relay tests")

from gleskit_mcp.web.relay import ProviderRelay
from gleskit_mcp.web.redaction import SecretFilter, StreamFilter

SECRET = "synthetic-relay-provider-secret-value"


class RelayTests(unittest.TestCase):
    def fixture(self, handler, kind="openai-compatible"):
        relay = ProviderRelay({"kind": kind, "endpoint": "https://provider.example/v1", "models": ["fixture/model"]},
                              SECRET, transport=httpx.MockTransport(handler))
        relay.url = "http://127.0.0.1:5018"
        return relay, TestClient(relay.app(), base_url=relay.url)

    def post(self, client, relay, **kwargs):
        headers = kwargs.pop("headers", {"Authorization": "Bearer " + relay.token})
        return client.post("/v1/" + ("messages" if relay.row["kind"] == "anthropic" else "chat/completions"),
                           json={"model": "fixture/model", "stream": True}, headers=headers, **kwargs)

    def test_fixed_destination_auth_and_header_allowlist(self):
        observed = []
        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={"ok": True})
        relay, client = self.fixture(handler)
        with client:
            result = self.post(client, relay, headers={"Authorization": "Bearer " + relay.token,
                        "Cookie": "private-cookie", "X-Forwarded-Host": "other.example"})
            self.assertEqual(result.status_code, 200)
        self.assertEqual(str(observed[0].url), "https://provider.example/v1/chat/completions")
        self.assertEqual(observed[0].headers["authorization"], "Bearer " + SECRET)
        self.assertNotIn("cookie", observed[0].headers)
        self.assertNotIn("x-forwarded-host", observed[0].headers)
        self.assertNotIn(relay.token, str(observed[0].headers))

    def test_browser_origin_unknown_model_path_and_missing_auth_fail_closed(self):
        observed = []
        relay, client = self.fixture(lambda request: observed.append(request) or httpx.Response(200, json={}))
        with client:
            self.assertEqual(self.post(client, relay, headers={}).status_code, 401)
            auth = {"Authorization": "Bearer " + relay.token}
            self.assertEqual(self.post(client, relay, headers={**auth, "Origin": "https://other.example"}).status_code, 403)
            self.assertEqual(self.post(client, relay, headers={**auth, "Host": "other.example"}).status_code, 403)
            self.assertEqual(client.post("/v1/chat/completions", json={"model": "unauthorized"}, headers=auth).status_code, 400)
            self.assertEqual(client.post("/v1/chat/completions?destination=https://other.example", json={}, headers=auth).status_code, 404)
            self.assertEqual(client.post("/v1/arbitrary", json={}, headers=auth).status_code, 404)
        self.assertFalse(observed)

    def test_provider_errors_redirects_malformed_json_and_headers_never_escape(self):
        for status in (401, 429, 500, 302):
            with self.subTest(status=status):
                relay, client = self.fixture(lambda request: httpx.Response(status, json={"error": SECRET},
                    headers={"X-Echo": SECRET, "Location": "https://other.example/" + SECRET}))
                with client:
                    result = self.post(client, relay)
                self.assertFalse(result.is_success)
                self.assertNotIn(SECRET, result.text + str(result.headers))
                self.assertNotIn("location", result.headers)
        relay, client = self.fixture(lambda request: httpx.Response(200, content=SECRET, headers={"Content-Type": "application/json"}))
        with client:
            result = self.post(client, relay)
        self.assertEqual(result.status_code, 502)
        self.assertNotIn(SECRET, result.text)

    def test_json_success_and_anthropic_auth_are_filtered(self):
        observed = []
        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={"text": SECRET, "nested": [base64.b64encode(SECRET.encode()).decode()], "ok": True, "count": 3})
        relay, client = self.fixture(handler, "anthropic")
        with client:
            result = self.post(client, relay, headers={"x-api-key": relay.token, "anthropic-version": "2023-06-01"})
        self.assertEqual(observed[0].headers["x-api-key"], SECRET)
        self.assertNotIn("authorization", observed[0].headers)
        self.assertNotIn(SECRET, result.text)
        self.assertEqual(result.json(), {"text": "[redacted]", "nested": ["[redacted]"], "ok": True, "count": 3})

    def test_sse_filters_keys_split_across_network_chunks_and_text_deltas(self):
        chunks = [
            {"id": "fixture", "choices": [{"index": 0, "delta": {"content": "Before " + SECRET[:13]}, "finish_reason": None}]},
            {"id": "fixture", "choices": [{"index": 0, "delta": {"content": SECRET[13:] + " after."}, "finish_reason": "stop"}]},
        ]
        encoded = "".join("data: " + json.dumps(row) + "\n\n" for row in chunks) + "data: [DONE]\n\n"
        class Split(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index in range(0, len(encoded), 7):
                    yield encoded[index:index + 7].encode()
        relay, client = self.fixture(lambda request: httpx.Response(200, stream=Split(), headers={"Content-Type": "text/event-stream"}))
        with client:
            result = self.post(client, relay)
        payloads = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
        content = "".join(choice.get("delta", {}).get("content", "") for row in payloads for choice in row.get("choices", []))
        self.assertEqual(content, "Before [redacted] after.")
        self.assertNotIn(SECRET, result.text)
        self.assertEqual(payloads[-1]["choices"][0]["finish_reason"], "stop")

    def test_sse_error_event_and_disconnect_are_safe(self):
        for encoded in ("event: error\ndata: " + json.dumps({"error": {"message": SECRET}}) + "\n\n", "data: " + SECRET + "\n\n",
                        'data: {"choices":[{"index":0,"delta":{"content":"Incomplete answer"},"finish_reason":null}]}\n\n'):
            relay, client = self.fixture(lambda request: httpx.Response(200, content=encoded, headers={"Content-Type": "text/event-stream"}))
            with client:
                result = self.post(client, relay)
            self.assertNotIn(SECRET, result.text)
            self.assertIn("provider stream failed", result.text)

    def test_incremental_anthropic_and_response_tool_arguments_keep_order(self):
        for kind, pieces in (("anthropic", [
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"key":"' + SECRET[:10]}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": SECRET[10:] + '"}'}}]),
            ("responses", [{"type": "response.function_call_arguments.delta", "item_id": "fixture", "delta": SECRET[:10]},
                           {"type": "response.function_call_arguments.delta", "item_id": "fixture", "delta": SECRET[10:]}])):
            stream = StreamFilter(SecretFilter((SECRET,)))
            values = [stream.clean(row) for row in pieces]
            values.extend(stream.flush())
            if kind == "anthropic":
                result = "".join(row["delta"]["partial_json"] for row in values)
                self.assertEqual(result, '{"key":"[redacted]"}')
            else:
                self.assertEqual("".join(row["delta"] for row in values), "[redacted]")

    def test_innocent_word_endings_and_json_scalar_types_are_preserved(self):
        redact = SecretFilter(("synthetic-audit-key",))
        self.assertEqual(redact.value({"ok": True, "count": 1, "value": None}), {"ok": True, "count": 1, "value": None})
        stream = StreamFilter(redact)
        first = stream.clean({"choices": [{"index": 0, "delta": {"content": "results"}}]})
        output = [first, *stream.flush()]
        self.assertEqual("".join(choice["delta"].get("content", "") for row in output for choice in row["choices"]), "results")


if __name__ == "__main__":
    unittest.main()
