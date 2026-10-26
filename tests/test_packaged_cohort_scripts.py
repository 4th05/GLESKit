from pathlib import Path
import tempfile
import unittest

from gleskit_mcp.cohort_download import CohortDownloadBackend
from gleskit_mcp.cohort_providers import _gtex_catalog_script_path
from gleskit_mcp.config import load_settings


class PackagedCohortScriptTests(unittest.TestCase):
    def test_scripts_do_not_require_a_source_checkout_in_project_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backend = CohortDownloadBackend(load_settings(root))
            self.assertTrue(backend.script_path.is_file())
            self.assertTrue(_gtex_catalog_script_path(root).is_file())
            self.assertFalse(backend.script_path.is_relative_to(root))
