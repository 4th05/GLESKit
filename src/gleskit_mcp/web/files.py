from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
import time

from .common import WebError, contained_file, identifier, private_dir, read_json, write_private


class FigureDownloads:
    """Private cross-process byte handoff, served only behind the UI's boundary."""

    MIME_TYPES = {"image/png", "image/svg+xml", "image/jpeg", "image/webp", "application/pdf",
                  "text/csv", "text/plain", "application/json", "application/octet-stream"}

    def __init__(self, profile, origin):
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", origin):
            raise WebError("Figure downloads require the local UI origin.")
        self.root = private_dir(Path(profile) / "figure-downloads")
        self.origin = origin

    @staticmethod
    def token(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", value):
            raise WebError("This figure link is invalid or expired.", 404)
        return value

    def store(self, token, data, mime_type, filename, expires_at):
        token = self.token(token)
        if mime_type not in self.MIME_TYPES or len(data) > 64 * 1024 * 1024:
            raise WebError("Unsupported or oversized figure download.")
        # Prune only our own generated, expired records; never workspace results.
        for meta in self.root.glob("*.json"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{24}\.json", meta.name) or meta.is_symlink():
                continue
            try:
                if read_json(meta)["expires_at"] <= time.time():
                    (self.root / (meta.stem + ".bin")).unlink(missing_ok=True)
                    meta.unlink(missing_ok=True)
            except (OSError, KeyError, TypeError, WebError):
                continue
        records = sorted((meta for meta in self.root.glob("*.json")
                          if re.fullmatch(r"[A-Za-z0-9_-]{24}\.json", meta.name) and not meta.is_symlink()),
                         key=lambda meta: meta.stat().st_mtime)
        for meta in records[:-255]:
            (self.root / (meta.stem + ".bin")).unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
        fd = os.open(self.root / (token + ".bin"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            write_private(self.root / (token + ".json"), {"mime": mime_type,
                          "filename": Path(filename).name[:200], "expires_at": expires_at})
        except BaseException:
            (self.root / (token + ".bin")).unlink(missing_ok=True)
            raise

    def get(self, token):
        token = self.token(token)
        try:
            meta = contained_file(self.root / (token + ".json"), self.root)
            payload = contained_file(self.root / (token + ".bin"), self.root)
            for path in (meta, payload):
                info = path.stat()
                if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError()
            if meta.stat().st_size > 4096 or payload.stat().st_size > 64 * 1024 * 1024:
                raise ValueError()
            value = read_json(meta)
            if (value["expires_at"] <= time.time() or value["mime"] not in self.MIME_TYPES
                    or not isinstance(value["filename"], str) or Path(value["filename"]).name != value["filename"]):
                raise ValueError()
            return payload, value["mime"], value["filename"]
        except (OSError, KeyError, TypeError, ValueError, WebError):
            raise WebError("This figure link is invalid or expired.", 404) from None

    def url(self, token):
        return self.origin + "/api/figures/" + self.token(token)


class Files:
    def __init__(self, profile):
        self.profile = profile
        self.lock = threading.Lock()
        self.registered = {}
        self.staged = {}
        self.linked = set()
        self.read_roots = {profile.workspace.resolve()}

    def _workspace_file(self, path):
        for root in self.read_roots:
            try:
                return contained_file(Path(path), root)
            except WebError:
                continue
        raise WebError("File is outside the explicitly opened workspaces.", 403)

    def register(self, path, *, linked=False):
        path = Path(path)
        if linked:
            path = path.resolve()
            if path not in self.linked or not path.is_file():
                raise WebError("This external file has not been explicitly linked.", 403)
        else:
            path = self._workspace_file(path)
        with self.lock:
            token = secrets.token_urlsafe(24)
            self.registered[token] = path
        return "/api/files/" + token

    def get(self, token):
        with self.lock:
            path = self.registered.get(token)
        if path is None:
            raise WebError("This file link expired or is not registered.", 404)
        if path not in self.linked:
            path = self._workspace_file(path)
        elif path.is_symlink() or path.resolve() != path:
            raise WebError("The explicitly linked file has changed location.", 403)
        if not path.is_file():
            raise WebError("The linked file is no longer available.", 404)
        return path

    def link(self, source):
        if not isinstance(source, str) or not Path(source).is_absolute():
            raise WebError("Linked files require an explicit absolute path.")
        candidate = Path(source)
        if candidate.is_symlink() or not candidate.is_file():
            raise WebError("Linked input must be a regular file, not a symlink or directory.")
        candidate = candidate.resolve()
        with self.lock:
            self.linked.add(candidate)
        return candidate

    def take_stage(self, token):
        identifier(token)
        with self.lock:
            value = self.staged.pop(token, None)
        if value is None:
            raise WebError("This staged upload expired or was already used.")
        return contained_file(value, self.profile.staging)

    def input(self, values, prefix=""):
        key = lambda suffix: prefix + suffix if prefix else suffix[0].lower() + suffix[1:]
        mode = values.get(key("SourceMode"))
        if mode == "linked":
            return self.link(values.get(key("SourcePath"))), False
        if mode != "copied":
            raise WebError("Select copied or linked input mode.")
        staged = values.get(key("StagingId"))
        if staged:
            return self.take_stage(staged), True
        encoded = values.get(key("DataB64"))
        if not isinstance(encoded, str) or len(encoded) > 16 * 1024 * 1024:
            raise WebError("Invalid copied input. Use the file picker or link a large local file.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise WebError("Copied input is not valid base64.") from None
        path = self.profile.staging / ("upload_" + secrets.token_hex(16))
        with path.open("xb") as stream:
            path.chmod(0o600)
            stream.write(data)
        return path, True

    def copy_into(self, source, target_dir, filename):
        private_dir(target_dir)
        name = Path(str(filename or "upload.bin")).name
        name = "".join(c if c.isalnum() or c in ".-_" else "_" for c in name)[:200]
        if name in {"", ".", ".."}:
            name = "upload.bin"
        destination = target_dir / name
        with destination.open("xb") as stream, source.open("rb") as incoming:
            destination.chmod(0o600)
            shutil.copyfileobj(incoming, stream, length=1024 * 1024)
        return destination

    def enrich_artifact(self, payload):
        if payload.get("kind") == "figure":
            figure = payload.get("payload", {})
            urls = {}
            for field in ("display_path", "png_path", "svg_path"):
                path = figure.get(field)
                if not path:
                    continue
                try:
                    urls[path] = self.register(path)
                except WebError:
                    continue
            figure["web_file_urls"] = urls
            preview = figure.get("display_path") or figure.get("png_path")
            if preview in urls:
                figure["preview_data_url"] = urls[preview] + "?inline=1"
        return payload
