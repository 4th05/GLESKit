from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gleskit_mcp.config import load_settings
from gleskit_mcp.workspace import WorkspaceGateway, clear_runtime_manifest, write_runtime_manifest


class WorkspaceRuntimeTests(unittest.TestCase):
    def test_workspace_status_distinguishes_missing_stale_and_cleared_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            settings = load_settings(project_root)
            gateway = WorkspaceGateway(settings)

            missing_status = gateway.status()
            self.assertFalse(missing_status["available"])
            self.assertEqual(missing_status["reason"], "workspace_missing")

            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id="session_123",
            )
            connected_status = gateway.status()
            self.assertTrue(connected_status["available"])
            self.assertEqual(connected_status["reason"], "workspace_connected")
            self.assertEqual(connected_status["active_session_id"], "session_123")
            self.assertEqual(connected_status["connection_state"], "connected")

            stale_payload = json.loads(settings.runtime_manifest_path.read_text())
            stale_payload["updated_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=settings.runtime_ttl_seconds + 10)
            ).replace(microsecond=0).isoformat()
            settings.runtime_manifest_path.write_text(json.dumps(stale_payload, indent=2, sort_keys=True))
            stale_status = gateway.status()
            self.assertFalse(stale_status["available"])
            self.assertEqual(stale_status["reason"], "workspace_stale")

            clear_runtime_manifest(settings.runtime_manifest_path)
            cleared_status = gateway.status()
            self.assertFalse(cleared_status["available"])
            self.assertEqual(cleared_status["reason"], "workspace_cleared")
            self.assertIsNone(cleared_status["active_session_id"])

            self.assertIsNone(gateway.get_workspace())

    def test_workspace_status_recovers_from_missing_manifest_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            default_workspace_root = project_root / "var" / "gleskit_mcp"
            (default_workspace_root / "sessions").mkdir(parents=True, exist_ok=True)
            settings = load_settings(project_root)
            gateway = WorkspaceGateway(settings)

            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=project_root / "missing-workspace-root",
                active_session_id=None,
                capture_enabled=False,
            )

            recovered_status = gateway.status()

            self.assertTrue(recovered_status["available"])
            self.assertEqual(recovered_status["reason"], "workspace_connected")
            self.assertEqual(recovered_status["workspace_root"], str(default_workspace_root.resolve()))


if __name__ == "__main__":
    unittest.main()
