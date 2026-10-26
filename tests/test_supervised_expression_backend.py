from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from gleskit_mcp.analysis import ExpressionMatrixAnalysisBackend, SupervisedExpressionBackend
from gleskit_mcp.providers.base import ProviderError


class SupervisedExpressionBackendTests(unittest.TestCase):
    def _write_matrix(self, root: Path) -> Path:
        matrix_path = root / "matrix.tsv"
        matrix_path.write_text(
            "gene\ts1\ts2\ts3\ts4\ts5\ts6\ts7\ts8\n"
            "G1\t0\t0\t0\t1\t8\t9\t9\t10\n"
            "G2\t1\t1\t2\t2\t8\t8\t9\t9\n"
            "G3\t0\t1\t0\t1\t7\t8\t7\t8\n",
            encoding="utf-8",
        )
        return matrix_path

    def test_run_supervised_model_classification_with_stratified_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_backend = ExpressionMatrixAnalysisBackend()
            backend = SupervisedExpressionBackend(matrix_backend)
            matrix = matrix_backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tcondition\n"
                "s1\tA\ns2\tA\ns3\tA\ns4\tA\n"
                "s5\tB\ns6\tB\ns7\tB\ns8\tB\n",
                encoding="utf-8",
            )

            result = backend.run_supervised_model(
                matrix,
                metadata_path=str(metadata_path),
                analysis_axis="columns",
                sample_column="sample",
                target_column="condition",
                task="classification",
                model_family="logistic_regression",
                split_strategy="stratified_train_test",
                test_size=0.25,
                random_seed=7,
            )

            self.assertEqual(result["analysis"], "supervised_expression_model")
            self.assertEqual(result["input"]["task"], "classification")
            self.assertGreaterEqual(float(result["metrics"]["accuracy"]), 0.5)
            self.assertEqual(result["split"]["test_count"], 2)

    def test_run_supervised_model_regression_with_group_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_backend = ExpressionMatrixAnalysisBackend()
            backend = SupervisedExpressionBackend(matrix_backend)
            matrix = matrix_backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tresponse\tbatch\n"
                "s1\t0.1\tb1\ns2\t0.2\tb1\ns3\t0.4\tb2\ns4\t0.5\tb2\n"
                "s5\t1.4\tb3\ns6\t1.5\tb3\ns7\t1.8\tb4\ns8\t2.0\tb4\n",
                encoding="utf-8",
            )

            result = backend.run_supervised_model(
                matrix,
                metadata_path=str(metadata_path),
                analysis_axis="columns",
                sample_column="sample",
                target_column="response",
                task="regression",
                model_family="ridge",
                split_strategy="group_train_test",
                group_column="batch",
                test_size=0.25,
                random_seed=3,
            )

            self.assertEqual(result["input"]["task"], "regression")
            self.assertIn("rmse", result["metrics"])
            self.assertGreaterEqual(result["split"]["train_count"], 4)

    def test_run_supervised_model_rejects_bad_targets_or_orientation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_backend = ExpressionMatrixAnalysisBackend()
            backend = SupervisedExpressionBackend(matrix_backend)
            matrix = matrix_backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")

            bad_target_path = root / "bad_target.tsv"
            bad_target_path.write_text(
                "sample\tresponse\ns1\ta\ns2\tb\ns3\tc\ns4\td\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProviderError):
                backend.run_supervised_model(
                    matrix,
                    metadata_path=str(bad_target_path),
                    analysis_axis="columns",
                    sample_column="sample",
                    target_column="response",
                    task="regression",
                    model_family="linear_regression",
                )

            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tcondition\n"
                "s1\tA\ns2\tA\ns3\tA\ns4\tA\n"
                "s5\tB\ns6\tB\ns7\tB\ns8\tB\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProviderError):
                backend.run_supervised_model(
                    matrix,
                    metadata_path=str(metadata_path),
                    analysis_axis="rows",
                    sample_column="sample",
                    target_column="condition",
                    task="classification",
                    model_family="logistic_regression",
                )

    def test_evaluate_cluster_agreement_adds_held_out_metrics_beyond_consensus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_backend = ExpressionMatrixAnalysisBackend()
            backend = SupervisedExpressionBackend(matrix_backend)
            matrix = matrix_backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")

            consensus = matrix_backend.run_consensus_clustering(
                matrix,
                analysis_axis="columns",
                cluster_count=2,
                iterations=5,
                sample_fraction=1.0,
                feature_fraction=1.0,
                random_seed=4,
            )
            agreement = backend.evaluate_cluster_agreement(
                matrix,
                analysis_axis="columns",
                cluster_count=2,
                iterations=5,
                train_fraction=0.75,
                random_seed=4,
            )

            self.assertEqual(consensus["analysis"], "consensus_clustering")
            self.assertNotIn("agreement", consensus)
            self.assertEqual(agreement["analysis"], "cluster_agreement")
            self.assertIn("agreement", agreement)
            self.assertIn("rate", agreement["agreement"])
            self.assertEqual(agreement["evaluation_scope"]["agreement_population"], "test_only")
            self.assertEqual(agreement["agreement"]["count"], agreement["agreement"]["count_test_only"])
            self.assertEqual(agreement["agreement"]["rate"], agreement["agreement"]["rate_test_only"])

    def test_evaluate_cluster_agreement_normalizes_accuracy_metric_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_backend = ExpressionMatrixAnalysisBackend()
            backend = SupervisedExpressionBackend(matrix_backend)
            matrix = matrix_backend.load_matrix(matrix_path=str(self._write_matrix(root)), row_id_column="gene")

            agreement = backend.evaluate_cluster_agreement(
                matrix,
                analysis_axis="columns",
                cluster_count=2,
                iterations=5,
                train_fraction=0.75,
                random_seed=4,
                metric="accuracy",
            )

            self.assertEqual(agreement["analysis"], "cluster_agreement")
            self.assertEqual(agreement["input"]["metric"], "correlation")
            self.assertEqual(agreement["input"]["requested_metric"], "accuracy")


if __name__ == "__main__":
    unittest.main()
