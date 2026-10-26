from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from gleskit_mcp.analysis.expression_matrix import ExpressionMatrixAnalysisBackend
from gleskit_mcp.providers.base import ProviderError


class ExpressionMatrixAnalysisBackendTests(unittest.TestCase):
    def _write_matrix(self, root: Path) -> Path:
        matrix_path = root / "matrix.tsv"
        matrix_path.write_text(
            "\n".join(
                [
                    "gene\ts1\ts2\ts3\ts4",
                    "g1\t1\t1\t10\t10",
                    "g2\t1\t1\t9\t9",
                    "g3\t10\t10\t1\t1",
                    "g4\t9\t9\t1\t1",
                ]
            )
            + "\n"
        )
        return matrix_path

    def test_load_matrix_and_run_pca(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            result = backend.run_pca(matrix, analysis_axis="columns", n_components=2)

            self.assertEqual(matrix.values.shape, (4, 4))
            self.assertEqual(result["analysis"], "pca")
            self.assertIn("First component explains", result["summary"])
            self.assertEqual(len(result["scores"]), 4)
            self.assertEqual(result["components"]["names"], ["PC1", "PC2"])

    def test_load_matrix_accepts_delimited_text_mislabeled_as_xls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "COAD__geneExp.xls"
            matrix_path.write_text(
                "gene\ts1\ts2\n"
                "G1\t1\t2\n"
                "G2\t3\t4\n",
                encoding="utf-8",
            )

            matrix = ExpressionMatrixAnalysisBackend().load_matrix(
                matrix_path=str(matrix_path),
                row_id_column="gene",
            )

            self.assertEqual(matrix.row_labels, ["G1", "G2"])
            self.assertEqual(matrix.column_labels, ["s1", "s2"])
            self.assertEqual(matrix.values.tolist(), [[1.0, 2.0], [3.0, 4.0]])

    def test_load_matrix_can_allow_duplicate_row_ids_for_value_level_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\n"
                "G1\t1\t2\n"
                "G1\t3\t4\n",
                encoding="utf-8",
            )

            matrix = ExpressionMatrixAnalysisBackend().load_matrix(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                require_unique_row_ids=False,
            )

            self.assertEqual(matrix.row_labels, ["G1", "G1"])

    def test_normalize_analysis_axis_accepts_common_aliases(self) -> None:
        backend = ExpressionMatrixAnalysisBackend()

        self.assertEqual(backend.normalize_analysis_axis("samples_in_columns"), "columns")
        self.assertEqual(backend.normalize_analysis_axis("genes_in_rows"), "columns")
        self.assertEqual(backend.normalize_analysis_axis("genes_in_columns"), "rows")
        self.assertEqual(backend.normalize_analysis_axis("sample rows"), "rows")

    def test_normalize_analysis_axis_rejects_invalid_values_with_helpful_message(self) -> None:
        backend = ExpressionMatrixAnalysisBackend()

        with self.assertRaises(ProviderError) as context:
            backend.normalize_analysis_axis("samples")

        self.assertIn("analysis_axis must be 'rows' or 'columns'", str(context.exception))
        self.assertIn("Use 'columns' when samples are in the columns", str(context.exception))

    def test_run_pca_rejects_suspicious_orientation_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\ts4\n"
                "g1\t1\t2\t3\t4\n"
                "g2\t5\t6\t7\t8\n",
                encoding="utf-8",
            )
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(matrix_path), row_id_column="gene")

            with self.assertRaises(ProviderError):
                backend.run_pca(matrix, analysis_axis="columns")

            forced = backend.run_pca(matrix, analysis_axis="columns", force=True)
            self.assertEqual(forced["analysis"], "pca")
            self.assertEqual(forced["observations"]["count"], 4)
            self.assertEqual(forced["features"]["count"], 2)

    def test_run_log_transform_default_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t0\t3\ng2\t1\t7\n", encoding="utf-8")
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(matrix_path), row_id_column="gene")
            transformed, payload = backend.run_log_transform(matrix)

            self.assertEqual(payload["analysis"], "log_transform")
            self.assertEqual(payload["parameters"]["base"], 2.0)
            self.assertEqual(payload["parameters"]["offset"], 1.0)
            self.assertEqual(payload["parameters"]["preset"], "custom")
            self.assertEqual(transformed.values.tolist(), [[0.0, 2.0], [1.0, 3.0]])
            self.assertEqual(transformed.row_labels, ["g1", "g2"])
            self.assertEqual(transformed.column_labels, ["s1", "s2"])

    def test_run_log_transform_accepts_custom_preset_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t0\t3\ng2\t1\t7\n", encoding="utf-8")
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(matrix_path), row_id_column="gene")
            transformed, payload = backend.run_log_transform(matrix, base=10.0, offset=1.0, preset="custom")

            self.assertEqual(payload["parameters"]["preset"], "custom")
            self.assertAlmostEqual(float(transformed.values[0, 1]), 0.6020599913, places=6)

    def test_run_log_transform_custom_base_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t0\t9\ng2\t99\t999\n", encoding="utf-8")
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(matrix_path), row_id_column="gene")
            transformed, payload = backend.run_log_transform(matrix, base=10.0, offset=1.0)

            self.assertEqual(payload["parameters"]["base"], 10.0)
            self.assertEqual(payload["parameters"]["offset"], 1.0)
            self.assertAlmostEqual(float(transformed.values[0, 0]), 0.0, places=6)
            self.assertAlmostEqual(float(transformed.values[0, 1]), 1.0, places=6)
            self.assertAlmostEqual(float(transformed.values[1, 0]), 2.0, places=6)

    def test_run_log_transform_rejects_invalid_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ng1\t0\n", encoding="utf-8")
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(matrix_path), row_id_column="gene")

            with self.assertRaises(ProviderError):
                backend.run_log_transform(matrix, base=1.0, offset=1.0)
            with self.assertRaises(ProviderError):
                backend.run_log_transform(matrix, base=2.0, offset=-1.0)
            with self.assertRaises(ProviderError):
                backend.run_log_transform(matrix, base=2.0, offset=0.0)

    def test_run_hierarchical_clustering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            result = backend.run_hierarchical_clustering(matrix, analysis_axis="columns", cluster_count=2)

            self.assertEqual(result["analysis"], "hierarchical_clustering")
            self.assertEqual(result["cluster_count"], 2)
            self.assertEqual(len(result["clusters"]), 2)
            self.assertEqual(len(result["order"]), 4)

    def test_run_correlation_pairwise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            result = backend.run_correlation(matrix, analysis_axis="columns", method="pearson", top_n=3)

            self.assertEqual(result["analysis"], "correlation")
            self.assertEqual(result["mode"], "pairwise")
            self.assertEqual(len(result["correlations"]), 3)
            self.assertAlmostEqual(float(result["correlations"][0]["correlation"]), 1.0, places=6)

    def test_run_correlation_grouped_vector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            vector_path = root / "vector.tsv"
            vector_path.write_text(
                "gene\tscore\n"
                "g1\t0.1\n"
                "g2\t0.2\n"
                "g3\t0.9\n"
                "g4\t1.0\n",
                encoding="utf-8",
            )
            vector_matrix = backend.load_matrix(matrix_path=str(vector_path), row_id_column="gene")
            result = backend.run_correlation(
                matrix,
                analysis_axis="columns",
                secondary_matrix=vector_matrix,
                secondary_value_column="score",
                secondary_alignment_axis="features",
                group_labels=["A", "A", "B", "B"],
            )

            self.assertEqual(result["analysis"], "correlation")
            self.assertEqual(result["mode"], "grouped_vector")
            self.assertEqual(result["overall"]["r"], result["overall"]["correlation"])
            self.assertEqual(len(result["correlations"]), 2)
            self.assertIn("r", result["correlations"][0])

    def test_load_matrix_honors_header_row_and_skip_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            matrix_path.write_text(
                "ignore this line\n"
                "gene\ts1\ts2\n"
                "g1\t1\t2\n"
                "g2\t3\t4\n",
                encoding="utf-8",
            )
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                header_row=2,
                skip_rows=1,
            )

            self.assertEqual(matrix.row_labels, ["g1", "g2"])
            self.assertEqual(matrix.column_labels, ["s1", "s2"])

    def test_run_consensus_clustering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            result = backend.run_consensus_clustering(
                matrix,
                analysis_axis="columns",
                cluster_count=2,
                iterations=10,
                sample_fraction=1.0,
                feature_fraction=1.0,
                random_seed=7,
            )

            self.assertEqual(result["analysis"], "consensus_clustering")
            self.assertEqual(result["cluster_count"], 2)
            self.assertEqual(len(result["stability"]), 4)
            self.assertEqual(len(result["order"]), 4)
            self.assertEqual(result["consistency_summary"]["scope"], "all_observations")
            self.assertEqual(result["consistency_summary"]["observation_count"], 4)
            self.assertTrue(result["consistency_summary"]["counts_at_thresholds"])

    def test_consensus_clustering_normalizes_pearson_metric_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            result = backend.run_consensus_clustering(
                matrix,
                analysis_axis="columns",
                cluster_count=2,
                iterations=5,
                sample_fraction=1.0,
                feature_fraction=1.0,
                random_seed=7,
                metric="pearson",
            )

            self.assertEqual(result["parameters"]["metric"], "correlation")
            self.assertEqual(result["parameters"]["requested_metric"], "pearson")

    def test_hierarchical_clustering_accepts_spearman_metric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ExpressionMatrixAnalysisBackend()
            matrix = backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            result = backend.run_hierarchical_clustering(
                matrix,
                analysis_axis="columns",
                cluster_count=2,
                metric="spearman",
            )

            self.assertEqual(result["analysis"], "hierarchical_clustering")
            self.assertEqual(result["parameters"]["metric"], "spearman")


if __name__ == "__main__":
    unittest.main()
