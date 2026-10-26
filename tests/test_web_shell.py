from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    import starlette
    import httpx
except ImportError:
    raise unittest.SkipTest("Install the optional web dependencies to run browser-host tests")

from gleskit_mcp.config import load_settings
from gleskit_mcp.store import WorkspaceStore
from gleskit_mcp.web.common import WebError, identifier
from gleskit_mcp.web.profile import WebProfile
from gleskit_mcp.web.engine import Engine
from gleskit_mcp.web.chat import Chat
from gleskit_mcp.web.commands import ALL_COMMANDS, Commands
from gleskit_mcp.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


class MemoryCredentials:
    storage = "memory-only"
    def __init__(self): self.rows = {}
    def get(self, key): return self.rows.get(key)
    def set(self, key, value): self.rows[key] = value
    def delete(self, key): self.rows.pop(key, None)


class FakeEngine(Engine):
    def __init__(self, profile, providers):
        super().__init__(profile, providers)
        self.starts = 0
        self.running = False
        self.messages = {}
        self.sent = []
    def start(self):
        if not self.running:
            self.starts += 1
            self.running = True
        return self.status()
    def stop(self):
        self.running = False
        return self.status()
    def status(self): return {"running": self.running, "available": True, "transport": "stdio", "error": None}
    def mcp_status(self, *, refresh=False): return {"running": self.running, "transport": "stdio", "connection_state": "connected" if self.running else "stopped", "error": None}
    def request(self, method, path, body=None):
        if method == "POST" and path == "/session":
            key = "ses_" + str(len(self.messages) + 1)
            self.messages[key] = []
            return {"id": key, "title": body["title"], "time": {"created": 1000}}
        parts = path.split("/")
        conversation = parts[2]
        if path.endswith("/prompt_async"):
            self.sent.append(body)
            self.messages[conversation] += [{"info": {"id": body["messageID"], "role": "user", "time": {"created": 1000}}, "parts": body["parts"]},
                 {"info": {"id": "assistant_" + body["messageID"], "parentID": body["messageID"], "sessionID": conversation,
                           "role": "assistant", "time": {"created": 2000}}, "parts": [{"type": "text", "text": "Partial answer"}]}]
            return None
        if path.endswith("/message"): return self.messages[conversation]
        if method == "DELETE":
            self.messages.pop(conversation)
        return {}


class WebShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-web-test-")
        self.root = Path(self.temp.name).resolve()
        self.base = load_settings(self.root)
        with patch("gleskit_mcp.web.profile.load_settings", return_value=self.base):
            self.profile = WebProfile(ROOT, self.root / "profile")
        self.commands = Commands(self.profile, MemoryCredentials(), FakeEngine)
        self.tab = "tab_a"
        self.commands.runtime.claim(self.tab)

    def tearDown(self):
        self.commands.close()
        self.temp.cleanup()

    def call(self, name, **values):
        return self.commands.execute(name, values, self.tab)

    def test_child_resource_limits_are_explicit_and_not_inherited_from_the_host(self):
        from dataclasses import replace
        self.profile.settings = replace(self.profile.settings, local_max_workers=2, r_threads=1, r_max_vsize="4Gb")
        self.assertNotIn("GLESKIT_LOCAL_MAX_WORKERS", self.profile.environment(), "Native child behavior is unchanged")
        self.profile.forward_resource_limits = True
        with patch.dict("os.environ", {"GLESKIT_LOCAL_MAX_WORKERS": "99", "OPENAI_API_KEY": "synthetic-not-forwarded"}):
            env = self.profile.environment()
        self.assertEqual(env["GLESKIT_LOCAL_MAX_WORKERS"], "2")
        self.assertEqual(env["GLESKIT_R_THREADS"], "1")
        self.assertEqual(env["GLESKIT_R_MAX_VSIZE"], "4Gb")
        self.assertNotIn("OPENAI_API_KEY", env)

    def provider(self):
        return self.call("save_chat_provider", providerId="fixture", kind="openai-compatible", label="Synthetic fixture",
                         endpoint="http://127.0.0.1:19999/v1", apiKey="synthetic-fixture-secret", models=["vendor/test"],
                         defaultModel="vendor/test", enabled=True)

    def conversation(self):
        self.provider()
        return self.call("create_chat_conversation")["conversation_id"]

    def test_owned_chat_history_remains_readable_after_host_restart_with_engine_stopped(self):
        conversation = self.conversation()
        self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Synthetic question")
        engine = self.commands.engine
        engine.messages[conversation][-1]["info"].update({"finish": "stop", "time": {"created": 2000, "completed": 3000}})
        engine.messages[conversation][-1]["parts"].append({"type": "reasoning", "text": "Synthetic hidden reasoning must not be cached"})
        engine.messages[conversation][-1]["info"]["system"] = "Synthetic private prompt must not be cached"
        original = self.commands.chat.detail(conversation)
        history = self.profile.data / "chat-history" / (conversation + ".json")
        self.assertNotIn("must not be cached", history.read_text())
        engine.stop()
        starts, sent = engine.starts, len(engine.sent)
        reopened = Chat(self.profile, engine).detail(conversation)
        self.assertEqual(reopened["messages"], original["messages"])
        self.assertFalse(reopened["engine_running"])
        self.assertEqual(engine.starts, starts)
        self.assertEqual(len(engine.sent), sent, "Reading cached history must never resubmit a prompt")

    def test_uncached_history_when_engine_is_stopped_explains_how_to_restore_it(self):
        conversation = self.conversation()
        self.commands.engine.stop()
        payload = self.commands.chat.detail(conversation)
        self.assertEqual(payload["messages"], [])
        self.assertIn("No messages were deleted", payload["history_warning"])
        self.assertFalse((self.profile.data / "chat-history" / (conversation + ".json")).exists())

    def test_failed_history_refresh_preserves_the_last_saved_messages(self):
        conversation = self.conversation()
        self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Synthetic question")
        original = self.commands.chat.detail(conversation)
        with patch.object(self.commands.engine, "request", side_effect=WebError("Synthetic disconnect")):
            payload = self.commands.chat.detail(conversation)
        self.assertEqual(payload["messages"], original["messages"])
        self.assertIn("last locally saved messages", payload["history_warning"])

    def test_restart_and_provider_retry_are_explicit_without_resending(self):
        conversation = self.conversation()
        self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Status only")
        self.commands.chat.on_event({"type": "session.status", "properties": {"sessionID": conversation,
            "status": {"type": "retry", "message": "synthetic-fixture-secret", "attempt": 1}}})
        events = self.commands.chat.detail(conversation)["turn_events"]
        self.assertTrue(any(row["event_type"] == "turn.retrying" for row in events))
        self.assertNotIn("synthetic-fixture-secret", json.dumps(events))
        restarted = Chat(self.profile, self.commands.engine)
        self.assertEqual(restarted.states[conversation]["status"], "failed")
        self.assertIn("restarted", restarted.states[conversation]["error"])
        self.assertEqual(len(self.commands.engine.sent), 1)
        self.commands.engine.callback = self.commands.chat.on_event

    def test_model_listing_is_explicit_bounded_and_filters_provider_errors(self):
        self.provider()
        observed = []
        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={"data": [{"id": "vendor/other", "name": "synthetic-fixture-secret"}]})
        real_client = httpx.Client
        with patch("gleskit_mcp.web.providers.httpx.Client", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)):
            models = self.call("fetch_provider_models", providerId="fixture")
        self.assertEqual(models, [{"id": "vendor/other", "label": "[redacted]"}])
        self.assertEqual(str(observed[0].url), "http://127.0.0.1:19999/v1/models")
        self.assertEqual(observed[0].headers["authorization"], "Bearer synthetic-fixture-secret")
        self.assertEqual(self.commands.engine.starts, 0)

    def test_draft_model_catalog_command_does_not_create_a_connection_or_engine(self):
        real_client = httpx.Client
        with patch("gleskit_mcp.web.providers.httpx.Client", side_effect=lambda **kwargs: real_client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": "deepseek/fixture"}]})), **kwargs)):
            result = self.call("fetch_provider_models", kind="openrouter", providerId=None, apiKey=None, force=False)
        self.assertEqual(result[0]["id"], "deepseek/fixture")
        self.assertEqual(self.commands.providers.list(), [])
        self.assertEqual(self.commands.engine.starts, 0)
        self.commands.runtime.close()
        with self.assertRaisesRegex(WebError, "connection expired"):
            self.call("fetch_provider_models", kind="openrouter")

    def test_connecting_mcp_does_not_queue_or_submit_model_requests(self):
        conversation = self.conversation()
        with patch.object(self.commands.engine, "mcp_status", return_value={"running": False, "connection_state": "connecting"}):
            with self.assertRaisesRegex(WebError, "still connecting"):
                self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Status")
        self.assertEqual(self.commands.engine.sent, [])

    def test_background_mcp_refresh_does_not_disconnect_verified_tools(self):
        engine = self.commands.engine
        engine.start()
        engine._mcp = {"running": True, "connection_state": "connected", "error": None}
        engine._mcp_checked = time.monotonic() - 10
        release = threading.Event()
        try:
            with patch.object(engine, "_check_mcp", side_effect=lambda *args: release.wait(2)):
                status = Engine.mcp_status(engine, refresh=True)
                self.assertTrue(status["running"])
                self.assertEqual(status["connection_state"], "connected")
        finally:
            release.set()
            engine._mcp_thread.join(timeout=3)

    def test_metadata_and_settings_never_start_engine(self):
        self.assertEqual(self.call("bootstrap_workspace")["sessions"]["session_count"], 0)
        self.call("reference_status")
        self.call("get_web_status")
        self.call("get_web_settings")
        self.call("bootstrap_chat")
        self.assertEqual(self.commands.engine.starts, 0)
        self.assertFalse(self.commands.runtime.snapshot()["capture_enabled"])
        self.assertIsNone(self.commands.runtime.active)

    def test_openrouter_catalog_and_independent_compatible_connections(self):
        self.assertIn("openrouter", {row["kind"] for row in self.call("get_provider_catalog")})
        ids = []
        for label in ("First fixture", "Second fixture"):
            row = self.call("save_chat_provider", kind="openai-compatible", label=label, endpoint="http://127.0.0.1:19999/v1",
                            apiKey="synthetic-independent-secret", models=["fixture/model"], defaultModel="fixture/model", enabled=True)
            ids.append(row["provider_id"])
        self.assertNotEqual(*ids)
        self.assertEqual(len(self.call("list_chat_providers")), 2)
        router = self.call("save_chat_provider", kind="openrouter", label="Router fixture", endpoint=None,
                           apiKey="synthetic-router-secret", models=["vendor/model"], enabled=True)
        self.assertEqual(router["endpoint"], "https://openrouter.ai/api/v1")
        self.assertEqual(self.commands.engine.starts, 0)

    def test_engine_loss_does_not_lock_cancel_reconnect_or_settings(self):
        conversation = self.conversation()
        self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Fixture")
        self.call("get_chat_conversation", conversationId=conversation)
        self.commands.engine.running = False
        self.commands.chat.on_event({"type": "gleskit.engine.stopped", "properties": {}})
        self.assertNotIn(conversation, self.commands.chat.active)
        detail = self.call("get_chat_conversation", conversationId=conversation)
        self.assertEqual(detail["turn_state"]["status"], "failed")
        self.assertEqual(detail["messages"][-1]["content_markdown"], "Partial answer")
        self.call("cancel_chat_request", conversationId=conversation)
        self.call("reconnect_chat_runtime")
        self.assertTrue(self.commands.engine.running)
        self.assertEqual(len(self.commands.engine.sent), 1)

    def test_failed_abort_stops_owned_engine_and_records_cancellation(self):
        conversation = self.conversation()
        self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Fixture")
        with patch.object(self.commands.engine, "request", side_effect=WebError("Synthetic lost acknowledgement")):
            self.call("cancel_chat_request", conversationId=conversation)
        self.assertFalse(self.commands.engine.running)
        self.assertNotIn(conversation, self.commands.chat.active)
        self.assertEqual(self.commands.chat.states[conversation]["status"], "cancelled")

    def test_disconnected_mcp_prevents_provider_submission(self):
        conversation = self.conversation()
        with patch.object(self.commands.engine, "mcp_status", return_value={"running": False, "error": "Synthetic MCP not connected"}):
            self.assertFalse(self.call("bootstrap_chat")["mcp_status"]["running"])
            with self.assertRaisesRegex(WebError, "MCP not connected"):
                self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Fixture")
        self.assertFalse(self.commands.engine.sent)

    def test_keyless_connection_is_explicit_and_loopback_only(self):
        self.call("save_chat_provider", kind="local", label="Local fixture", endpoint="http://127.0.0.1:19999/v1",
                  apiKey=None, models=["local/model"], enabled=True)
        self.assertTrue(self.call("list_chat_providers")[0]["connected"])
        self.call("bootstrap_chat")
        self.assertEqual(self.commands.engine.starts, 1)
        with self.assertRaisesRegex(WebError, "loopback"):
            self.call("save_chat_provider", kind="local", label="Not local", endpoint="https://external.example/v1",
                      apiKey=None, models=["local/model"], enabled=True)

    def test_existing_r_executable_is_resolved_for_the_isolated_child(self):
        with patch("gleskit_mcp.web.profile.load_settings", return_value=self.base), \
             patch("gleskit_mcp.web.profile.shutil.which", side_effect=lambda name: "/synthetic-runtime/bin/Rscript" if name == self.base.rscript_bin else None):
            profile = WebProfile(ROOT, self.root / "separate-r-profile")
        try:
            self.assertEqual(profile.environment()["GLESKIT_RSCRIPT_BIN"], "/synthetic-runtime/bin/Rscript")
            config = json.loads(profile.settings_file.read_text())
            self.assertEqual(config["tools"]["rscript_bin"], "/synthetic-runtime/bin/Rscript")
            self.assertEqual(profile.settings.r_lib_root, self.base.r_lib_root)
        finally:
            profile.close()

    def test_session_note_snapshot_and_deletion_contracts(self):
        session = self.call("create_session", title="Synthetic session", description="Fixture", species="human", tags=[])["session_id"]
        note = self.call("create_session_note", sessionId=session, title="Note", bodyMarkdown="Fixture text", author="user", linkedArtifactIds=[], tags=[])
        self.assertEqual(self.call("get_session_note", sessionId=session, noteId=note["note_id"])["body_markdown"], "Fixture text")
        snapshot = self.call("get_session_snapshot", sessionId=session)
        self.assertEqual(snapshot["summary"]["note_count"], 1)
        self.assertEqual(snapshot["artifacts"]["artifact_count"], 0)
        self.assertEqual(self.call("delete_sessions", sessionIds=[session])["deleted_count"], 1)

    def test_control_and_runtime_are_explicit_and_revisioned(self):
        session = self.call("create_session", title="Fixture", description="", species="human", tags=[])["session_id"]
        first = self.commands.runtime.revision
        self.commands.execute("write_runtime_heartbeat", {"activeSessionId": session, "captureEnabled": True}, self.tab, first)
        self.assertTrue(self.commands.runtime.snapshot()["capture_enabled"])
        with self.assertRaisesRegex(WebError, "changed"):
            self.commands.execute("write_runtime_heartbeat", {"activeSessionId": None, "captureEnabled": False}, self.tab, first)
        self.assertFalse(self.commands.runtime.claim("tab_b")["writable"])
        self.commands.runtime.claim("tab_b", takeover=True)
        with self.assertRaises(WebError): self.call("create_session", title="Forbidden", description="", species="human", tags=[])
        self.commands.runtime.expires = time.monotonic() - 1
        self.commands.runtime.renew("tab_b")
        self.assertFalse(self.commands.runtime.snapshot()["capture_enabled"])
        self.assertIsNone(self.commands.runtime.active)

    def test_provider_secret_never_enters_metadata_config_or_mcp_env(self):
        self.provider()
        self.assertNotIn("synthetic-fixture-secret", self.commands.providers.path.read_text())
        self.assertNotIn("synthetic-fixture-secret", json.dumps(self.call("list_chat_providers")))
        config = self.commands.engine.configuration()
        self.assertEqual(config["share"], "disabled")
        self.assertEqual(config["plugin"], [])
        self.assertEqual(config["mcp"]["gleskit"]["environment"]["GLESKIT_PROVIDER_API_KEY"], "")
        self.assertEqual(config["mcp"]["gleskit"]["environment"]["OPENCODE_SERVER_PASSWORD"], "")
        self.assertNotIn("synthetic-fixture-secret", json.dumps(config))

    def test_every_submitted_turn_contains_real_instruction_file_contents(self):
        conversation = self.conversation()
        for content in ("Interpret this gene signature", "Find papers and generate a figure"):
            result = self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content=content)
            sent = self.commands.engine.sent[-1]
            self.assertIn((self.profile.skill / "SKILL.md").read_text(), sent["system"])
            self.assertIn((self.profile.skill / "references/00_core_rules.md").read_text(), sent["system"])
            self.assertNotIn(str(self.profile.workspace), sent["system"])
            if "papers" in content:
                self.assertIn("references/40_literature_and_provenance.md", sent["system"])
                self.assertIn("references/50_visuals_and_dashboards.md", sent["system"])
            self.call("cancel_chat_request", conversationId=conversation)
        self.assertEqual(len(self.commands.engine.sent), 2)

    def test_missing_required_file_prevents_submission(self):
        conversation = self.conversation()
        (self.profile.skill / "references/00_core_rules.md").unlink()
        with self.assertRaisesRegex(WebError, "Required instruction"):
            self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Gene question")
        self.assertFalse(self.commands.engine.sent)

    def test_approval_actions_preserve_frontend_conversation_contract(self):
        conversation = self.conversation()
        for action, expected in (("allow", "once"), ("allow_always", "always"), ("deny", "reject")):
            self.commands.chat.approvals["approval_fixture"] = conversation
            with patch.object(self.commands.engine, "request", return_value={}) as request:
                result = self.call("resolve_chat_tool_approval", approvalId="approval_fixture", action=action)
            self.assertEqual(request.call_args.args[2]["response"], expected)
            self.assertEqual(result["conversation_id"], conversation)
        result = self.call("reset_chat_conversation_tool_approval", conversationId=conversation)
        self.assertEqual(result["conversation_id"], conversation)
        self.assertEqual(result["tool_approval_mode"], "ask")

    def test_partial_and_final_answers_keep_the_same_turn(self):
        conversation = self.conversation()
        result = self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Gene question")
        with self.assertRaisesRegex(WebError, "active turn"):
            self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Duplicate")
        detail = self.call("get_chat_conversation", conversationId=conversation)
        self.assertEqual(detail["messages"][-1]["content_markdown"], "Partial answer")
        self.assertFalse(detail["messages"][-1]["finished"])
        self.assertEqual(detail["messages"][-1]["turn_id"], result["turn_id"])
        self.assertIn(conversation, self.commands.chat.active)
        assistant = self.commands.engine.messages[conversation][-1]
        assistant["info"].update(finish="stop", time={"created": 2000, "completed": 3000})
        self.commands.chat.on_event({"type": "message.updated", "properties": {"info": assistant["info"]}})
        detail = self.call("get_chat_conversation", conversationId=conversation)
        self.assertTrue(detail["messages"][-1]["finished"])
        self.assertNotIn(conversation, self.commands.chat.active)
        self.assertEqual(len(self.commands.engine.sent), 1)

    def test_tool_steps_do_not_finish_a_turn_or_enter_assistant_text(self):
        conversation = self.conversation()
        sent = self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Gene question")
        self.commands.chat.on_event({"type": "message.part.updated", "properties": {"sessionID": conversation, "part": {
            "messageID": "assistant_" + sent["turn_id"], "type": "tool", "tool": "gleskit_resolve_gene", "state": {"status": "completed", "output": "Activity only"}}}})
        self.assertIn(conversation, self.commands.chat.active)
        detail = self.call("get_chat_conversation", conversationId=conversation)
        self.assertNotIn("Activity only", detail["messages"][-1]["content_markdown"])
        self.assertTrue(any(row["event_type"] == "tool.finished" for row in detail["turn_events"]))

    def test_progress_bursts_are_coalesced_and_never_replayed_as_activity(self):
        conversation = self.conversation()
        sent = self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Hi there")
        event = {"type": "message.part.updated", "properties": {"sessionID": conversation, "part": {
            "messageID": "assistant_" + sent["turn_id"], "type": "text", "text": "Partial answer"}}}
        with patch("gleskit_mcp.web.chat.time.monotonic", return_value=100) as clock:
            for _ in range(1000): self.commands.chat.on_event(event)
            clock.return_value = 100.8
            for _ in range(1000): self.commands.chat.on_event(event)
        streamed = [row for _, row in self.commands.engine.events.after(0)[1]]
        self.assertEqual(sum(row["event_type"] == "turn.progress" for row in streamed), 2)
        detail = self.call("get_chat_conversation", conversationId=conversation)
        self.assertEqual({row["event_type"] for row in detail["turn_events"]}, {"turn.started", "turn.prompt_compiled"})
        self.assertEqual(detail["messages"][-1]["content_markdown"], "Partial answer")
        self.assertEqual(detail["turn_state"]["status"], "active")
        self.assertEqual(len(self.commands.engine.sent), 1)

    def test_tool_activity_has_stable_call_ids_and_no_duplicate_status_updates(self):
        conversation = self.conversation()
        sent = self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Status")
        for call in ("first_call", "second_call"):
            for status in ("running", "running", "completed", "completed", "running"):
                self.commands.chat.on_event({"type": "message.part.updated", "properties": {"sessionID": conversation, "part": {
                    "messageID": "assistant_" + sent["turn_id"], "callID": call, "type": "tool", "tool": "gleskit_workspace_status",
                    "state": {"status": status, "input": {}, "output": "Status"}}}})
        events = self.call("get_chat_conversation", conversationId=conversation)["turn_events"]
        tools = [row for row in events if row["event_type"].startswith("tool.")]
        self.assertEqual([row["event_type"] for row in tools], ["tool.started", "tool.finished"] * 2)
        self.assertEqual({row["payload"]["call_id"] for row in tools}, {"first_call", "second_call"})
        self.assertIn(conversation, self.commands.chat.active)

    def test_completion_is_one_stable_event_across_live_history_and_restart(self):
        conversation = self.conversation()
        sent = self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Hi there")
        assistant = self.commands.engine.messages[conversation][-1]
        assistant["info"].update(finish="stop", time={"created": 2000, "completed": 3000})
        self.commands.chat.on_event({"type": "message.updated", "properties": {"info": assistant["info"]}})
        def terminals():
            return [row for row in self.call("get_chat_conversation", conversationId=conversation)["turn_events"]
                    if row["event_type"] in {"turn.completed", "turn.failed", "turn.cancelled"}]
        first = terminals()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["event_id"], sent["turn_id"] + ":terminal")
        self.assertEqual(first, terminals(), "Reloading must not manufacture new completion timestamps or IDs")
        self.commands.engine.events.rows.clear()  # A disconnected browser can outlive the replay buffer.
        self.assertEqual(first, terminals())
        self.commands.chat = Chat(self.profile, self.commands.engine)
        self.assertEqual(first, terminals())
        self.assertEqual(len(self.commands.engine.sent), 1)

    def test_cancellation_and_failure_survive_replay_gaps_without_duplicate_terminals(self):
        conversation = self.conversation()
        for status in ("cancelled", "failed"):
            with self.subTest(status=status):
                self.call("send_chat_message", conversationId=conversation, providerId="fixture", modelId="vendor/test", agentId="gleskit", content="Status")
                self.commands.chat.finish(conversation, status, "Synthetic failure" if status == "failed" else None)
                self.commands.engine.events.rows.clear()
                detail = self.call("get_chat_conversation", conversationId=conversation)
                events = [row for row in detail["turn_events"] if row["event_type"].startswith("turn.")]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "turn." + status)
                self.assertEqual(detail["turn_state"]["status"], status)

    def test_path_identifiers_and_symlink_sessions_are_rejected(self):
        for value in ("../outside", "/tmp/file", "a/b", "..", "a\\b", "a%2fb"):
            with self.assertRaises(WebError): self.call("get_session_snapshot", sessionId=value)
        (self.profile.workspace / "sessions/session_fake").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(WebError): self.call("get_session_snapshot", sessionId="session_fake")

    def test_profile_does_not_adopt_or_overwrite_existing_data(self):
        old = self.root / "unrelated"
        old.mkdir(); (old / "results.txt").write_text("KEEP")
        with self.assertRaisesRegex(WebError, "existing directory"):
            WebProfile(ROOT, old)
        self.assertEqual((old / "results.txt").read_text(), "KEEP")
        with self.assertRaisesRegex(WebError, "already open"):
            WebProfile(ROOT, self.profile.data)

    def test_existing_workspace_can_only_be_browsed_read_only(self):
        old = WorkspaceStore(self.base.workspace_default_root)
        session = old.create_session(title="Existing result", description="Do not modify", species="human")
        original = {str(path.relative_to(old.workspace_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in old.workspace_root.rglob("*") if path.is_file()}
        self.call("browse_existing_workspace")
        self.assertTrue(self.call("bootstrap_workspace")["paths"]["read_only"])
        self.assertEqual(self.call("get_session_snapshot", sessionId=session.session_id)["summary"]["title"], "Existing result")
        for name, values in [("delete_sessions", {"sessionIds": [session.session_id]}),
                             ("update_session", {"sessionId": session.session_id, "title": "Changed"}),
                             ("create_session", {"title": "New", "description": "", "species": "human", "tags": []})]:
            with self.assertRaisesRegex(WebError, "read-only"): self.call(name, **values)
        self.call("write_runtime_heartbeat", activeSessionId=session.session_id, captureEnabled=True)
        self.assertFalse(self.commands.runtime.capture)
        after = {str(path.relative_to(old.workspace_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in old.workspace_root.rglob("*") if path.is_file()}
        self.assertEqual(original, after)
        self.call("use_web_workspace")
        self.assertEqual(self.call("bootstrap_workspace")["sessions"]["session_count"], 0)

    def test_only_registered_contained_files_are_delivered(self):
        image = self.profile.workspace / "fixture.png"
        image.write_bytes(b"synthetic-image")
        url = self.commands.files.register(image)
        self.assertEqual(self.commands.files.get(url.split("/")[-1]), image)
        outside = self.root / "private.txt"; outside.write_text("SECRET")
        with self.assertRaises(WebError): self.commands.files.register(outside)
        with self.assertRaises(WebError): self.commands.files.get("not-registered")
        image.unlink(); image.symlink_to(outside)
        with self.assertRaises(WebError): self.commands.files.get(url.split("/")[-1])

    def test_frontend_commands_have_explicit_backend_adapters(self):
        source = (ROOT / "apps/web/src/main.js").read_text()
        names = set(re.findall(r'invoke\("([^"\n]+)"', source))
        self.assertFalse(names - ALL_COMMANDS - {"open_external_url"})


class WebHttpTests(WebShellTests):
    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient
        self.ui = self.root / "ui"; self.ui.mkdir()
        (self.ui / "index.html").write_text("<!doctype html><title>Synthetic UI</title>")
        self.app = create_app(self.profile, self.ui, "http://127.0.0.1:5017", commands=self.commands)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:5017", raise_server_exceptions=False)
        self.client.__enter__()
        self.nonce = self.app.state.host.nonce
        self.login()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def login(self):
        response = self.client.post("/api/auth/bootstrap", json={"nonce": self.nonce}, headers={"Origin": "http://127.0.0.1:5017"})
        self.assertEqual(response.status_code, 200)
        self.headers = {"Origin": "http://127.0.0.1:5017", "X-GLESKit-CSRF": self.app.state.host.csrf,
                        "X-GLESKit-Tab": self.tab, "X-GLESKit-Request": "request_fixture"}

    def test_auth_host_origin_and_csrf_boundaries(self):
        self.assertEqual(self.client.get("/api/session").status_code, 200)
        self.assertEqual(self.client.get("/api/session", headers={"Host": "evil.example"}).status_code, 400)
        bad = {**self.headers, "Origin": "https://evil.example"}
        self.assertEqual(self.client.post("/api/commands/list_sessions", json={}, headers=bad).status_code, 403)
        bad = {**self.headers, "X-GLESKit-CSRF": "wrong"}
        self.assertEqual(self.client.post("/api/commands/list_sessions", json={}, headers=bad).status_code, 403)
        response = self.client.post("/api/auth/bootstrap", json={"nonce": self.nonce}, headers=self.headers)
        self.assertEqual(response.status_code, 401)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/session").status_code, 401)
        self.assertEqual(self.client.get("/api/events?tab=tab_a").status_code, 401)
        self.assertEqual(self.client.get("/api/files/secret").status_code, 401)

    def test_idempotency_does_not_create_a_second_session(self):
        values = {"title": "Once", "description": "", "species": "human", "tags": []}
        first = self.client.post("/api/commands/create_session", json=values, headers=self.headers)
        second = self.client.post("/api/commands/create_session", json=values, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.call("list_sessions")["session_count"], 1)
        values["title"] = "Different"
        self.assertEqual(self.client.post("/api/commands/create_session", json=values, headers=self.headers).status_code, 409)

    def test_http_draft_model_discovery_does_not_retain_key_or_change_provider(self):
        key = "synthetic-draft-catalog-key"
        real_client = httpx.Client
        with patch("gleskit_mcp.web.providers.httpx.Client", side_effect=lambda **kwargs: real_client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": "fixture/model"}]})), **kwargs)):
            response = self.client.post("/api/commands/fetch_provider_models", json={"kind": "openai", "apiKey": key}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], [{"id": "fixture/model", "label": "fixture/model"}])
        self.assertEqual(len(self.app.state.host.results), 0)
        self.assertEqual(self.commands.providers.list(), [])
        self.assertEqual(self.commands.engine.starts, 0)
        self.assertNotIn(key, json.dumps(response.json()))

    def test_upload_is_streamed_and_opaque(self):
        response = self.client.post("/api/uploads/stage", content=b"gene,value\nA,1\n", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        token = response.json()["staging_id"]
        path = self.commands.files.take_stage(token)
        self.assertEqual(path.read_bytes(), b"gene,value\nA,1\n")
        with self.assertRaises(WebError): self.commands.files.take_stage(token)

    def test_static_and_file_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("'unsafe-inline'", response.headers["content-security-policy"].split("script-src")[1].split(";")[0])
        self.assertEqual(self.client.get("/src/gleskit_mcp/config.py").status_code, 404)
        self.assertEqual(self.client.post("/api/commands/run_shell", json={}, headers=self.headers).status_code, 404)


class ConfiguredWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-configured-workspace-")
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "checkout"
        for relative in ("docs/opencode/skills/gleskit-research-assistant", "docs/opencode/prompts/agents"):
            shutil.copytree(ROOT / relative, self.project / relative)
        self.base = load_settings(self.project)
        self.store = WorkspaceStore(self.base.workspace_default_root)
        self.existing = self.store.create_session(title="Existing fixture", description="Preserve", species="human")
        self.original = self.hashes()
        self.profile = WebProfile(self.project, self.project / "var/local-web/profile")
        self.commands = Commands(self.profile, MemoryCredentials(), FakeEngine)
        self.commands.runtime.claim("fixture_tab")

    def tearDown(self):
        self.commands.close()
        self.temp.cleanup()

    def call(self, command, **values):
        return self.commands.execute(command, values, "fixture_tab")

    def hashes(self):
        return {str(path.relative_to(self.store.workspace_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.store.workspace_root.rglob("*") if path.is_file()}

    def test_normal_launch_reads_existing_sessions_without_rewriting_them(self):
        before = self.original
        bootstrap = self.call("bootstrap_workspace")
        self.assertEqual(bootstrap["paths"]["workspace_source"], "configured")
        self.assertFalse(bootstrap["paths"]["read_only"])
        self.assertEqual(bootstrap["sessions"]["session_count"], 1)
        self.assertEqual(self.call("get_session_snapshot", sessionId=self.existing.session_id)["summary"]["title"], "Existing fixture")
        self.assertIsNone(self.commands.runtime.active)
        self.assertFalse(self.commands.runtime.capture)
        self.assertEqual(self.commands.engine.starts, 0)
        self.assertEqual(before, self.hashes())

    def test_new_sessions_and_mcp_use_the_same_configured_root(self):
        before = self.hashes()
        created = self.call("create_session", title="New fixture", description="", species="human", tags=[])
        self.assertEqual(len(self.store.list_sessions()), 2)
        self.assertEqual(self.store.get_session(created["session_id"]).title, "New fixture")
        self.call("write_runtime_heartbeat", activeSessionId=created["session_id"], captureEnabled=True)
        environment = self.commands.engine.configuration()["mcp"]["gleskit"]["environment"]
        self.assertEqual(environment["GLESKIT_WORKSPACE_HOME"], str(self.store.workspace_root))
        manifest = json.loads(self.profile.heartbeat.read_text())
        self.assertEqual(manifest["workspace_root"], str(self.store.workspace_root))
        self.assertEqual(manifest["active_session_id"], created["session_id"])
        self.assertTrue(manifest["capture_enabled"])
        after = self.hashes()
        self.assertTrue(all(after[path] == digest for path, digest in before.items()))

    def test_workspace_choice_persists_and_switch_stops_only_the_owned_engine(self):
        before = self.hashes()
        self.commands.engine.running = True
        self.call("use_web_workspace")
        self.assertFalse(self.commands.engine.running)
        self.assertEqual(self.profile.workspace, self.profile.separate_workspace)
        self.assertEqual(self.call("list_sessions")["session_count"], 0)
        self.commands.close()
        self.profile = WebProfile(self.project, self.profile.data)
        self.assertEqual(self.profile.workspace_source, "separate")
        self.commands = Commands(self.profile, MemoryCredentials(), FakeEngine)
        self.commands.runtime.claim("fixture_tab")
        self.call("use_configured_workspace")
        self.assertEqual(self.call("list_sessions")["session_count"], 1)
        self.assertEqual(self.profile.workspace, self.store.workspace_root)
        self.assertEqual(before, self.hashes())
        self.commands.close()
        self.profile = WebProfile(self.project, self.profile.data)
        self.commands = Commands(self.profile, MemoryCredentials(), FakeEngine)
        self.assertEqual(self.profile.workspace_source, "configured")

    def test_custom_profile_stays_isolated_unless_explicitly_connected(self):
        profile = WebProfile(self.project, self.root / "custom-profile")
        try:
            self.assertEqual(profile.workspace_source, "separate")
            self.assertNotEqual(profile.workspace, self.store.workspace_root)
        finally:
            profile.close()
        profile = WebProfile(self.project, self.root / "custom-profile", workspace_source="configured")
        try:
            self.assertEqual(profile.workspace, self.store.workspace_root)
        finally:
            profile.close()

    def test_read_only_view_persists_and_disables_the_mcp_workspace(self):
        before = self.hashes()
        self.call("browse_existing_workspace")
        self.assertFalse(self.commands.runtime.snapshot()["available"])
        self.assertEqual(json.loads(self.profile.heartbeat.read_text())["connection_state"], "cleared")
        self.commands.close()
        self.profile = WebProfile(self.project, self.profile.data)
        self.commands = Commands(self.profile, MemoryCredentials(), FakeEngine)
        self.commands.runtime.claim("fixture_tab")
        self.assertTrue(self.call("bootstrap_workspace")["paths"]["read_only"])
        with self.assertRaisesRegex(WebError, "read-only"):
            self.call("delete_sessions", sessionIds=[self.existing.session_id])
        self.assertEqual(before, self.hashes())

    def test_fresh_checkout_has_no_bundled_sessions(self):
        project = self.root / "fresh-checkout"
        for relative in ("docs/opencode/skills/gleskit-research-assistant", "docs/opencode/prompts/agents"):
            shutil.copytree(ROOT / relative, project / relative)
        profile = WebProfile(project, project / "var/local-web/profile")
        commands = Commands(profile, MemoryCredentials(), FakeEngine)
        try:
            self.assertEqual(commands.workspace.list_sessions()["session_count"], 0)
            self.assertEqual(profile.workspace_source, "configured")
        finally:
            commands.close()


if __name__ == "__main__":
    unittest.main()
