"""Fixed-destination, authenticated loopback provider relay.

Real keys stay in this Python process. OpenCode receives only a revocable local
token. No upstream error body/header is ever forwarded or logged.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import socket
import threading
from copy import deepcopy

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .common import WebError
from .redaction import SecretFilter, StreamFilter

MAX_BODY = 16 * 1024 * 1024
MAX_EVENT = 1024 * 1024
ROUTES = {"openai": {"responses", "chat/completions"}, "anthropic": {"messages"},
          "openrouter": {"chat/completions"}, "openai-compatible": {"chat/completions"}, "local": {"chat/completions"}}


def safe_error(message, status=502):
    return JSONResponse({"error": {"message": message, "type": "gleskit_provider_error"}}, status_code=status,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


class ProviderRelay:
    def __init__(self, row, secret, *, transport=None):
        self.row, self.secret = row.copy(), secret
        self.token = secrets.token_urlsafe(32)
        self.redact = SecretFilter((secret, self.token))
        self.transport = transport
        self.url, self.server, self.thread, self.socket = "", None, None, None
        self.loop, self.client = None, None
        self.started = threading.Event()

    def app(self):
        @asynccontextmanager
        async def lifespan(app):
            self.loop = asyncio.get_running_loop()
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=self.transport,
                                        timeout=httpx.Timeout(120, connect=10, pool=10), limits=httpx.Limits(max_connections=8)) as client:
                self.client = client
                self.started.set()
                yield
            self.client = None

        return Starlette(routes=[Route("/v1/{path:path}", self.forward, methods=["POST"])], lifespan=lifespan)

    def upstream_headers(self, incoming):
        headers = {"Content-Type": "application/json", "Accept": incoming.get("accept", "application/json")}
        if self.row["kind"] == "anthropic":
            headers["anthropic-version"] = incoming.get("anthropic-version", "2023-06-01")
            if incoming.get("anthropic-beta"):
                headers["anthropic-beta"] = incoming["anthropic-beta"]
            if self.secret:
                headers["x-api-key"] = self.secret
        elif self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        if self.row["kind"] == "openrouter":
            headers["X-Title"] = "GLESKit"
        return headers

    async def forward(self, request: Request):
        # Browsers never need relay access. Reject Origin even with a valid key.
        if request.headers.get("origin") or request.headers.get("host") != self.url.removeprefix("http://"):
            return safe_error("Invalid local relay origin.", 403)
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + self.token):
            # Anthropic SDK sends its local capability in x-api-key instead.
            if self.row["kind"] != "anthropic" or not secrets.compare_digest(request.headers.get("x-api-key", ""), self.token):
                return safe_error("Invalid local relay credential.", 401)
        path = request.path_params["path"]
        if path not in ROUTES[self.row["kind"]] or request.url.query:
            return safe_error("Unsupported provider operation.", 404)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BODY:
                return safe_error("Provider request exceeds the local size limit.", 413)
        try:
            body = json.loads(data)
            if not isinstance(body, dict) or body.get("model") not in self.row["models"]:
                return safe_error("The model is not enabled for this connection.", 400)
            url = self.row["endpoint"].rstrip("/") + "/" + path
            response = await self.client.send(self.client.build_request("POST", url, content=bytes(data),
                                               headers=self.upstream_headers(request.headers)), stream=True)
        except (ValueError, httpx.HTTPError):
            return safe_error("Cannot reach the selected model provider. Check its endpoint and connection.")
        if not response.is_success:
            status = response.status_code if response.status_code in {400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504} else 502
            await response.aclose()
            return safe_error(f"Model provider returned HTTP {status}. Check the provider, key, model and limits.", status)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type == "text/event-stream":
            return StreamingResponse(self.stream(response), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
        try:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BODY:
                    return safe_error("Provider response exceeds the local size limit.")
            if content_type != "application/json":
                return safe_error("Provider returned an unsupported response format.")
            return JSONResponse(self.redact.value(json.loads(data)), headers={"Cache-Control": "no-store"})
        except (ValueError, httpx.HTTPError):
            return safe_error("Provider returned an invalid or interrupted response.")
        finally:
            await response.aclose()

    def frame(self, value, event=""):
        prefix = "event: " + event + "\n" if event else ""
        return prefix + "data: " + json.dumps(self.redact.value(value), ensure_ascii=True) + "\n\n"

    async def stream(self, response):
        sanitizer = StreamFilter(self.redact)
        data, event, size, complete = [], "", 0, False
        try:
            async for line in response.aiter_lines():
                size += len(line.encode())
                if size > MAX_EVENT:
                    raise ValueError("Oversized SSE event")
                if line.startswith("event:"):
                    event = line[6:].strip()
                    if not event.replace(".", "").replace("_", "").replace("-", "").isalnum() or len(event) > 80:
                        raise ValueError("Invalid SSE event")
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line:
                    text = "\n".join(data)
                    if text == "[DONE]":
                        complete = True
                        for pending in sanitizer.flush():
                            yield self.frame(pending, pending.get("type", "") if event else "")
                        yield "data: [DONE]\n\n"
                    elif data:
                        payload = json.loads(text)
                        if not isinstance(payload, dict) or payload.get("error") or payload.get("type") in {"error", "response.failed"}:
                            raise ValueError("Provider SSE error")
                        terminal = (any(choice.get("finish_reason") for choice in payload.get("choices", [])) or
                                    payload.get("type") in {"message_stop", "content_block_stop", "response.completed"})
                        complete = complete or bool(any(choice.get("finish_reason") for choice in payload.get("choices", [])) or
                                                    payload.get("type") in {"message_stop", "response.completed"})
                        cleaned = sanitizer.clean(payload)
                        # A final chat chunk may contain text as well as its
                        # finish reason. Emit text, then held prefixes, then the
                        # terminal marker, preserving concatenation order.
                        if terminal and any(choice.get("delta") for choice in cleaned.get("choices", [])):
                            progress = deepcopy(cleaned)
                            for choice in progress["choices"]:
                                choice["finish_reason"] = None
                            yield self.frame(progress, event)
                            for choice in cleaned["choices"]:
                                choice["delta"] = {}
                        if terminal:
                            for pending in sanitizer.flush():
                                yield self.frame(pending, pending.get("type", "") if event else "")
                        yield self.frame(cleaned, event)
                    else:
                        yield ": keepalive\n\n"
                    data, event, size = [], "", 0
            if data or not complete:
                raise ValueError("Truncated SSE response")
            for pending in sanitizer.flush():
                yield self.frame(pending, pending.get("type", ""))
        except (ValueError, httpx.HTTPError):
            message = "The model provider stream failed or was interrupted. No prompt was resent by GLESKit."
            payload = {"error": {"type": "api_error", "message": message}}
            if self.row["kind"] == "anthropic":
                payload["type"] = "error"
            elif self.row["kind"] == "openai":
                payload = {"type": "error", "code": "gleskit_provider_error", "message": message, "param": None}
            yield self.frame(payload, "error")
        finally:
            await response.aclose()

    def start(self):
        if self.thread:
            return
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(32)
        self.socket.setblocking(False)
        self.url = "http://127.0.0.1:" + str(self.socket.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(self.app(), access_log=False, log_level="critical", timeout_graceful_shutdown=1))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve(sockets=[self.socket])), daemon=True)
        self.thread.start()
        if not self.started.wait(5):
            self.stop()
            raise WebError("The local credential relay could not start.", 503)

    def stop(self):
        # Revoke immediately, then close any in-flight upstream requests.
        self.token = secrets.token_urlsafe(32)
        if self.client and self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.client.aclose(), self.loop).result(timeout=3)
            except Exception:
                pass
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=4)
        if self.socket:
            self.socket.close()
        self.secret = None
        self.url = ""
