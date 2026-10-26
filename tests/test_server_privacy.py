import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
from gleskit_mcp.mcp_server import _require_loopback_host, _FigureDownloadHttpServer, _FigureDownloadRegistry


class ServerPrivacyTests(unittest.TestCase):
    def test_only_literal_loopback_addresses_are_accepted(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            self.assertTrue(_require_loopback_host(host))
        for host in ("0.0.0.0", "::", "192.168.1.2", "example.org", "localhost.example.org", ""):
            with self.assertRaisesRegex(ValueError, "blocked"):
                _require_loopback_host(host)

    def test_nonlocal_companion_is_rejected_before_socket_creation(self):
        with patch("gleskit_mcp.mcp_server.ThreadingHTTPServer") as server:
            with self.assertRaises(ValueError):
                _FigureDownloadHttpServer(_FigureDownloadRegistry(), bind_host="0.0.0.0", port=0)
            server.assert_not_called()

    def test_public_make_target_cannot_launch_a_server(self):
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(["make", "expose_mcp", "EXPOSE_HOST=0.0.0.0"], cwd=repo, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"blocked", result.stderr)
        self.assertNotIn(b"ngrok http", result.stdout)

    def test_download_tokens_invalid_expired_and_path_traversal(self):
        registry = _FigureDownloadRegistry()
        server = _FigureDownloadHttpServer(registry, bind_host="127.0.0.1", port=0)
        self.addCleanup(server.stop)
        token, _ = registry.register(data=b"synthetic-plot", mime_type="image/png", filename="fixture.png")
        with urlopen(server.url_for_token(token)) as response:
            self.assertEqual(response.read(), b"synthetic-plot")
            self.assertIn("no-store", response.headers["Cache-Control"])
        for invalid in ("unknown-token", "..%2F..%2Fetc%2Fhosts"):
            with self.assertRaises(HTTPError) as error:
                urlopen(server.url_for_token(invalid))
            self.assertEqual(error.exception.code, 404)
        registry._entries[token].expires_at = 0
        with self.assertRaises(HTTPError) as error:
            urlopen(server.url_for_token(token))
        self.assertEqual(error.exception.code, 404)
