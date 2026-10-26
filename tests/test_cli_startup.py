"""Metadata commands must not initialize analysis or plotting backends."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LightweightCLITests(unittest.TestCase):
    def probe(self, command):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code = '''import contextlib, io, json, sys
from gleskit_mcp.cli import main
with contextlib.redirect_stdout(io.StringIO()) as output:
    try:
        status = main(sys.argv[1:])
    except SystemExit as exc:
        status = exc.code
print(json.dumps({"status":status,"output":output.getvalue(),"heavy":[n for n in ("pandas","scipy","matplotlib.pyplot","gleskit_mcp.services") if n in sys.modules]}))
'''
            env = {"PATH": os.defpath, "HOME": str(root), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            result = subprocess.run([sys.executable, "-c", code, "--project-root", str(root),
                                     "--workspace-root", str(root / "workspace"), *command],
                                    cwd=root, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value["status"], 0)
            self.assertEqual(value["heavy"], [])
            return value["output"]

    def test_help_does_not_import_analysis(self):
        self.assertIn("usage:", self.probe(["--help"]))

    def test_session_list_does_not_import_analysis(self):
        self.assertIsInstance(json.loads(self.probe(["list-sessions"])), dict)

    def test_reference_status_does_not_import_analysis(self):
        self.assertFalse(json.loads(self.probe(["reference-status"]))["available"])
