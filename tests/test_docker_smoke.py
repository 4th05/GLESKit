"""Release smoke safety contracts; ordinary tests never start Docker."""
import json
import io
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from scripts.release.docker_smoke import PROBE, PrefixReader, Probe, scan_image_archive, snapshot

ROOT = Path(__file__).resolve().parents[1]


class DockerSmokeTests(unittest.TestCase):
    def test_manual_ci_uses_pinned_read_only_checkout_and_no_publishing(self):
        workflow = (ROOT / ".github/workflows/docker-validation.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertEqual(workflow.count("persist-credentials: false"), 2)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertNotIn("--push", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertEqual(workflow.count("checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"), 2)

    def test_editor_credentials_and_compose_overrides_do_not_reach_docker_client(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-not-forwarded", "COMPOSE_FILE": "synthetic-private-file",
                                   "BUILDX_BUILDER": "synthetic-remote", "VITE_PRIVATE_KEY": "synthetic-not-forwarded"}):
            probe = Probe(ROOT, "desktop-linux", "synthetic:fixture", Path("unused-report.json"))
            for name in ("OPENAI_API_KEY", "COMPOSE_FILE", "BUILDX_BUILDER", "VITE_PRIVATE_KEY"):
                self.assertNotIn(name, probe.env)

    def image_archive(self, body):
        inner, outer = io.BytesIO(), io.BytesIO()
        with tarfile.open(fileobj=inner, mode="w") as layer:
            entry = tarfile.TarInfo("app/synthetic.txt"); entry.size = len(body)
            layer.addfile(entry, io.BytesIO(body))
        with tarfile.open(fileobj=outer, mode="w") as saved:
            entry = tarfile.TarInfo("layer/layer.tar"); entry.size = len(inner.getvalue())
            saved.addfile(entry, io.BytesIO(inner.getvalue()))
        outer.seek(0)
        return outer

    def test_streamed_layer_audit_catches_canaries_without_image_spooling(self):
        result = scan_image_archive(self.image_archive(b"synthetic public file"), [b"private-canary"])
        self.assertEqual(result["image_layers_scanned"], 1)
        self.assertEqual(result["layer_files_scanned"], 1)
        with self.assertRaisesRegex(RuntimeError, "Private build marker"):
            scan_image_archive(self.image_archive(b"synthetic private-canary"), [b"private-canary"])
        self.assertEqual(PrefixReader(b"abc", io.BytesIO(b"def")).read(), b"abcdef")

    def test_probe_import_and_report_do_not_claim_public_or_network_approval(self):
        compile(PROBE, "synthetic-container-probe", "exec")
        with tempfile.TemporaryDirectory() as folder:
            probe = Probe(ROOT, "desktop-linux", "synthetic:fixture", Path(folder) / "report.json")
            self.assertFalse(probe.report["release_approved"])
            self.assertFalse(probe.report["network_observation_gate_passed"])
            self.assertTrue(probe.project.startswith("gleskit-validation-"))
            self.assertNotIn("gleskit-local_state", probe.state_volume)

    def test_remote_context_is_rejected_before_daemon_or_source_operations(self):
        probe = Probe(ROOT, "remote", "synthetic:fixture", Path("unused-report.json"))
        with patch.object(probe, "docker", return_value=b"ssh://remote.invalid") as docker:
            with self.assertRaisesRegex(RuntimeError, "local Unix"):
                probe.preflight()
        self.assertEqual(docker.call_count, 1)
        self.assertEqual(docker.call_args.args[:2], ("context", "inspect"))

    def test_snapshot_cannot_admit_private_runtime_history_or_notebook_material(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"; root.mkdir()
            target = Path(folder) / "snapshot"; target.mkdir()
            files = {"Dockerfile": "FROM scratch\n", ".dockerignore": "**\n", "compose.yaml": "services: {}\n",
                     "docker/third-party-inventory.json": "{}", "src/product.py": "answer=42\n", "README.md": "Product\n",
                     "var/private.csv": "private", ".env": "private", ".git/config": "private",
                     "notebooks/private.ipynb": "private", "src/.aws/credentials": "private"}
            for name, body in files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(body)
            with patch("scripts.release.docker_smoke.subprocess.check_output", return_value="\0".join(files).encode()):
                selected = snapshot(root, target)
            self.assertEqual(set(selected), {name for name in files if name in {
                "Dockerfile", ".dockerignore", "compose.yaml", "docker/third-party-inventory.json", "src/product.py", "README.md"}})
            self.assertFalse((target / ".git").exists())
            self.assertFalse((target / "var").exists())

    def test_source_with_a_credential_shape_fails_closed_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "src").mkdir()
            key = "sk-proj-" + "x" * 48
            (root / "src/new.py").write_text(key)
            with patch("scripts.release.docker_smoke.subprocess.check_output", return_value=b"src/new.py\0"):
                with self.assertRaisesRegex(RuntimeError, "privacy scanning") as caught:
                    snapshot(root, root / "copy")
            self.assertNotIn(key, str(caught.exception))

    def test_docker_failure_diagnostics_are_private_and_not_reflected_in_error(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.json"
            probe = Probe(ROOT, "desktop-linux", "synthetic:fixture", report)
            secret = b"synthetic-diagnostic-canary"
            with patch("scripts.release.docker_smoke.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", secret)):
                with self.assertRaises(RuntimeError) as caught:
                    probe.docker("inspect", "synthetic")
            self.assertNotIn(secret.decode(), str(caught.exception))
            self.assertEqual(report.with_suffix(".private.log").stat().st_mode & 0o777, 0o600)

    def test_stale_image_source_is_rejected_without_starting_the_app(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root / "src/gleskit_mcp/test.py"; path.parent.mkdir(parents=True)
            path.write_text("synthetic = True\n")
            probe = Probe(ROOT, "desktop-linux", "synthetic:fixture", root / "report.json")
            with patch.object(probe, "docker", return_value=b"{}"):
                with self.assertRaisesRegex(RuntimeError, "reviewed source"):
                    probe.verify_source(root)

