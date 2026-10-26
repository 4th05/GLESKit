from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

from gleskit_mcp.analysis import SingleCellAnalysisBackend

try:
    import anndata as ad
except ImportError:  # pragma: no cover
    ad = None


@unittest.skipIf(ad is None, "anndata is required for single-cell backend tests")
class SingleCellAnalysisBackendTests(unittest.TestCase):
    def _write_h5ad(self, root: Path) -> Path:
        matrix = np.asarray(
            [
                [5, 1, 0],
                [4, 1, 0],
                [0, 2, 6],
                [0, 1, 7],
            ],
            dtype=float,
        )
        obs = pd.DataFrame(
            {
                "sample": ["s1", "s1", "s2", "s2"],
                "condition": ["A", "A", "B", "B"],
                "cell_type": ["T", "T", "T", "T"],
            },
            index=["c1", "c2", "c3", "c4"],
        )
        var = pd.DataFrame(index=["G1", "G2", "G3"])
        adata = ad.AnnData(X=matrix, obs=obs, var=var)
        path = root / "cells.h5ad"
        adata.write_h5ad(path)
        return path

    def test_summarize_h5ad_and_build_pseudobulk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = SingleCellAnalysisBackend()
            h5ad_path = self._write_h5ad(root)

            summary = backend.summarize_h5ad(h5ad_path=str(h5ad_path))
            pseudobulk = backend.build_pseudobulk_dataset(
                h5ad_path=str(h5ad_path),
                sample_column="sample",
                condition_column="condition",
                cell_type_column="cell_type",
                cell_type_values=["T"],
            )

            self.assertEqual(summary["analysis"], "summary")
            self.assertEqual(summary["object"]["cell_count"], 4)
            self.assertEqual(pseudobulk.counts.shape[0], 3)
            self.assertEqual(pseudobulk.metadata.shape[0], 2)
            self.assertIn("sample_count", pseudobulk.summary)

    def test_cell_type_deg_counts_and_gene_lookup_from_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = SingleCellAnalysisBackend()
            workbook = root / "deg.xlsx"
            with pd.ExcelWriter(workbook) as writer:
                pd.DataFrame(
                    {
                        "gene": ["SOCS3", "IL7R"],
                        "p_val_adj": [0.01, 0.2],
                        "avg_log2FC": [1.5, 0.2],
                    }
                ).to_excel(writer, sheet_name="CD4", index=False)
                pd.DataFrame(
                    {
                        "gene": ["SOCS3", "LYZ"],
                        "p_val_adj": [0.03, 0.001],
                        "avg_log2FC": [1.2, 2.0],
                    }
                ).to_excel(writer, sheet_name="CD8", index=False)

            counts = backend.cell_type_deg_counts(deg_table_path=str(workbook), gene_column="gene")
            lookup = backend.gene_cell_type_lookup(
                deg_table_path=str(workbook),
                gene="SOCS3",
                gene_column="gene",
            )

            self.assertEqual(counts["analysis"], "cell_type_deg_counts")
            self.assertEqual(len(counts["cell_type_counts"]), 2)
            self.assertEqual(counts["question_ready"]["top_cell_type"], "CD8")
            self.assertEqual(counts["question_ready"]["top_significant_gene_count"], 2)
            self.assertIn("Top cell type: CD8 (2 significant genes).", counts["summary"])
            self.assertEqual(lookup["analysis"], "gene_cell_type_lookup")
            self.assertEqual(len(lookup["matches"]), 2)
            self.assertTrue(all(row["gene"] == "SOCS3" for row in lookup["matches"]))

    def test_cluster_marker_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = SingleCellAnalysisBackend()
            h5ad_path = self._write_h5ad(root)

            result = backend.cluster_marker_analysis(
                h5ad_path=str(h5ad_path),
                cluster_labels={
                    "c1": "A",
                    "c2": "A",
                    "c3": "B",
                    "c4": "B",
                },
                top_n=2,
            )

            self.assertEqual(result["analysis"], "cluster_marker_analysis")
            self.assertEqual(result["cluster_count"], 2)
            self.assertGreaterEqual(result["significant_marker_count_bh"], 1)
            self.assertIn("A", result["marker_gene_sets"])
            self.assertTrue(result["cluster_markers"][0]["top_marker_genes"])


if __name__ == "__main__":
    unittest.main()
