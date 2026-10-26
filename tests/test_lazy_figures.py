"""Cold MCP startup must not import the plotting stack or scan system fonts."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


class LazyFigureTests(unittest.TestCase):
    def test_service_startup_defers_and_caches_the_existing_figure_catalog(self):
        code = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from gleskit_mcp.config import load_settings
from gleskit_mcp.services import GLESKitService
assert "matplotlib.pyplot" not in sys.modules
settings = load_settings(Path(sys.argv[1]))
service = GLESKitService(settings=settings)
assert "matplotlib.pyplot" not in sys.modules
assert "figure_catalog" not in service.__dict__
created = []
def catalog(value):
    assert value is settings
    result = object()
    created.append(result)
    return result
with patch.dict(sys.modules, {"gleskit_mcp.figures": SimpleNamespace(FigureCatalog=catalog)}):
    first = service.figure_catalog
    assert service.figure_catalog is first
    assert len(created) == 1
    service.figure_catalog = "replacement"
    assert service.figure_catalog == "replacement"
print("ready-without-plotting")
'''
        with tempfile.TemporaryDirectory(prefix="gleskit-lazy-figure-test-") as folder:
            environment = {**os.environ, "MPLCONFIGDIR": str(Path(folder) / "font-cache")}
            result = subprocess.run([sys.executable, "-c", code, folder], env=environment,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "ready-without-plotting")
            self.assertFalse((Path(folder) / "font-cache").exists())
