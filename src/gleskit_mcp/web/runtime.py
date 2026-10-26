from __future__ import annotations

import threading
import time

from gleskit_mcp.models import utc_now_iso
from .common import WebError, identifier, write_private


class Runtime:
    """One controller; a background server alone does not enable persistence."""

    def __init__(self, profile):
        self.profile = profile
        self.lock = threading.RLock()
        self.owner = None
        self.expires = 0
        self.revision = 0
        self.active = None
        self.capture = False

    def claim(self, tab, takeover=False):
        identifier(tab)
        with self.lock:
            self._expire()
            if self.owner in (None, tab) or takeover:
                if self.owner not in (None, tab):
                    self.active, self.capture = None, False
                self.owner, self.expires = tab, time.monotonic() + 20
                self.revision += 1
                self._write()
            return {"writable": self.owner == tab, "revision": self.revision}

    def _expire(self):
        if self.owner and self.expires < time.monotonic():
            self.owner, self.active, self.capture = None, None, False
            self.revision += 1
            self._write()

    def require(self, tab):
        with self.lock:
            self._expire()
            if self.owner != tab:
                raise WebError("Another browser tab controls this profile, or your connection expired. Open Settings to take control.", 409)

    def renew(self, tab):
        with self.lock:
            self._expire()
            if self.owner == tab:
                self.expires = time.monotonic() + 20
                self._write()

    def release(self, tab):
        # A short disconnect can be a reload. Never clear a different tab.
        with self.lock:
            if self.owner == tab:
                self.expires = min(self.expires, time.monotonic() + 20)

    def snapshot(self):
        with self.lock:
            self._expire()
            enabled = self.owner is not None and self.profile.workspace_source != "configured-read-only"
            return {"available": enabled, "active_session_id": self.active if enabled else None,
                    "capture_enabled": bool(self.capture and self.active and enabled),
                    "connection_state": "connected" if enabled else "cleared", "revision": self.revision}

    def select(self, tab, session, capture, expected=None):
        with self.lock:
            self.require(tab)
            if expected is not None and expected != self.revision:
                raise WebError("Workspace selection changed. Refresh before changing capture/session.", 409)
            if session is not None:
                identifier(session)
            if type(capture) is not bool:
                raise WebError("Capture must be true or false.")
            self.active, self.capture = session, bool(session and capture)
            self.revision += 1
            self._write()
            return self.snapshot()

    def _write(self):
        enabled = self.owner is not None and self.profile.workspace_source != "configured-read-only"
        write_private(self.profile.heartbeat, {"workspace_root": str(self.profile.workspace),
                      "active_session_id": self.active if enabled else None,
                      "capture_enabled": bool(self.capture and enabled and self.active),
                      "connection_state": "connected" if enabled else "cleared", "ui_name": "GLESKit Local",
                      "updated_at": utc_now_iso()})

    def close(self):
        with self.lock:
            self.owner, self.active, self.capture = None, None, False
            self._write()
