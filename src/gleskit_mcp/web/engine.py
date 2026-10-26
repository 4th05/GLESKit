from __future__ import annotations

from collections import deque
import json
import socket
import subprocess
import threading
import time

import httpx

from .common import WebError, write_private
from .providers import CATALOG
from .redaction import SecretFilter
from .relay import ProviderRelay
from .lifecycle import stop_child


PRIVACY_ENV = {
    "OPENCODE_DISABLE_SHARE": "true", "OPENCODE_AUTO_SHARE": "false",
    "OPENCODE_DISABLE_AUTOUPDATE": "true", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
    "OPENCODE_PURE": "true", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
    "OPENCODE_DISABLE_CLAUDE_CODE": "true", "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "true", "OPENCODE_DISABLE_MODELS_FETCH": "true",
    "OTEL_SDK_DISABLED": "true", "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
}


class EventBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.sequence = 0
        self.rows = deque(maxlen=512)

    def publish(self, event):
        # Avoid retaining unbounded tool output in the replay buffer.
        if len(json.dumps(event)) > 128 * 1024:
            event = {**event, "payload": {"message": "Large activity output is available in the conversation."}}
        with self.lock:
            self.sequence += 1
            self.rows.append((self.sequence, event))

    def after(self, sequence):
        with self.lock:
            gap = bool(self.rows and sequence and sequence < self.rows[0][0] - 1)
            return gap, [(number, row) for number, row in self.rows if number > sequence]


