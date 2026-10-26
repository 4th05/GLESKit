"""Stateless MCP figures use the authenticated browser origin, not child ports."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gleskit_mcp.config import load_settings
from gleskit_mcp.mcp_server import _FigureDownloadRegistry, build_server
from gleskit_mcp.web.app import create_app
from gleskit_mcp.web.commands import Commands
from gleskit_mcp.web.common import WebError, write_private
from gleskit_mcp.web.files import FigureDownloads
from gleskit_mcp.web.profile import WebProfile
from tests.test_web_shell import FakeEngine, MemoryCredentials

ROOT = Path(__file__).resolve().parents[1]


class WebFigureTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-web-figure-")
        self.root = Path(self.temp.name).resolve()
        self.profile = WebProfile(ROOT, self.root / "profile")
        self.commands = Commands(self.profile, MemoryCredentials(), FakeEngine)
        self.ui = self.root / "ui"; self.ui.mkdir()
        (self.ui / "index.html").write_text("<!doctype html><title>Synthetic fixture</title>")
        self.app = create_app(self.profile, self.ui, "http://127.0.0.1:5017", commands=self.commands)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:5017")
        self.client.__enter__()
        self.exports = FigureDownloads(self.profile.data, "http://127.0.0.1:5017")
        self.registry = _FigureDownloadRegistry(web_exports=self.exports)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self):
        self.assertEqual(self.client.post("/api/auth/bootstrap", json={"nonce": self.app.state.host.nonce},
                         headers={"Origin": "http://127.0.0.1:5017"}).status_code, 200)

    def test_generated_bytes_are_downloadable_only_through_authenticated_ui(self):
        token, _ = self.registry.register(data=b"synthetic png", mime_type="image/png", filename="figure.png")
        url = self.exports.url(token)
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login()
        response = self.client.get(url)
        self.assertEqual(response.content, b"synthetic png")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get(url, headers={"Host": "foreign.invalid"}).status_code, 400)
        self.assertIsNone(self.commands.runtime.active)

    def test_svg_never_executes_inline_even_with_inline_query(self):
        token, _ = self.registry.register(data=b"<svg/>", mime_type="image/svg+xml", filename="figure.svg")
        self.login()
        response = self.client.get(self.exports.url(token) + "?inline=1")
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_expired_invalid_and_symlinked_records_are_rejected(self):
        self.login()
        for token in ("unknown", "x" * 24, "..", "../.env"):
            with self.assertRaises(WebError): self.exports.get(token)
        token, _ = self.registry.register(data=b"png", mime_type="image/png", filename="figure.png")
        meta = self.exports.root / (token + ".json")
        value = json.loads(meta.read_text()); value["expires_at"] = time.time() - 1
        write_private(meta, value)
        self.assertEqual(self.client.get(self.exports.url(token)).status_code, 404)
        token, _ = self.registry.register(data=b"png", mime_type="image/png", filename="figure.png")
        path = self.exports.root / (token + ".bin")
        path.unlink(); path.symlink_to(self.profile.prompt)
        self.assertEqual(self.client.get(self.exports.url(token)).status_code, 404)

    def test_child_environment_contains_only_the_explicit_ui_bridge(self):
        env = self.profile.environment()
        self.assertEqual(env["GLESKIT_WEB_PROFILE"], str(self.profile.data))
        self.assertEqual(env["GLESKIT_WEB_ORIGIN"], "http://127.0.0.1:5017")
        self.assertNotIn("nonce", json.dumps(env))
        self.assertNotIn("cookie", json.dumps(env))

    def test_mcp_web_mode_does_not_open_a_companion_download_server(self):
        from types import SimpleNamespace
        with patch.dict(os.environ, {"GLESKIT_WEB_PROFILE": str(self.profile.data),
                        "GLESKIT_WEB_ORIGIN": "http://127.0.0.1:5017"}), \
                patch("gleskit_mcp.mcp_server._FigureDownloadHttpServer") as server:
            build_server(service=SimpleNamespace(settings=load_settings(self.root)), start_local_download_server=True)
            server.assert_not_called()

    def test_foreign_origin_is_rejected(self):
        with self.assertRaises(WebError): FigureDownloads(self.profile.data, "http://foreign.invalid:8123")
