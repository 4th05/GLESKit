from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import hashlib
from http.cookies import SimpleCookie
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from .commands import ALL_COMMANDS, READ_COMMANDS, Commands
from .common import WebError, contained_file, identifier, write_private

MAX_JSON = 16 * 1024 * 1024
MAX_FILE = 1024 * 1024 * 1024


async def body_json(request, limit=MAX_JSON):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise WebError("This endpoint requires JSON.", 415)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise WebError("Request too large. Use a file upload or linked file.", 413)
    try:
        value = json.loads(body)
    except ValueError:
        raise WebError("Invalid JSON request.") from None
    if not isinstance(value, dict):
        raise WebError("Request must be an object.")
    return value


def failure(message, status):
    return JSONResponse({"ok": False, "error": {"code": status, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"})


class Boundary:
    """Exact loopback Host/Origin, HttpOnly session and CSRF. No permissive CORS."""

    def __init__(self, app, host):
        self.app, self.host = app, host

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        host = scope["app"].state.host
        if request.headers.get("host") != host.authority:
            return await failure("Use the launcher's exact 127.0.0.1 URL.", 400)(scope, receive, send)
        path = scope["path"]
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != host.origin:
            return await failure("Requests from another website are not allowed.", 403)(scope, receive, send)
        if path.startswith("/api/") and path not in {"/api/auth/bootstrap", "/api/ping"}:
            cookie = request.cookies.get(host.cookie, "")
            if not secrets.compare_digest(cookie, host.session):
                return await failure("Open GLESKit using its local launcher to authenticate.", 401)(scope, receive, send)
            if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(request.headers.get("x-gleskit-csrf", ""), host.csrf):
                return await failure("The local session token is missing or expired. Reopen GLESKit.", 403)(scope, receive, send)
        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                            (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                            (b"content-security-policy", host.csp.encode()),
                            (b"x-gleskit-revision", str(host.commands.runtime.revision).encode())]
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, secure_send)


class Host:
    def __init__(self, commands, ui_root, origin):
        self.commands = commands
        self.ui_root = ui_root.resolve()
        self.origin = origin
        self.authority = origin.removeprefix("http://")
        if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", self.authority):
            raise WebError("The web host must be bound to an explicit loopback port.")
        self.instance = secrets.token_hex(16)
        self.nonce, self.session, self.csrf = (secrets.token_urlsafe(32) for _ in range(3))
        self.cookie = "gleskit_" + self.instance
        self.lock = asyncio.Lock()
        self.results = OrderedDict()
        self.stopping = False
        index = contained_file(self.ui_root / "index.html", self.ui_root).read_text()
        import base64
        hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"
                  for script in re.findall(r"<script>([\s\S]*?)</script>", index)]
        self.csp = ("default-src 'self'; script-src 'self' " + " ".join(hashes) +
                    "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
                    "connect-src 'self'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def handoff(self):
        write_private(self.commands.profile.data / "web-instance.json", {"schema": 1, "pid": os.getpid(),
                      "instance": self.instance, "origin": self.origin, "nonce": self.nonce})


def create_app(profile, ui_root: Path, origin: str, *, commands=None):
    controller = commands or Commands(profile)
    host = Host(controller, ui_root, origin)
    from .files import FigureDownloads
    figures = FigureDownloads(profile.data, origin)
    profile.figure_origin = origin

    @asynccontextmanager
    async def lifespan(app):
        host.handoff()
        try:
            yield
        finally:
            host.stopping = True
            await run_in_threadpool(controller.close)
            (profile.data / "web-instance.json").unlink(missing_ok=True)

    async def bootstrap(request):
        data = await body_json(request, 2048)
        async with host.lock:
            if set(data) != {"nonce"} or not isinstance(data["nonce"], str) or not secrets.compare_digest(data["nonce"], host.nonce):
                raise WebError("This launch link expired. Run the launcher again.", 401)
            host.nonce = secrets.token_urlsafe(32)
            host.handoff()
        response = JSONResponse({"ok": True})
        response.set_cookie(host.cookie, host.session, httponly=True, samesite="strict", path="/")
        return response

    async def session(request):
        return JSONResponse({"csrf": host.csrf, "instance": host.instance,
                             "runtime": controller.runtime.snapshot()})

    async def ping(request):
        return JSONResponse({"app": "gleskit-local-web", "instance": host.instance})

    async def control(request):
        values = await body_json(request, 2048)
        if set(values) - {"tab", "takeover"} or type(values.get("takeover", False)) is not bool:
            raise WebError("Invalid controller request.")
        result = controller.runtime.claim(values.get("tab"), values.get("takeover", False))
        return JSONResponse({**result, "runtime": controller.runtime.snapshot()})

    async def invoke(request):
        command = request.path_params["name"]
        if command not in ALL_COMMANDS:
            raise WebError("Unknown web command.", 404)
        values = await body_json(request)
        tab = identifier(request.headers.get("x-gleskit-tab"))
        revision = request.headers.get("x-gleskit-revision")
        if revision is not None and not revision.isdecimal():
            raise WebError("Invalid runtime revision.")
        expected = int(revision) if revision is not None else None

        async def execute():
            return await run_in_threadpool(controller.execute, command, values, tab, expected)

        if command in READ_COMMANDS:
            return JSONResponse({"ok": True, "result": await execute()})
        key = identifier(request.headers.get("x-gleskit-request"))
        fingerprint = hashlib.sha256(json.dumps([command, values], sort_keys=True).encode()).hexdigest()
        async with host.lock:
            existing = host.results.get(key)
            if existing:
                if existing[0] != fingerprint:
                    raise WebError("A request ID cannot be reused for different content.", 409)
                return JSONResponse(existing[1])
            try:
                result = {"ok": True, "result": await execute()}
            except Exception:
                # A failed mutating operation must never be automatically run twice.
                host.results[key] = (fingerprint, {"ok": False, "error": {"code": 409, "message": "This request already failed. Check current state before issuing a new request."}})
                raise
            host.results[key] = (fingerprint, result)
            if len(host.results) > 512:
                host.results.popitem(last=False)
        return JSONResponse(result)

    async def events(request):
        tab = identifier(request.query_params.get("tab"))
        value = request.headers.get("last-event-id", "0")
        if not value.isdecimal():
            raise WebError("Invalid event cursor.")
        cursor = int(value)
        async def stream():
            nonlocal cursor
            last_renew = 0
            last_revision = -1
            try:
                while not host.stopping and not await request.is_disconnected():
                    if time.monotonic() - last_renew > 5:
                        await run_in_threadpool(controller.runtime.renew, tab)
                        yield ": keepalive\n\n"
                        last_renew = time.monotonic()
                    runtime = controller.runtime.snapshot()
                    if runtime["revision"] != last_revision:
                        yield "event: gleskit-runtime\ndata: " + json.dumps({"runtime": runtime, "writable": controller.runtime.owner == tab}) + "\n\n"
                        last_revision = runtime["revision"]
                    gap, rows = controller.engine.events.after(cursor)
                    if gap:
                        yield 'event: gleskit-reconcile\ndata: {}\n\n'
                    for number, row in rows:
                        cursor = number
                        yield f"id: {number}\nevent: gleskit-chat\ndata: {json.dumps(row)}\n\n"
                    await asyncio.sleep(0.2)
            finally:
                controller.runtime.release(tab)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    async def stage(request):
        tab = identifier(request.headers.get("x-gleskit-tab"))
        controller.runtime.require(tab)
        token = "stage_" + secrets.token_hex(20)
        path = profile.staging / token
        size = 0
        try:
            with path.open("xb") as stream:
                path.chmod(0o600)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise WebError("Copied files are limited to 1 GiB. Link a larger local file instead.", 413)
                    await run_in_threadpool(stream.write, chunk)
            if size == 0:
                raise WebError("The selected file is empty.")
            with controller.files.lock:
                controller.files.staged[token] = path
            return JSONResponse({"staging_id": token, "bytes": size})
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def file(request):
        path = controller.files.get(request.path_params["token"])
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        inline = request.query_params.get("inline") == "1" and mime in {"image/png", "image/jpeg", "image/webp"}
        return FileResponse(path, media_type=mime, filename=path.name,
                            content_disposition_type="inline" if inline else "attachment")

    async def figure(request):
        path, mime, filename = figures.get(request.path_params["token"])
        inline = request.query_params.get("inline") == "1" and mime in {"image/png", "image/jpeg", "image/webp"}
        return FileResponse(path, media_type=mime, filename=filename,
                            content_disposition_type="inline" if inline else "attachment")

    async def static(request):
        name = request.path_params.get("path") or "index.html"
        if name.startswith(".") or "\\" in name or ".." in Path(name).parts:
            raise WebError("Invalid asset path.", 404)
        path = contained_file(host.ui_root / name, host.ui_root)
        return FileResponse(path)

    async def error(request, exc):
        if isinstance(exc, WebError):
            return failure(str(exc), exc.status)
        if isinstance(exc, FileNotFoundError):
            return failure("The selected local record or file was not found.", 404)
        if isinstance(exc, (KeyError, TypeError, ValueError)):
            return failure("Invalid command arguments or local record format.", 400)
        # Do not reflect provider errors, filesystem contents or tracebacks.
        return failure("The local operation failed. Existing results were not migrated or replaced.", 500)

    app = Starlette(routes=[Route("/api/auth/bootstrap", bootstrap, methods=["POST"]),
                           Route("/api/session", session), Route("/api/ping", ping),
                           Route("/api/control", control, methods=["POST"]),
                           Route("/api/commands/{name}", invoke, methods=["POST"]), Route("/api/events", events),
                           Route("/api/uploads/stage", stage, methods=["POST"]), Route("/api/files/{token}", file),
                           Route("/api/figures/{token}", figure),
                           Route("/{path:path}", static)], lifespan=lifespan,
                    exception_handlers={WebError: error, FileNotFoundError: error, ValueError: error,
                                        TypeError: error, KeyError: error, Exception: error})
    app.state.host = host
    app.add_middleware(Boundary, host=host)
    return app