class Engine:
    def __init__(self, profile, providers):
        import secrets
        self.profile, self.providers = profile, providers
        self.password = secrets.token_urlsafe(32)
        self.process = None
        self.url = ""
        self.lock = threading.RLock()
        self.known_secrets = {self.password}
        self.client = httpx.Client(trust_env=False, timeout=15, auth=("gleskit", self.password), follow_redirects=False)
        self.events = EventBus()
        self.callback = lambda event: None
        self.listener = None
        self.stopping = threading.Event()
        self.relay = None
        self._mcp_checked = 0
        self._mcp_thread = None
        self._mcp_ready = threading.Event()
        self._mcp = {"running": False, "transport": "stdio", "connection_state": "stopped", "error": None}

    def redact(self, value):
        redact = SecretFilter((*self.known_secrets.copy(), "gleskit:" + self.password))
        return redact.text(value) if isinstance(value, str) else redact.value(value)

    def status(self):
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {"running": running, "available": bool(self.profile.opencode),
                    "pid": self.process.pid if running else None, "transport": "stdio",
                    "error": "OpenCode stopped unexpectedly. Use Reconnect to restart it." if self.process is not None and not running else
                             None if self.profile.opencode else "OpenCode is not installed. Set --opencode to its executable."}

    def mcp_status(self, *, refresh=False):
        if not self.status()["running"]:
            return {"running": False, "transport": "stdio", "connection_state": "stopped", "error": None}
        with self.lock:
            if (not self._mcp_thread or not self._mcp_thread.is_alive()) and (
                    not self._mcp_checked or time.monotonic() - self._mcp_checked > (5 if refresh else 30)):
                self._mcp_ready.clear()
                # Refreshing a recent successful probe is not a disconnect.
                # Otherwise Send can turn a healthy connection into a transient
                # 503 merely by crossing the cache's refresh interval.
                if not self._mcp.get("running"):
                    self._mcp = {"running": False, "transport": "stdio", "connection_state": "connecting", "error": None}
                self._mcp_thread = threading.Thread(target=self._check_mcp, args=(self.url, self.stopping, self._mcp_ready), daemon=True)
                self._mcp_thread.start()
        return self._mcp.copy()

    def _check_mcp(self, url, stopping, ready):
        try:
            # First startup can build the plotting stack's font cache. This
            # bounded probe runs once in the background, never in UI metadata.
            response = self.client.get(url + "/mcp", timeout=120)
            response.raise_for_status()
            payload = response.json()
            state = payload.get("gleskit", {}).get("status", "missing") if isinstance(payload, dict) else "missing"
            if state not in {"connected", "disabled", "failed", "needs_auth", "needs_client_registration", "missing"}:
                state = "unreachable"
            result = {"running": state == "connected", "transport": "stdio", "connection_state": state,
                      "error": None if state == "connected" else "GLESKit MCP is not connected. Use Reconnect before sending a request."}
        except (httpx.HTTPError, ValueError, AttributeError):
            result = {"running": False, "transport": "stdio", "connection_state": "unreachable",
                      "error": "Cannot verify the GLESKit MCP connection. No message was submitted."}
        if not stopping.is_set():
            self._mcp, self._mcp_checked = result, time.monotonic()
            ready.set()
            self.callback({"type": "gleskit.mcp.status", "properties": result})
        else:
            ready.set()

    def ensure_mcp(self):
        state = self.mcp_status(refresh=True)
        if state.get("connection_state") == "connecting":
            raise WebError("Local GLESKit tools are still connecting. Wait for the ready indicator before sending; no prompt was submitted.", 503)
        if not state["running"]:
            raise WebError(state.get("error") or "GLESKit MCP is stopped. Reconnect before sending a request.", 503)

    def configuration(self):
        row = self.providers.active()
        provider = {}
        if row:
            options = {"apiKey": "{env:GLESKIT_RELAY_TOKEN}",
                       "baseURL": (self.relay.url if self.relay else "http://127.0.0.1:0") + "/v1"}
            provider[row["provider_id"]] = {"npm": "@ai-sdk/anthropic" if row["kind"] == "anthropic" else "@ai-sdk/openai-compatible",
                                            "name": row["label"], "options": options,
                                            "models": {model: {"name": model} for model in row["models"]}}
            if row["kind"] == "openai":
                provider[row["provider_id"]]["npm"] = "@ai-sdk/openai"
        child_env = self.profile.environment()
        # The provider key and local HTTP password must never reach MCP.
        child_env.update({"GLESKIT_PROVIDER_API_KEY": "", "GLESKIT_RELAY_TOKEN": "", "OPENCODE_SERVER_PASSWORD": "", "OPENCODE_SERVER_USERNAME": ""})
        result = {"$schema": "https://opencode.ai/config.json", "share": "disabled", "autoupdate": False,
                  "plugin": [], "default_agent": "gleskit", "provider": provider,
                  "agent": {"gleskit": {"description": "GLESKit biomedical research assistant", "mode": "all",
                      "prompt": "{file:" + str(self.profile.prompt) + "}",
                      "tools": {name: False for name in ("bash", "edit", "glob", "grep", "list", "lsp", "task", "webfetch", "websearch", "codesearch")},
                      "permission": {"*": "deny", "skill": {"*": "deny", "gleskit-research-assistant": "allow"},
                                     "read": {"*": "deny", str(self.profile.skill) + "/**": "allow"},
                                     "external_directory": {"*": "deny", str(self.profile.skill) + "/**": "allow"},
                                     "gleskit_*": "allow"}}},
                  "mcp": {"gleskit": {"type": "local", "command": self.profile.mcp_command(),
                                      "environment": child_env, "enabled": True, "timeout": 300000}}}
        if row:
            result["model"] = row["provider_id"] + "/" + row["default_model"]
            result["small_model"] = result["model"]
            result["enabled_providers"] = [row["provider_id"]]
        return result

    def start(self):
        with self.lock:
            if self.status()["running"]:
                return self.status()
            if self.process is not None:
                raise WebError("OpenCode stopped. Use Reconnect once to restart it.", 409)
            row = self.providers.active()
            if not self.providers.connected(row):
                raise WebError("Configure and activate a provider with an API key in Settings first.", 409)
            if not self.profile.opencode:
                raise WebError("OpenCode is missing. Install it or set --opencode to its executable.")
            env = self.profile.environment()
            secret = self.providers.credentials.get(row["provider_id"])
            if secret:
                self.known_secrets.add(secret)
            row["endpoint"] = row.get("endpoint") or CATALOG[row["kind"]]["endpoint"]
            self.relay = ProviderRelay(row, secret)
            try:
                self.relay.start()
                self.known_secrets.add(self.relay.token)
                env.update({"GLESKIT_RELAY_TOKEN": self.relay.token, "OPENCODE_SERVER_PASSWORD": self.password,
                            "OPENCODE_SERVER_USERNAME": "gleskit"})
                write_private(self.profile.engine_config, self.configuration())
                # OpenCode cannot accept a pre-bound socket. Never attach to
                # an unrelated listener if it loses the startup port race.
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
            except (OSError, WebError):
                self.stop()
                raise WebError("Cannot prepare the isolated local engine and credential relay.", 503) from None
            self.url = f"http://127.0.0.1:{port}"
            self.stopping = threading.Event()
            stopping = self.stopping
            try:
                self.process = subprocess.Popen([self.profile.opencode, "serve", "--hostname=127.0.0.1", f"--port={port}"],
                                                cwd=self.profile.home, env=env, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and self.process.poll() is None:
                    try:
                        if self.client.get(self.url + "/global/health", timeout=0.5).is_success:
                            self.listener = threading.Thread(target=self._listen, args=(stopping, self.url), daemon=True)
                            self.listener.start()
                            threading.Thread(target=self._watch, args=(self.process, stopping), daemon=True).start()
                            self.mcp_status(refresh=True)
                            return self.status()
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
            except OSError:
                self.stop()
                raise WebError("Cannot launch the configured OpenCode executable.") from None
            self.stop()
            raise WebError("OpenCode did not become ready within 30 seconds. Check its installation and use Start explicitly.", 503)

    def request(self, method, path, body=None):
        if not self.status()["running"]:
            raise WebError("OpenCode is stopped. Start it in Settings or use Retry Chat.", 409)
        try:
            response = self.client.request(method, self.url + path, json=body)
        except httpx.HTTPError:
            raise WebError("The local OpenCode request failed. No message was automatically resent.", 503) from None
        if not response.is_success:
            raise WebError(f"OpenCode returned HTTP {response.status_code}; provider error details were not copied to the UI.", 502)
        if not response.content:
            return None
        try:
            return self.redact(response.json())
        except ValueError:
            raise WebError("OpenCode returned an invalid response.", 502) from None

    def _watch(self, child, stopping):
        child.wait()
        if not stopping.is_set():
            self.callback({"type": "gleskit.engine.stopped", "properties": {}})

    def _listen(self, stopping, url):
        while not stopping.is_set():
            try:
                with self.client.stream("GET", url + "/event", timeout=30) as response:
                    response.raise_for_status()
                    data = []
                    for line in response.iter_lines():
                        if stopping.is_set():
                            return
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            try:
                                self.callback(self.redact(json.loads("\n".join(data))))
                            except (ValueError, WebError):
                                pass
                            data = []
            except (httpx.HTTPError, RuntimeError):
                stopping.wait(1)

    def stop(self):
        with self.lock:
            self.stopping.set()
            child, self.process = self.process, None
            # Snapshot descendants BEFORE signalling OpenCode: R workers may
            # own separate process groups and outlive a group-only kill.
            stop_child(child)
            if self.listener and self.listener is not threading.current_thread():
                self.listener.join(timeout=2)
            self.listener = None
            self.url = ""
            if self.relay:
                self.relay.stop()
                self.relay = None
            self._mcp_checked = 0
            self._mcp = {"running": False, "transport": "stdio", "connection_state": "stopped", "error": None}
            self._mcp_ready.set()
            self._mcp_ready = threading.Event()
            self._mcp_thread = None
        return self.status()

    def close(self):
        self.stop()
        self.client.close()
