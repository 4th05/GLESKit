from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from gleskit_mcp.analysis.deseq2 import DESeq2AnalysisBackend
from gleskit_mcp.config import load_settings


class DESeq2AnalysisBackendTests(unittest.TestCase):
    def test_analyze_counts_defaults_to_no_lfc_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            captured_request: dict[str, object] = {}

            def fake_runner(cmd, *, capture_output, text, timeout, check, env):
                input_path = Path(cmd[cmd.index("--input") + 1])
                captured_request.update(json.loads(input_path.read_text()))
                output_path = Path(cmd[cmd.index("--output") + 1])
                output_path.write_text(
                    json.dumps(
                        {
                            "provider": "deseq2",
                            "summary": "Stubbed DESeq2 run.",
                            "counts": {"sample_count": 2},
                            "summary_stats": {"significant_gene_count": 0},
                            "top_genes": {"upregulated": [], "downregulated": []},
                            "significant_gene_sets": {"upregulated": [], "downregulated": [], "all_significant": []},
                            "ranked_gene_scores": [],
                            "results": [],
                        }
                    )
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            DESeq2AnalysisBackend(settings, command_runner=fake_runner).analyze_counts(
                counts_path=str(root / "counts.tsv"),
                sample_metadata_text="sample\tcondition\nS1\tA\nS2\tB\n",
            )

            self.assertEqual(captured_request["log2_fold_change_threshold"], 0.0)

    def test_analyze_counts_uses_runner_payload_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            captured_request: dict[str, object] = {}

            def fake_runner(cmd, *, capture_output, text, timeout, check, env):
                input_path = Path(cmd[cmd.index("--input") + 1])
                captured_request.update(json.loads(input_path.read_text()))
                output_path = Path(cmd[cmd.index("--output") + 1])
                output_payload = {
                    "provider": "deseq2",
                    "summary": "Stubbed DESeq2 run.",
                    "counts": {"sample_count": 2},
                    "summary_stats": {"significant_gene_count": 1},
                    "top_genes": {"upregulated": [{"gene": "TP53"}], "downregulated": []},
                    "significant_gene_sets": {"upregulated": ["TP53"], "downregulated": [], "all_significant": ["TP53"]},
                    "ranked_gene_scores": [{"gene": "TP53", "score": 3.2}],
                    "results": [{"gene": "TP53", "log2_fold_change": 1.5}],
                }
                output_path.write_text(json.dumps(output_payload))
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            backend = DESeq2AnalysisBackend(settings, command_runner=fake_runner)
            result = backend.analyze_counts(
                counts_path=str(root / "counts.tsv"),
                sample_metadata_text="sample\tcondition\nS1\tA\nS2\tB\n",
                label="Example",
                comparison="B vs A",
                p_value_threshold=0.01,
                base_mean_threshold=10.0,
                significance_mode="p_value",
                log2_fold_change_threshold=0.5,
            )

            self.assertEqual(result["provider"], "deseq2")
            self.assertEqual(result["summary"], "Stubbed DESeq2 run.")
            self.assertEqual(result["top_genes"]["upregulated"][0]["gene"], "TP53")
            self.assertEqual(result["ranked_gene_scores"][0]["gene"], "TP53")
            self.assertEqual(captured_request["p_value_threshold"], 0.01)
            self.assertEqual(captured_request["base_mean_threshold"], 10.0)
            self.assertEqual(captured_request["significance_mode"], "p_value")
            self.assertEqual(captured_request["log2_fold_change_threshold"], 0.5)


if __name__ == "__main__":
    unittest.main()
