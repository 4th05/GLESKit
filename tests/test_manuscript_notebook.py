"""The manuscript notebook contains only reproducible analytical figures."""
import ast
import json
from pathlib import Path
import unittest

from scripts.release.privacy_gate import HOME_PATH

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks/eval/ncs_manuscript_figures_and_tables.ipynb"
FIGURES = {
    "fig3_genereportbench_results", "fig4_bixbench_results", "fig5_skills_ablation", "fig6_species_cohorts",
    "ed_fig2_bixbench_model_selection", "ed_fig3_bixbench_metadata_categories",
    "ed_fig4_genereportbench_model_selection", "ed_fig5_bixbench_failure_findings",
}


@unittest.skipUnless(NOTEBOOK.is_file(), "Research notebook is intentionally absent from product-only exports")
class ManuscriptNotebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notebook = json.loads(NOTEBOOK.read_text())
        cls.sources = ["".join(cell.get("source", [])) for cell in cls.notebook["cells"] if cell["cell_type"] == "code"]

    def test_every_code_cell_compiles(self):
        for index, source in enumerate(self.sources):
            with self.subTest(cell=index):
                compile(source, f"manuscript-cell-{index}", "exec")

    def test_no_static_figma_imports_or_excluded_diagnostics(self):
        code = "\n".join(self.sources)
        for marker in ("publish_static_asset", "paper_assets", "ASSET_DIR", "Image(",
                       "fig1_architecture", "fig2_benchmark_examples", "ed_fig1_architecture"):
            self.assertNotIn(marker, code)
        markdown = "\n".join("".join(cell.get("source", [])) for cell in self.notebook["cells"] if cell["cell_type"] == "markdown")
        self.assertNotIn("Excluded diagnostic", markdown)

    def test_exactly_the_eight_analytical_figures_are_generated(self):
        generated = []
        for source in self.sources:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "save_figure":
                    self.assertGreaterEqual(len(node.args), 2)
                    generated.append(ast.literal_eval(node.args[1]))
        self.assertEqual(len(generated), len(FIGURES))
        self.assertEqual(set(generated), FIGURES)

    def test_output_manifest_and_paper_sync_include_only_generated_pdfs(self):
        validation = ast.parse(self.sources[-1])
        assignments = {node.targets[0].id: node.value for node in validation.body
                       if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        self.assertEqual(set(ast.literal_eval(assignments["EXPECTED_PNG"])), {stem + ".png" for stem in FIGURES})
        sync = next(node for node in validation.body if isinstance(node, ast.If)
                    and isinstance(node.test, ast.Name) and node.test.id == "SYNC_TO_PAPER")
        sync_assignments = {node.targets[0].id: node.value for node in sync.body
                            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
        pdfs = ast.literal_eval(sync_assignments["main_pdfs"]) + ast.literal_eval(sync_assignments["extended_pdfs"])
        self.assertEqual(set(pdfs), {stem + ".pdf" for stem in FIGURES})
        self.assertEqual(len(pdfs), len(FIGURES))
        self.assertIn("os.environ.get('GLESKIT_SYNC_PAPER', '0') == '1'", self.sources[-1])

    def test_saved_outputs_do_not_expose_personal_machine_paths(self):
        outputs = [cell.get("outputs", []) for cell in self.notebook["cells"]]
        self.assertFalse(bool(HOME_PATH.search(json.dumps(outputs).encode())),
                         "Saved outputs must not contain personal machine paths")
