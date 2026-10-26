"""Keep the delivered shell and notebook surface unambiguous."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepoLayoutTests(unittest.TestCase):
    def test_only_the_manuscript_notebook_is_maintained(self):
        notebooks = {p.relative_to(ROOT).as_posix() for p in (ROOT / "notebooks").rglob("*.ipynb")
                     if ".ipynb_checkpoints" not in p.parts}
        # Reviewed product exports intentionally omit all research notebooks.
        expected = {"notebooks/eval/ncs_manuscript_figures_and_tables.ipynb"} if (ROOT / "notebooks").exists() else set()
        self.assertEqual(notebooks, expected)

    def test_web_is_the_only_frontend_and_has_no_native_or_unused_sdk_dependencies(self):
        app = ROOT / "apps/web"
        package = json.loads((app / "package.json").read_text())
        self.assertFalse((ROOT / "apps/desktop").exists())
        self.assertFalse((ROOT / "packaging/macos").exists())
        self.assertEqual(set(package["dependencies"]), {"jszip", "katex", "mermaid"})
        self.assertEqual(set(package["devDependencies"]), {"vite"})
        self.assertEqual(package["scripts"]["build"], "node scripts/build-web.mjs")
        self.assertIn('"./transport.web.js"', (app / "src/main.js").read_text())

    def test_notebook_source_does_not_depend_on_static_artwork_or_legacy_exports(self):
        if not (ROOT / "notebooks").exists():
            self.skipTest("Research artifacts are intentionally absent from product exports")
        for name in ("paper_assets", "figures", "data"):
            self.assertFalse((ROOT / "notebooks/eval" / name).exists(), name)
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("/notebooks/eval/paper_outputs/", ignored)
