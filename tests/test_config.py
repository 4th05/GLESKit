from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from gleskit_mcp.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_preview_limits_load_from_defaults_and_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "gleskit.defaults.json").write_text(
                json.dumps(
                    {
                        "runtime": {
                            "upload_preview_max_lines": 80,
                            "artifact_preview_max_lines": 120,
                        }
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "gleskit.local.json").write_text(
                json.dumps(
                    {
                        "runtime": {
                            "upload_preview_max_lines": 25,
                        }
                    }
                ),
                encoding="utf-8",
            )

            settings = load_settings(root)

            self.assertEqual(settings.upload_preview_max_lines, 25)
            self.assertEqual(settings.artifact_preview_max_lines, 120)

    def test_workspace_root_falls_back_to_project_default_when_override_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            default_workspace_root = root / "var" / "gleskit_mcp"
            (default_workspace_root / "sessions").mkdir(parents=True, exist_ok=True)
            (config_dir / "gleskit.defaults.json").write_text(json.dumps({}), encoding="utf-8")
            (config_dir / "gleskit.local.json").write_text(
                json.dumps({
                    "paths": {
                        "workspace_root": str(root / "renamed-away" / "var" / "gleskit_mcp")
                    }
                }),
                encoding="utf-8",
            )

            settings = load_settings(root)

            self.assertEqual(settings.workspace_default_root, default_workspace_root.resolve())


if __name__ == "__main__":
    unittest.main()
