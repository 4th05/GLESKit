from __future__ import annotations

import unittest

from gleskit_mcp.analysis.rnaseq import RNASEQAnalysisBackend


class RNASEQAnalysisBackendTests(unittest.TestCase):
    def test_analyze_result_infers_columns_and_splits_up_and_down_genes(self) -> None:
        backend = RNASEQAnalysisBackend()
        table_text = "\n".join(
            [
                "gene_symbol\tlog2FoldChange\tpadj\tpvalue\tbaseMean",
                "TP53\t2.1\t0.001\t0.0001\t100",
                "BRCA1\t1.4\t0.02\t0.001\t80",
                "MYC\t0.3\t0.8\t0.6\t200",
                "EGFR\t-1.8\t0.01\t0.002\t120",
                "CLOCK\t-2.3\t0.03\t0.004\t60",
            ]
        )

        result = backend.analyze_result(table_text=table_text, comparison="tumor vs control", top_n=3)

        self.assertEqual(result["table"]["column_map"]["gene"], "gene_symbol")
        self.assertEqual(result["summary"]["significance_mode"], "padj")
        self.assertEqual(result["summary"]["significant_gene_count"], 4)
        self.assertEqual(result["summary"]["upregulated_count"], 2)
        self.assertEqual(result["summary"]["downregulated_count"], 2)
        self.assertEqual(result["top_genes"]["upregulated"][0]["gene"], "TP53")
        self.assertEqual(result["top_genes"]["downregulated"][0]["gene"], "CLOCK")
        self.assertEqual(result["significant_gene_sets"]["upregulated"], ["TP53", "BRCA1"])
        self.assertEqual(result["significant_gene_sets"]["downregulated"], ["CLOCK", "EGFR"])

    def test_all_significant_gene_set_is_not_truncated_for_downstream_enrichment(self) -> None:
        backend = RNASEQAnalysisBackend()
        rows = ["gene\tlog2FoldChange\tpadj\tpvalue\tbaseMean"]
        rows.extend(
            f"G{index}\t2.0\t0.001\t0.0001\t100"
            for index in range(1, 226)
        )

        result = backend.analyze_result(table_text="\n".join(rows), enrichment_gene_limit=100)

        self.assertEqual(result["summary"]["significant_gene_count"], 225)
        self.assertEqual(len(result["significant_gene_sets"]["upregulated"]), 100)
        self.assertEqual(len(result["significant_gene_sets"]["all_significant"]), 225)

    def test_analyze_result_handles_excel_style_deg_headers(self) -> None:
        backend = RNASEQAnalysisBackend()
        table_text = "\n".join(
            [
                "Unnamed: 0\tp_val\tavg_log2FC\tpct.1\tpct.2\tp_val_adj",
                "TP53\t0.0001\t2.1\t0.8\t0.1\t0.001",
                "BRCA1\t0.003\t-1.4\t0.1\t0.7\t0.02",
                "MYC\t0.6\t0.2\t0.3\t0.2\t0.8",
            ]
        )

        result = backend.analyze_result(table_text=table_text, comparison="post vs pre", top_n=3)

        self.assertEqual(result["table"]["column_map"]["gene"], "Unnamed: 0")
        self.assertEqual(result["table"]["column_map"]["log2_fold_change"], "avg_log2FC")
        self.assertEqual(result["table"]["column_map"]["padj"], "p_val_adj")
        self.assertEqual(result["table"]["column_map"]["p_value"], "p_val")
        self.assertEqual(result["summary"]["significant_gene_count"], 2)
        self.assertEqual(result["top_genes"]["upregulated"][0]["gene"], "TP53")
        self.assertEqual(result["top_genes"]["downregulated"][0]["gene"], "BRCA1")


if __name__ == "__main__":
    unittest.main()
