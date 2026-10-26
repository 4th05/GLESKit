"""Packaging/launch contracts only; unit tests never invoke Docker or a provider."""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from gleskit_mcp.web import container
from gleskit_mcp.web import __main__ as native
from gleskit_mcp.web.common import WebError, write_private
from gleskit_mcp.web.lifecycle import identity, registry_path

ROOT = Path(__file__).resolve().parents[1]


class ContainerRuntimeTests(unittest.TestCase):
    def test_native_default_remains_loopback_and_cli_cannot_select_a_public_bind(self):
        self.assertEqual(inspect.signature(native.main).parameters["_bind_host"].default, "127.0.0.1")
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as stopped:
            native.main(_argv=["--host", "0.0.0.0"])
        self.assertEqual(stopped.exception.code, 2)

    def test_container_paths_and_browser_origin_are_fixed(self):
        self.assertEqual(container.PROJECT, Path("/app"))
        self.assertEqual(container.PROFILE, Path("/app/var/local-web/profile"))
        self.assertEqual(container.ORIGIN, "http://127.0.0.1:8123")
        self.assertEqual(container.INTERNAL_BIND, "0.0.0.0")
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as stopped:
            container.main(["--port", "9999"])
        self.assertEqual(stopped.exception.code, 2)

    def test_healthcheck_has_no_profile_or_engine_side_effects_or_output(self):
        with patch.dict("sys.modules", {"gleskit_mcp.web.profile": None, "gleskit_mcp.web.engine": None,
                                        "gleskit_mcp.web.app": None}), patch("sys.stdout", io.StringIO()) as output:
            with patch.object(container, "ping", return_value=True):
                self.assertEqual(container.main(["--healthcheck"]), 0)
            with patch.object(container, "ping", return_value=False):
                self.assertEqual(container.main(["--healthcheck"]), 1)
            self.assertEqual(output.getvalue(), "")

    def test_ping_is_direct_loopback_get_and_checks_instance(self):
        client = Mock()
        client.getresponse.return_value.status = 200
        client.getresponse.return_value.read.return_value = json.dumps({"app": "gleskit-local-web", "instance": "a" * 32}).encode()
        with patch.object(container.http.client, "HTTPConnection", return_value=client) as connect:
            self.assertTrue(container.ping("a" * 32))
            self.assertFalse(container.ping("b" * 32))
        connect.assert_called_with("127.0.0.1", 8123, timeout=2)
        client.request.assert_called_with("GET", "/api/ping", headers={"Host": "127.0.0.1:8123"})
        self.assertEqual(client.close.call_count, 2)

    def test_root_cannot_start_the_container_host(self):
        with patch.object(container.os, "getuid", return_value=0), patch.object(container.sys, "platform", "linux"), \
                patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as stopped:
            container.main([])
        self.assertEqual(stopped.exception.code, 1)

    def test_container_reuses_existing_server_with_only_internal_bind_changed(self):
        with patch.object(container, "PROJECT") as project, patch.object(container.os, "getuid", return_value=10001), \
                patch.object(container.sys, "platform", "linux"), patch.object(container.os, "umask"), \
                patch.object(native, "main") as serve:
            project.__str__.return_value = "/app"
            state = project.__truediv__.return_value
            state.is_symlink.return_value = False
            state.is_dir.return_value = True
            state.resolve.return_value = state
            state.stat.return_value.st_uid = 10001
            self.assertEqual(container.main([]), 0)
        values = serve.call_args.kwargs
        self.assertEqual(values["_bind_host"], "0.0.0.0")
        self.assertTrue(values["_forward_resource_limits"])
        argv = values["_argv"]
        self.assertEqual(argv[argv.index("--project-root") + 1], "/app")
        self.assertEqual(argv[argv.index("--port") + 1], "8123")
        self.assertEqual(argv[argv.index("--opencode") + 1], "/opt/gleskit/bin/opencode")
        self.assertIn("--no-browser", argv)

    def test_runtime_lock_identities_are_not_silently_updated(self):
        meta = json.loads((ROOT / "docker/runtime.json").read_text())
        for component in ("python", "r_base", "frontend"):
            row = meta[component]
            self.assertEqual(hashlib.sha256((ROOT / row["lock"]).read_bytes()).hexdigest(), row["lock_sha256"])
        self.assertEqual(hashlib.sha256((ROOT / meta["r_base"]["installer"]).read_bytes()).hexdigest(), meta["r_base"]["installer_sha256"])
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn(meta["r_base"]["platform_digest"], dockerfile)
        self.assertIn(meta["frontend"]["platform_digest"], dockerfile)
        self.assertNotRegex(dockerfile, r"(?m)^COPY\s+(?:--\S+\s+)*\.(?:\s|/)")
        self.assertIn("--require-hashes", dockerfile)

    def test_compose_does_not_mount_checkout_or_expose_private_children(self):
        compose = (ROOT / "compose.yaml").read_text()
        for literal in ('"127.0.0.1:8123:8123"', "state:/app/var", "read_only: true", "init: true",
                        'user: "10001:10001"', 'restart: "no"', "no-new-privileges:true"):
            self.assertIn(literal, compose)
        for forbidden in ("privileged:", "network_mode:", "docker.sock", "env_file:", "${", "./var:", "./:/app"):
            self.assertNotIn(forbidden, compose)
        patterns = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
        self.assertEqual(patterns[0], "**")
        self.assertIn("**/.env", patterns)
        self.assertIn("**/.env.*", patterns)
        self.assertNotIn("!var/", patterns)
        self.assertNotIn("!src/gleskit_mcp/**/", patterns)


class ContainerLaunchHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gleskit-handoff-unit-")
        self.root = Path(self.temp.name).resolve()
        self.profile = self.root / "var/local-web/profile"
        self.row = {"pid": 321, "uid": os.getuid(), "started": "fixture-start"}
        self.instance = {"schema": 1, "pid": 321, "instance": "a" * 32,
                         "origin": container.ORIGIN, "nonce": "n" * 43}
        self.handoff = self.profile / "web-instance.json"
        write_private(self.handoff, self.instance)
        write_private(registry_path(self.root, self.profile), {"schema": 1, "project": str(self.root),
                      "profile": str(self.profile), "host": identity(self.row)})
        self.patches = [patch.object(container, "PROJECT", self.root), patch.object(container, "PROFILE", self.profile),
                        patch.object(container, "ping", return_value=True),
                        patch("gleskit_mcp.web.lifecycle.process_table", return_value={321: self.row}),
                        patch("gleskit_mcp.web.lifecycle.command_args", return_value=["python", "-m", "gleskit_mcp.web.container"])]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_verified_private_handoff_returns_exact_loopback_launch_url(self):
        self.assertEqual(container.launch_url(), container.ORIGIN + "/#launch=" + "n" * 43)

    def test_insecure_handoff_permissions_are_rejected(self):
        self.handoff.chmod(0o644)
        with self.assertRaisesRegex(WebError, "verified running"):
            container.launch_url()

    def test_symlink_handoff_is_rejected(self):
        target = self.profile / "target.json"
        self.handoff.rename(target)
        self.handoff.symlink_to(target)
        with self.assertRaises(WebError): container.launch_url()

    def test_stale_pid_and_foreign_instance_are_rejected(self):
        with patch("gleskit_mcp.web.lifecycle.process_table", return_value={321: {**self.row, "started": "reused"}}):
            with self.assertRaises(WebError): container.launch_url()
        with patch.object(container, "ping", return_value=False):
            with self.assertRaises(WebError): container.launch_url()

    def test_nonloopback_origin_never_returns_capability(self):
        write_private(self.handoff, {**self.instance, "origin": "http://192.0.2.1:8123"})
        with self.assertRaises(WebError): container.launch_url()
