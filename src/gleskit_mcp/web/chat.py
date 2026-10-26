from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
import uuid

from .common import WebError, identifier, private_dir, read_json, write_private
from .instructions import prepare

TERMINAL_EVENTS = {"turn.completed", "turn.failed", "turn.cancelled"}
PROGRESS_INTERVAL = 0.75


def iso(milliseconds=None):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat() if milliseconds else datetime.now(timezone.utc).isoformat()


class Chat:
    def __init__(self, profile, engine):
        self.profile, self.engine = profile, engine
        self.path = profile.data / "conversations.json"
        self.rows = read_json(self.path, {})
        self.active = {}
        self.turns = {}
        self.approvals = {}
        self.prepared = {}
        self.state_path = profile.data / "chat-turn-state.json"
        self.states = read_json(self.state_path, {})
        self.last_raw = {}
        self.history_dir = private_dir(profile.data / "chat-history")
        self.progress_at = {}
        self.tool_states = {}
        self.lock = threading.RLock()
        self.engine.callback = self.on_event
        changed = False
        for row in self.states.values():
            if row.get("status") == "active":
                row.update(status="failed", created_at=iso(), error="The local chat host restarted. Reopen the conversation before retrying; no prompt was resent.")
                changed = True
        if changed:
            write_private(self.state_path, self.states)

    def recover(self):
        if not self.engine.status()["running"]:
            with self.lock:
                for conversation in list(self.active):
                    self.finish(conversation, "failed", "OpenCode stopped before the request completed. Use Reconnect; no prompt was resent.")

    def finish(self, conversation, status, error=None, message=None):
        # Terminal state survives replay gaps; never store raw provider errors.
        turn = self.active.pop(conversation, None)
        if not turn:
            return
        self.states[conversation] = {"turn_id": turn, "status": status, "error": error, "message_id": message, "created_at": iso()}
        write_private(self.state_path, self.states)
        self.progress_at.pop(conversation, None)
        self.tool_states = {key: value for key, value in self.tool_states.items() if key[0] != conversation}
        self.approvals = {key: value for key, value in self.approvals.items() if value != conversation}
        self.event(conversation, turn, "turn." + status, {"error": error} if error else {}, message)

    def idle(self):
        self.recover()
        with self.lock:
            if self.active:
                raise WebError("Finish or cancel the active chat turn before changing the engine/provider.", 409)

    def config(self):
        return {"system_prompt_text": self.profile.prompt.read_text(), "default_agent": "gleskit",
                "agents": [{"agent_id": "gleskit", "label": "GLESKit", "description": "Biomedical research assistant",
                            "is_default": True, "prompt_path": str(self.profile.prompt)}],
                "skills": [{"skill_id": "gleskit-research-assistant", "label": "GLESKit research assistant",
                            "content_markdown": (self.profile.skill / "SKILL.md").read_text(),
                            "enabled": True, "folder_name": str(self.profile.skill)}],
                "mcp_enabled": True, "prompt_restore_available": False, "restore_all_available": False}

    def bootstrap(self):
        provider = self.engine.providers.active()
        if self.engine.providers.connected(provider):
            self.engine.start()
        return {"paths": {"opencode_config_path": str(self.profile.engine_config),
                          "opencode_skill_root": str(self.profile.skill), "opencode_prompt_path": str(self.profile.prompt),
                          "server_url": "Managed authenticated loopback server" if self.engine.status()["running"] else ""},
                "conversations": list(self.rows.values()), "providers": self.engine.providers.list(),
                "gleskit_config": self.config(), "mcp_status": self.engine.mcp_status()}

    def create(self, title=None):
        self.engine.start()
        raw = self.engine.request("POST", "/session", {"title": (title or "New conversation")[:200]})
        key = identifier(raw["id"])
        row = {"conversation_id": key, "title": raw.get("title", title or "New conversation"),
               "created_at": iso(raw.get("time", {}).get("created")), "updated_at": iso(), "agent": "gleskit",
               "active_skill_ids": ["gleskit-research-assistant"], "tool_approval_mode": "ask"}
        with self.lock:
            self.rows[key] = row
            write_private(self.path, self.rows)
        return row

    def require(self, conversation):
        identifier(conversation)
        if conversation not in self.rows:
            raise WebError("This conversation does not belong to the web profile.", 404)

    def event(self, conversation, turn, kind, payload=None, message=None):
        result = {"event_id": turn + ":terminal" if kind in TERMINAL_EVENTS else uuid.uuid4().hex, "conversation_id": conversation, "turn_id": turn,
                  "message_id": message, "event_type": kind, "payload": payload or {}, "created_at": iso()}
        self.engine.events.publish(result)
        return result

    def send(self, conversation, provider, model, agent, content, runtime, *, pipeline=False, refs=(), extra=""):
        self.require(conversation)
        active_provider = self.engine.providers.active()
        if not active_provider or provider != active_provider["provider_id"] or model not in active_provider["models"]:
            raise WebError("Choose a model belonging to the active provider.")
        if agent != "gleskit" or not isinstance(content, str) or not content.strip() or len(content) > 128000:
            raise WebError("Select the GLESKit agent and enter a nonempty message (maximum 128,000 characters).")
        for ref in refs:
            identifier(ref)
        # Missing instructions fail BEFORE starting the engine or submitting text.
        system, files = prepare(self.profile, content, runtime, pipeline=pipeline, refs=refs, extra=extra)
        self.recover()
        with self.lock:
            if conversation in self.active:
                raise WebError("This conversation already has an active turn. Cancel it or wait.", 409)
            self.engine.start()
            self.engine.ensure_mcp()
            turn = "msg_" + uuid.uuid4().hex
            self.active[conversation] = turn
            self.states[conversation] = {"turn_id": turn, "status": "active", "error": None, "message_id": None}
            write_private(self.state_path, self.states)
            self.prepared[turn] = {"files": files, "sha256": hashlib.sha256(system.encode()).hexdigest()}
            self.event(conversation, turn, "turn.started", {"pipeline_mode": pipeline, "input_data_refs": list(refs)})
            self.event(conversation, turn, "turn.prompt_compiled", {"instruction_files": files, "system_prompt_len": len(system),
                                                                   "system_prompt_hash": self.prepared[turn]["sha256"]})
            body = {"messageID": turn, "agent": "gleskit", "model": {"providerID": provider, "modelID": model},
                    "system": system, "parts": [{"type": "text", "text": content}]}
            try:
                self.engine.request("POST", f"/session/{conversation}/prompt_async", body)
            except WebError:
                # Never blindly resend after a lost acknowledgement.
                try:
                    accepted = any(row.get("info", {}).get("id") == turn for row in
                                   self.engine.request("GET", f"/session/{conversation}/message"))
                except WebError:
                    accepted = False
                if not accepted:
                    self.finish(conversation, "failed", "Submission could not be confirmed. Reopen the conversation before retrying.")
                    raise
            self.rows[conversation].update({"provider_id": provider, "model_id": model, "updated_at": iso()})
            write_private(self.path, self.rows)
            return {"accepted": True, "conversation_id": conversation, "turn_id": turn}

    def detail(self, conversation):
        self.require(conversation)
        self.recover()
        history_warning = None
        raw = self.last_raw.get(conversation)
        if raw is None:
            raw = read_json(self.history_dir / (conversation + ".json"), None)
        if self.engine.status()["running"]:
            try:
                latest = self.engine.request("GET", f"/session/{conversation}/message")
            except WebError:
                if raw is None:
                    raise
                history_warning = "Showing the last locally saved messages. Reconnect Chat in Settings to load newer messages; no prompt was resent."
            else:
                # Store only the fields used to display this owned conversation,
                # never system prompts, engine configuration or hidden reasoning.
                latest = self.engine.redact([{"info": {key: (bool(value) if key == "error" else value) for key, value in item.get("info", {}).items()
                    if key in {"id", "role", "parentID", "time", "finish", "error", "providerID", "modelID"}},
                    "parts": [{"type": "text", "text": part.get("text", "")} if part.get("type") == "text" else
                              {"type": "tool", "id": part.get("id", ""), "callID": part.get("callID", part.get("id", "")), "tool": part.get("tool", "tool"),
                               "state": {key: value for key, value in part.get("state", {}).items() if key in {"input", "output"}}}
                              for part in item.get("parts", []) if part.get("type") in {"text", "tool"}]}
                    for item in latest if item.get("info", {}).get("role") in {"user", "assistant"}])
                if latest != raw:
                    write_private(self.history_dir / (conversation + ".json"), latest)
                raw = latest
                self.last_raw[conversation] = raw
        elif raw is None:
            history_warning = "This conversation's messages are stored in the local OpenCode engine. Activate a provider in Settings and reopen the conversation to load them. No messages were deleted."
        raw = raw or []
        messages, events, terminals = [], [], {}
        current_turn = ""
        with self.lock:
            for item in raw:
                info, parts = item.get("info", {}), item.get("parts", [])
                key = info.get("id", "")
                if info.get("role") == "user":
                    current_turn = key
                turn = key if info.get("role") == "user" else self.turns.get(info.get("parentID"), info.get("parentID") or current_turn)
                self.turns[key] = turn
                finished = bool(info.get("time", {}).get("completed") and info.get("finish") not in (None, "tool-calls", "unknown"))
                error = "The agent reported an error. Check provider/model configuration." if info.get("error") else None
                tools = [{"call_id": part.get("callID", part.get("id", "")), "name": part.get("tool", "tool"),
                          "args_summary": json.dumps(part.get("state", {}).get("input", {}))[:500],
                          "result_summary": str(part.get("state", {}).get("output", ""))[:1000], "provenance_summary": ""}
                         for part in parts if part.get("type") == "tool"]
                messages.append({"message_id": key, "conversation_id": conversation, "role": info.get("role", "assistant"),
                                 "turn_id": turn, "parent_message_id": info.get("parentID"),
                                 "content_markdown": "\n".join(part.get("text", "") for part in parts if part.get("type") == "text"),
                                 "tool_calls_json": json.dumps(tools) if tools else None, "finished": finished, "error": error,
                                 "created_at": iso(info.get("time", {}).get("created")),
                                 "provider_id": info.get("providerID"), "model_id": info.get("modelID")})
                if info.get("role") == "assistant" and (finished or error):
                    terminals[turn] = {"event_id": turn + ":terminal", "conversation_id": conversation, "turn_id": turn,
                                   "event_type": "turn.failed" if error else "turn.completed", "payload": {"error": error} if error else {},
                                   "message_id": key, "created_at": iso(info.get("time", {}).get("completed"))}
                    if self.active.get(conversation) == turn:
                        self.finish(conversation, "failed" if error else "completed", error, key)
            for number, event in self.engine.events.after(0)[1]:
                if event["conversation_id"] == conversation:
                    if event["event_type"] in TERMINAL_EVENTS:
                        terminals[event["turn_id"]] = event
                    elif event["event_type"] != "turn.progress":
                        # Progress invalidates the live snapshot; it is not an
                        # activity step and must never enter replayed timelines.
                        events.append(event)
            state = self.states.get(conversation, {}).copy()
            if state.get("status") in {"failed", "cancelled", "completed"}:
                previous = terminals.get(state["turn_id"], {})
                terminals[state["turn_id"]] = {"event_id": state["turn_id"] + ":terminal", "conversation_id": conversation,
                               "turn_id": state["turn_id"], "event_type": "turn." + state["status"],
                               "message_id": state.get("message_id"), "payload": {"error": state["error"]} if state.get("error") else {},
                               "created_at": state.get("created_at") or previous.get("created_at") or self.rows[conversation]["updated_at"]}
            events.extend(terminals.values())
        return {"conversation": self.rows[conversation], "messages": messages, "turn_events": events,
                "turn_state": state, "engine_running": self.engine.status()["running"], "history_warning": history_warning}

    def rename(self, conversation, title):
        self.require(conversation)
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise WebError("Enter a conversation title (maximum 200 characters).")
        self.engine.request("PATCH", f"/session/{conversation}", {"title": title})
        with self.lock:
            self.rows[conversation]["title"] = title
            write_private(self.path, self.rows)
        return self.rows[conversation]

    def delete(self, conversation):
        self.require(conversation)
        if conversation in self.active:
            raise WebError("Cancel the active turn before deleting its conversation.", 409)
        self.engine.request("DELETE", f"/session/{conversation}")
        with self.lock:
            self.rows.pop(conversation)
            self.states.pop(conversation, None)
            self.last_raw.pop(conversation, None)
            (self.history_dir / (conversation + ".json")).unlink(missing_ok=True)
            write_private(self.path, self.rows)
            write_private(self.state_path, self.states)
        return {"deleted": conversation}

    def cancel(self, conversation):
        self.require(conversation)
        try:
            if self.engine.status()["running"]:
                self.engine.request("POST", f"/session/{conversation}/abort", {})
        except WebError:
            # An unacknowledged abort is not a confirmed cancellation. Stop
            # only the owned engine/relay rather than leave hidden work running.
            self.engine.stop()
        with self.lock:
            self.finish(conversation, "cancelled")
        self.recover()
        return self.rows[conversation]

    def approve(self, approval, action):
        identifier(approval)
        if approval not in self.approvals:
            raise WebError("This permission request is no longer active.", 409)
        if action not in {"allow", "allow_once", "allow_always", "reject", "deny"}:
            raise WebError("Invalid approval action.")
        conversation = self.approvals[approval]
        self.engine.request("POST", f"/session/{conversation}/permissions/{approval}",
                            {"response": "reject" if action in {"reject", "deny"} else "always" if action == "allow_always" else "once", "remember": action == "allow_always"})
        self.approvals.pop(approval, None)
        if action == "allow_always":
            with self.lock:
                self.rows[conversation]["tool_approval_mode"] = "allow_always"
                write_private(self.path, self.rows)
        return self.rows[conversation]

    def reset_approval(self, conversation):
        self.require(conversation)
        self.engine.request("PATCH", f"/session/{conversation}", {"permission": []})
        with self.lock:
            self.rows[conversation]["tool_approval_mode"] = "ask"
            write_private(self.path, self.rows)
        return self.rows[conversation]

    def on_event(self, value):
        value = value.get("payload", value)
        kind, payload = value.get("type", ""), value.get("properties", {})
        if kind == "gleskit.engine.stopped":
            self.recover()
            return
        if kind == "gleskit.mcp.status":
            self.event("", "", "system.mcp_status", payload)
            return
        info, part = payload.get("info", {}), payload.get("part", {})
        conversation = payload.get("sessionID") or info.get("sessionID") or part.get("sessionID")
        if conversation not in self.rows:
            return
        with self.lock:
            current = self.active.get(conversation)
            if info.get("id"):
                self.turns[info["id"]] = self.turns.get(info.get("parentID"), info.get("parentID") or info["id"])
            turn = self.turns.get(info.get("id") or part.get("messageID"), current)
            if not current or turn != current:
                return
            if kind in {"permission.asked", "permission.requested", "permission.request"}:
                approval = identifier(payload.get("id"))
                self.approvals[approval] = conversation
                self.event(conversation, turn, "tool.approval_requested", {"approval_id": approval,
                           "tool_name": payload.get("permission", "tool"), "args_summary": str(payload.get("patterns", [])),
                           "args_json": json.dumps(payload)})
            elif kind in {"permission.replied", "permission.responded"}:
                self.event(conversation, turn, "tool.approval_resolved", payload)
            elif kind == "session.error":
                self.finish(conversation, "failed", "Agent/provider failure. Check the provider connection, model and limits. No prompt was automatically resent.")
            elif kind == "session.status" and payload.get("status", {}).get("type") == "retry":
                # Never forward OpenCode's raw provider error/backoff message.
                self.event(conversation, turn, "turn.retrying", {"message": "OpenCode is waiting to retry a failed provider request. Cancel to stop; check the provider's availability and limits."})
            elif kind == "message.updated" and info.get("role") == "assistant" and info.get("time", {}).get("completed") and (info.get("error") or info.get("finish") not in (None, "tool-calls", "unknown")):
                self.finish(conversation, "failed" if info.get("error") else "completed",
                            "The agent reported an error. Check the model connection." if info.get("error") else None, info.get("id"))
            elif kind == "message.part.updated" and part.get("type") == "tool":
                state = part.get("state", {})
                status = state.get("status")
                if status in {"running", "completed", "error"}:
                    call = part.get("callID") or part.get("id")
                    if call:
                        key = (conversation, turn, call)
                        previous = self.tool_states.get(key)
                        if previous == status or previous in {"completed", "error"}:
                            return
                        self.tool_states[key] = status
                    self.event(conversation, turn, {"running": "tool.started", "completed": "tool.finished", "error": "tool.failed"}[status],
                               {"call_id": call, "tool_name": part.get("tool", "tool"), "args_summary": json.dumps(state.get("input", {}))[:500],
                                "result_summary": str(state.get("output", ""))[:2000]}, part.get("messageID"))
            elif kind.startswith("message."):
                now = time.monotonic()
                previous = self.progress_at.get(conversation)
                if previous is None or now - previous >= PROGRESS_INTERVAL:
                    self.progress_at[conversation] = now
                    self.event(conversation, turn, "turn.progress", {}, info.get("id") or part.get("messageID"))

    def stop(self):
        self.idle()
        return self.engine.stop()

    def close(self):
        with self.lock:
            for conversation in list(self.active):
                self.finish(conversation, "failed", "The local chat host stopped. No prompt was resent.")
        self.engine.close()
