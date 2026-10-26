"""Opt-in real OpenCode test: synthetic model/credentials, loopback only."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import subprocess
import unittest
from unittest.mock import patch
import importlib.util

if not all(importlib.util.find_spec(name) for name in ("starlette", "httpx")):
    raise unittest.SkipTest("Install web extras to use the optional real-engine probe")

from gleskit_mcp.config import load_settings
from gleskit_mcp.web.profile import WebProfile
from gleskit_mcp.web.commands import Commands
from tests.test_web_shell import MemoryCredentials

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "var/macos-stage2-prerequisites/runtime/opencode"


@unittest.skipUnless(os.environ.get("GLESKIT_RUN_WEB_ENGINE_TESTS") == "1", "explicit synthetic real-engine test")
class RealWebEngineTests(unittest.TestCase):
    @contextmanager
    def protocol_fixture(self):
        if not ENGINE.is_file():
            self.skipTest("Pinned internal OpenCode fixture is not present")
        state = {"case": "success", "observed": [], "release": threading.Event()}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                case = state["case"]
                state["observed"].append({"path": self.path, "body": body, "auth": self.headers.get("Authorization"),
                                          "key": self.headers.get("x-api-key")})
                if case in {"401", "429", "500", "malformed"}:
                    self.send_response(200 if case == "malformed" else int(case))
                    self.send_header("Content-Type", "application/json"); self.end_headers()
                    key = self.headers.get("Authorization") or self.headers.get("x-api-key") or ""
                    self.wfile.write(("invalid " + key if case == "malformed" else json.dumps({"error": key})).encode())
                    return
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                def emit(value):
                    event = "event: " + value["type"] + "\n" if value.get("type") else ""
                    self.wfile.write((event + "data: " + json.dumps(value) + "\n\n").encode()); self.wfile.flush()
                text = "Synthetic protocol completion."
                if case == "echo":
                    key = (self.headers.get("Authorization") or self.headers.get("x-api-key") or "").removeprefix("Bearer ")
                    text = "Credential echo " + key + " / " + base64.b64encode(key.encode()).decode()
                try:
                    if self.path.endswith("/messages"):
                        emit({"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant", "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}})
                        emit({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                        emit({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                        emit({"type": "content_block_stop", "index": 0})
                        emit({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}})
                        emit({"type": "message_stop"})
                    elif self.path.endswith("/responses"):
                        item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}
                        response = {"id": "resp_fixture", "object": "response", "created_at": 1, "model": body["model"], "status": "in_progress", "output": []}
                        emit({"type": "response.created", "sequence_number": 0, "response": response})
                        emit({"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": {**item, "status": "in_progress", "content": []}})
                        emit({"type": "response.content_part.added", "sequence_number": 2, "item_id": item["id"], "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
                        emit({"type": "response.output_text.delta", "sequence_number": 3, "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text})
                        emit({"type": "response.output_text.done", "sequence_number": 4, "item_id": item["id"], "output_index": 0, "content_index": 0, "text": text})
                        emit({"type": "response.output_item.done", "sequence_number": 5, "output_index": 0, "item": item})
                        emit({"type": "response.completed", "sequence_number": 6, "response": {**response, "status": "completed", "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}})
                    else:
                        def chunk(delta, finish=None):
                            emit({"id": "chat_fixture", "object": "chat.completion.chunk", "created": 1, "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
                        if case == "tools" and not any(message.get("role") == "tool" for message in body["messages"]):
                            name = next(tool["function"]["name"] for tool in body["tools"] if tool["function"]["name"].endswith("workspace_status"))
                            chunk({"role": "assistant", "content": "Checking local tools."})
                            chunk({"tool_calls": [{"index": 0, "id": "call_fixture", "type": "function", "function": {"name": name, "arguments": "{}"}}]})
                            chunk({}, "tool_calls")
                        else:
                            chunk({"role": "assistant", "content": "Intermediate text. " if case == "hold" else text})
                            if case == "hold": state["release"].wait(state.get("hold_seconds", 12))
                            chunk({}, "stop")
                        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="gleskit-protocol-fixture-") as folder:
                with patch("gleskit_mcp.web.profile.load_settings", return_value=load_settings(Path(folder))):
                    profile = WebProfile(ROOT, Path(folder) / "profile", opencode=ENGINE)
                commands = Commands(profile, MemoryCredentials())
                commands.runtime.claim("protocol_tab")
                def call(name, **values):
                    # The browser SSE loop normally renews this lease. This
                    # in-process fixture has no browser and must do so itself.
                    commands.runtime.claim("protocol_tab")
                    return commands.execute(name, values, "protocol_tab")
                try: yield commands, call, state, server.server_port, Path(folder)
                finally:
                    state["release"].set()
                    commands.close()
        finally:
            server.shutdown(); server.server_close()

    def wait_terminal(self, call, conversation, status="completed", timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = call("get_chat_conversation", conversationId=conversation)
            if detail["turn_state"].get("status") == status: return detail
            time.sleep(0.1)
        self.fail("Real engine did not reach expected terminal state: " + status)

    def wait_mcp(self, commands):
        deadline = time.monotonic() + 125
        while time.monotonic() < deadline:
            status = commands.engine.mcp_status()
            if status["running"]: return
            if status.get("connection_state") != "connecting": self.fail("Real MCP readiness failed: " + str(status))
            time.sleep(0.2)
        self.fail("Real MCP startup exceeded its readiness deadline")

    def test_real_tool_round_trip_has_one_compact_timeline_and_preserves_instructions(self):
        with self.protocol_fixture() as (commands, call, state, port, folder):
            provider = call("save_chat_provider", kind="openai-compatible", label="Synthetic activity fixture",
                            endpoint=f"http://127.0.0.1:{port}/v1", models=["fixture/model"], defaultModel="fixture/model",
                            apiKey="synthetic-activity-fixture", enabled=True)["provider_id"]
            conversation = call("create_chat_conversation")["conversation_id"]
            self.wait_mcp(commands)
            state["case"] = "tools"
            sent = call("send_chat_message", conversationId=conversation, providerId=provider, modelId="fixture/model",
                        agentId="gleskit", content="Hi there, can you help me?")
            detail = self.wait_terminal(call, conversation)
            events = detail["turn_events"]
            self.assertNotIn("turn.progress", [row["event_type"] for row in events])
            self.assertEqual(sum(row["event_type"] == "turn.completed" for row in events), 1)
            tools = [row for row in events if row["event_type"].startswith("tool.")]
            self.assertEqual([row["event_type"] for row in tools], ["tool.started", "tool.finished"])
            self.assertEqual(len({row["payload"]["call_id"] for row in tools}), 1)
            self.assertTrue(tools[0]["payload"]["call_id"])
            self.assertEqual(len(events), 5, "Start, instructions, tool start/end and one turn completion")
            self.assertEqual(len(state["observed"]), 2, "One tool round-trip, not a retry loop")
            self.assertEqual(sum(row["role"] == "user" for row in detail["messages"]), 1)
            self.assertTrue(any(row["finished"] and "Synthetic protocol completion." in row["content_markdown"] for row in detail["messages"]))
            content = json.dumps(state["observed"][0]["body"])
            for name in ("SKILL.md", "references/00_core_rules.md", "references/12_tool_call_contract.md"):
                self.assertIn(json.dumps((commands.profile.skill / name).read_text())[1:-1], content)
            self.assertEqual({row["file"] for row in commands.chat.prepared[sent["turn_id"]]["files"]},
                             {"gleskit-agent-prompt.md", "SKILL.md", "references/00_core_rules.md", "references/12_tool_call_contract.md"})

    def test_provider_protocols_tools_recovery_and_credential_boundary(self):
        with self.protocol_fixture() as (commands, call, state, port, folder):
            canaries = []
            original_popen = subprocess.Popen
            with patch("gleskit_mcp.web.engine.subprocess.Popen", wraps=original_popen) as spawned:
                def connect(kind, index):
                    key = "synthetic-protocol-private-canary-" + str(index)
                    canaries.append(key)
                    saved = call("save_chat_provider", kind=kind, label="Synthetic protocol", endpoint=f"http://127.0.0.1:{port}/v1",
                                 models=["fixture/model"], defaultModel="fixture/model", apiKey=key, enabled=True)
                    return saved["provider_id"]
                def send(conversation, provider):
                    self.wait_mcp(commands)
                    return call("send_chat_message", conversationId=conversation, providerId=provider, modelId="fixture/model", agentId="gleskit", content="Check the local workspace status; do not run analysis or access external data.")
                # One real MCP tool round-trip is two model requests, one UI
                # turn. Intermediate text is not a second, fabricated question.
                provider = connect("openrouter", 1)
                state["case"] = "tools"
                conversation = call("create_chat_conversation")["conversation_id"]
                send(conversation, provider)
                detail = self.wait_terminal(call, conversation)
                self.assertFalse((commands.profile.cache / "matplotlib").exists(), "Chat/tool metadata must not scan plotting fonts")
                self.assertEqual(len(state["observed"]), 2)
                self.assertTrue(any(json.loads(row.get("tool_calls_json") or "[]") for row in detail["messages"]))
                self.assertTrue(any(row["event_type"] == "tool.finished" for row in detail["turn_events"]))
                self.assertEqual(sum(row["role"] == "user" for row in detail["messages"]), 1)
                state["case"] = "success"
                send(conversation, provider)
                detail = self.wait_terminal(call, conversation)
                self.assertEqual(sum(row["role"] == "user" for row in detail["messages"]), 2)
                self.assertEqual(len(state["observed"]), 3)
                # Cancel a genuinely in-progress stream, then kill/recover an
                # owned engine mid-turn. Neither operation resubmits a prompt.
                state["case"] = "hold"
                send(conversation, provider)
                deadline = time.monotonic() + 10
                while len(state["observed"]) < 4 and time.monotonic() < deadline: time.sleep(0.1)
                self.assertEqual(len(state["observed"]), 4)
                call("cancel_chat_request", conversationId=conversation)
                self.assertEqual(call("get_chat_conversation", conversationId=conversation)["turn_state"]["status"], "cancelled")
                state["release"].set()
                state["case"] = "success"
                send(conversation, provider)
                self.wait_terminal(call, conversation)
                state["release"].clear(); state["case"] = "hold"
                send(conversation, provider)
                deadline = time.monotonic() + 10
                while len(state["observed"]) < 6 and time.monotonic() < deadline: time.sleep(0.1)
                commands.engine.process.kill(); commands.engine.process.wait(timeout=5)
                self.wait_terminal(call, conversation, "failed")
                state["release"].set(); state["case"] = "success"
                call("reconnect_chat_runtime")
                send(conversation, provider)
                self.wait_terminal(call, conversation)
                for index, kind in enumerate(("openai", "anthropic"), 2):
                    provider = connect(kind, index)
                    conversation = call("create_chat_conversation")["conversation_id"]
                    before = len(state["observed"])
                    send(conversation, provider)
                    detail = self.wait_terminal(call, conversation)
                    self.assertEqual(len(state["observed"]), before + 1)
                    self.assertTrue(any("Synthetic protocol completion." in row["content_markdown"] for row in detail["messages"]))
                    observed = state["observed"][-1]
                    self.assertEqual(observed["path"], "/v1/responses" if kind == "openai" else "/v1/messages")
                    self.assertEqual(observed["auth"] if kind == "openai" else observed["key"], ("Bearer " if kind == "openai" else "") + canaries[-1])
                # Real engine failure paths; retries are cancelled explicitly
                # rather than waiting for OpenCode's rate-limit backoff.
                provider = connect("openai-compatible", 4)
                state["case"] = "echo"
                conversation = call("create_chat_conversation")["conversation_id"]
                send(conversation, provider)
                detail = self.wait_terminal(call, conversation)
                self.assertTrue(any("Credential echo [redacted] / [redacted]" in row["content_markdown"] for row in detail["messages"]))
                for case in ("401", "429", "500", "malformed"):
                    state["case"] = case
                    conversation = call("create_chat_conversation")["conversation_id"]
                    before = len(state["observed"])
                    send(conversation, provider)
                    deadline = time.monotonic() + 10
                    while len(state["observed"]) == before and time.monotonic() < deadline: time.sleep(0.1)
                    self.assertGreater(len(state["observed"]), before)
                    if case in {"429", "500", "malformed"}: call("cancel_chat_request", conversationId=conversation)
                    else: self.wait_terminal(call, conversation, "failed")
                commands.engine.stop()
                for invocation in spawned.call_args_list:
                    encoded = json.dumps(invocation.kwargs.get("env", {}))
                    for key in canaries: self.assertNotIn(key, encoded, "Provider key entered child environment")
                needles = [value.encode() for key in canaries for value in (key, base64.b64encode(key.encode()).decode(), base64.b64encode(("Bearer " + key).encode()).decode())]
                for path in folder.rglob("*"):
                    if path.is_file():
                        content = path.read_bytes()
                        self.assertFalse(any(needle in content for needle in needles), "Credential canary found in temporary profile: " + path.name)

    def test_real_engine_receives_preloaded_skills_and_completes_once(self):
        if not ENGINE.is_file():
            self.skipTest("Pinned internal OpenCode fixture is not present")
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4 * 1024 * 1024:
                    self.send_error(413); return
                body = json.loads(self.rfile.read(length))
                observed.append({"route": self.path, "model": body.get("model"),
                                 "authorized": self.headers.get("Authorization") == "Bearer synthetic-web-fixture",
                                 "messages": body.get("messages", [])})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for payload in [
                    {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "vendor/test",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Synthetic browser "}, "finish_reason": None}]},
                    {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "vendor/test",
                     "choices": [{"index": 0, "delta": {"content": "completion."}, "finish_reason": None}]},
                    {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "vendor/test",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]:
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="gleskit-real-web-fixture-") as folder:
                base = load_settings(Path(folder))
                with patch("gleskit_mcp.web.profile.load_settings", return_value=base):
                    profile = WebProfile(ROOT, Path(folder) / "profile", opencode=ENGINE)
                commands = Commands(profile, MemoryCredentials())
                try:
                    commands.runtime.claim("fixture_tab")
                    def call(name, **values):
                        commands.runtime.claim("fixture_tab")
                        return commands.execute(name, values, "fixture_tab")
                    call("save_chat_provider", providerId="fixture", kind="openai-compatible", label="Synthetic local fixture",
                         endpoint=f"http://127.0.0.1:{server.server_port}/v1", models=["vendor/test"], defaultModel="vendor/test",
                         apiKey="synthetic-web-fixture", enabled=True)
                    conversation = call("create_chat_conversation")["conversation_id"]
                    self.wait_mcp(commands)
                    result = call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test",
                                  agentId="gleskit", content="Explain this synthetic gene signature with literature references.")
                    deadline = time.monotonic() + 45
                    detail = {}
                    while time.monotonic() < deadline:
                        detail = call("get_chat_conversation", conversationId=conversation)
                        if any(row["role"] == "assistant" and row["finished"] for row in detail["messages"]): break
                        time.sleep(0.25)
                    self.assertTrue(any(row["finished"] and "Synthetic browser completion." in row["content_markdown"] for row in detail.get("messages", [])),
                                    "Pinned engine did not produce a persisted terminal answer")
                    self.assertEqual(len(observed), 1, "A single UI turn must create exactly one provider request")
                    self.assertTrue(observed[0]["authorized"])
                    self.assertEqual(observed[0]["route"], "/v1/chat/completions")
                    self.assertEqual(observed[0]["model"], "vendor/test")
                    content = "\n".join(message.get("content", "") if isinstance(message.get("content"), str) else json.dumps(message.get("content"))
                                        for message in observed[0]["messages"])
                    self.assertIn((profile.skill / "SKILL.md").read_text(), content)
                    self.assertIn((profile.skill / "references/00_core_rules.md").read_text(), content)
                    self.assertIn((profile.skill / "references/40_literature_and_provenance.md").read_text(), content)
                    self.assertNotIn(str(profile.workspace), commands.chat.prepared[result["turn_id"]].get("files", []))
                    self.assertNotIn(conversation, commands.chat.active)
                    self.assertTrue(any(row["event_type"] == "turn.completed" for row in detail["turn_events"]))
                    mcp = commands.engine.request("GET", "/mcp")
                    self.assertEqual(mcp.get("gleskit", {}).get("status"), "connected", "Managed GLESKit MCP must remain connected")
                finally:
                    commands.close()
        finally:
            server.shutdown()
            server.server_close()
