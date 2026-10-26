from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import pandas as pd

from gleskit_mcp.analysis import ExpressionStatisticsBackend
from gleskit_mcp.tabular import load_table


class ExpressionStatisticsBackendTests(unittest.TestCase):
    def test_qpcr_matrix_infers_groups_and_applies_log2_with_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            matrix_path = Path(tmp) / "qpcr.tsv"
            matrix_path.write_text(
                "feature\tP_1\tP_2\tC_1\tC_2\n"
                "miR-1\t2\t4\t8\t16\n"
                "miR-2\t4\t4\t4\t4\n",
                encoding="utf-8",
            )

            result = ExpressionStatisticsBackend().matrix_featurewise_group_test(
                matrix_path=str(matrix_path),
                row_id_column="feature",
                group_a="P",
                group_b="C",
                test="welch",
                value_transform="log2",
            )

            self.assertEqual(result["result"]["tested_feature_count"], 2)
            self.assertEqual(result["input"]["value_transform"], "log2")
            self.assertEqual(result["input"]["sample_group_map"], {"P_1": "P", "P_2": "P", "C_1": "C", "C_2": "C"})
            self.assertIn("significant_feature_count_bh", result["result"])
            self.assertIn("significant_feature_count_by", result["result"])
            self.assertIn("significant_feature_count_bonferroni", result["result"])
            self.assertIn("significant_feature_count_raw", result["result"])
            self.assertIn("significant_feature_percentage_raw", result["result"])
            self.assertIn("significant_feature_count_all_corrections", result["result"])

    def test_threshold_count_handles_missing_values_and_exact_column(self) -> None:
        result = ExpressionStatisticsBackend().threshold_count(
            table_text="gene\tdispersion\nG1\t0.000001\nG2\t0.2\nG3\tNA\n",
            value_column="dispersion",
            threshold=1e-5,
            operator="<",
        )

        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["result"]["tested_count"], 2)
        self.assertEqual(result["result"]["missing_count"], 1)

    def test_qpcr_group_inference_requires_unambiguous_labels(self) -> None:
        with self.assertRaisesRegex(Exception, "sample_group_map"):
            ExpressionStatisticsBackend().matrix_group_test(
                table_text="feature\tsample_a\tsample_b\nG1\t1\t2\n",
                row_id_column="feature",
                test="anova",
            )

    def test_qpcr_featurewise_test_omits_missing_measurements_per_feature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            matrix_path = Path(tmp) / "qpcr_missing.tsv"
            matrix_path.write_text(
                "feature\tP_1\tP_2\tP_3\tC_1\tC_2\tC_3\n"
                "miR-1\t2\tNAN\t4\t8\t9\t10\n"
                "miR-2\t4\t5\t6\t4\t5\t6\n",
                encoding="utf-8",
            )

            result = ExpressionStatisticsBackend().matrix_featurewise_group_test(
                matrix_path=str(matrix_path),
                row_id_column="feature",
                group_a="P",
                group_b="C",
                test="welch",
                missing_value_policy="omit",
            )

            self.assertEqual(result["input"]["missing_value_policy"], "omit")
            self.assertEqual(result["input"]["missing_value_count"], 1)
            self.assertEqual(result["result"]["tested_feature_count"], 2)
            missing_feature = next(row for row in result["features"] if row["feature"] == "miR-1")
            self.assertEqual(missing_feature["group_counts"], {"P": 2, "C": 3})

    def test_load_table_accepts_wide_csv_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.csv"
            path.write_text("gene,value\nG1," + ("x" * 200_000) + "\n", encoding="utf-8")

            loaded = load_table(path=str(path), dtype=str, keep_default_na=False)

            self.assertEqual(loaded.frame.iloc[0, 0], "G1")
            self.assertEqual(len(str(loaded.frame.iloc[0, 1])), 200_000)

    def test_load_table_uses_content_not_xls_suffix_for_delimited_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "COAD__geneExp.xls"
            path.write_text(
                "gene\tcell_line_a\tcell_line_b\n"
                "G1\t1.5\t2.5\n",
                encoding="utf-8",
            )

            loaded = load_table(path=str(path), dtype=str, keep_default_na=False)

            self.assertEqual(loaded.file_format, "delimited")
            self.assertEqual(loaded.frame.columns.tolist(), ["gene", "cell_line_a", "cell_line_b"])
            self.assertEqual(loaded.frame.iloc[0].tolist(), ["G1", "1.5", "2.5"])

    def test_distribution_summary_allows_duplicate_feature_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expression.tsv"
            path.write_text(
                "gene\ts1\ts2\n"
                "G1\t1\t2\n"
                "G1\t3\t4\n",
                encoding="utf-8",
            )

            result = ExpressionStatisticsBackend().distribution_summary(
                source_path=str(path),
                row_id_column="gene",
                scope="all_values",
            )

            self.assertEqual(result["result"]["value_count"], 4)

    def test_matrix_annotation_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            annotation_path = root / "gene_lengths.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\n"
                "G1\t1\t1\t1\n"
                "G2\t2\t2\t2\n"
                "G3\t3\t3\t3\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "gene\tlength\n"
                "G1\t10\n"
                "G2\t20\n"
                "G3\t30\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_annotation_correlation(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                analysis_axis="columns",
                target_axis="features",
                summary_stat="mean",
                annotation_path=str(annotation_path),
                annotation_key_column="gene",
                annotation_value_column="length",
                method="pearson",
            )

            self.assertEqual(result["analysis"], "correlation")
            self.assertAlmostEqual(float(result["result"]["correlation"]), 1.0, places=6)
            self.assertEqual(result["result"]["label_count"], 3)

    def test_matrix_annotation_correlation_infers_annotation_key_column_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            annotation_path = root / "gene_lengths.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\n"
                "G1\t1\t1\t1\n"
                "G2\t2\t2\t2\n"
                "G3\t3\t3\t3\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "Geneid\tlength\n"
                "G1\t10\n"
                "G2\t20\n"
                "G3\t30\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_annotation_correlation(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                analysis_axis="columns",
                target_axis="features",
                summary_stat="mean",
                annotation_path=str(annotation_path),
                annotation_key_column="",
                annotation_value_column="length",
                method="pearson",
            )

            self.assertEqual(result["input"]["annotation_key_column"], "Geneid")
            self.assertTrue(any("Inferred annotation key column" in note for note in result["resolution_notes"]))

    def test_matrix_annotation_correlation_recovers_blank_first_row_id_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            annotation_path = root / "gene_lengths.tsv"
            matrix_path.write_text(
                "\ts1\ts2\ts3\n"
                "G1\t1\t1\t1\n"
                "G2\t2\t2\t2\n"
                "G3\t3\t3\t3\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "Geneid\tlength\n"
                "G1\t10\n"
                "G2\t20\n"
                "G3\t30\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_annotation_correlation(
                matrix_path=str(matrix_path),
                row_id_column="gene_id",
                analysis_axis="columns",
                target_axis="features",
                summary_stat="mean",
                annotation_path=str(annotation_path),
                annotation_key_column="Geneid",
                annotation_value_column="length",
                method="pearson",
            )

            self.assertEqual(result["input"]["row_id_column"], "column_1")
            self.assertTrue(any("used first matrix column" in note for note in result["resolution_notes"]))

    def test_matrix_annotation_correlation_accepts_unnamed_blank_header_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.csv"
            annotation_path = root / "gene_lengths.csv"
            matrix_path.write_text(
                ",s1,s2,s3\n"
                "G1,1,1,1\n"
                "G2,2,2,2\n"
                "G3,3,3,3\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "Geneid,length\n"
                "G1,10\n"
                "G2,20\n"
                "G3,30\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_annotation_correlation(
                matrix_path=str(matrix_path),
                row_id_column="Unnamed: 0",
                analysis_axis="columns",
                target_axis="features",
                summary_stat="mean",
                annotation_path=str(annotation_path),
                annotation_key_column="Geneid",
                annotation_value_column="length",
                method="pearson",
            )

            self.assertEqual(result["input"]["row_id_column"], "column_1")
            self.assertTrue(any("blank first header" in note for note in result["resolution_notes"]))

    def test_matrix_annotation_correlation_applies_annotation_subgroup_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            annotation_path = root / "gene_lengths.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\n"
                "G1\t10\t10\n"
                "G2\t20\t20\n"
                "G3\t30\t30\n"
                "G4\t999\t999\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "gene\tlength\tbiotype\n"
                "G1\t10\tprotein_coding\n"
                "G2\t20\tprotein_coding\n"
                "G3\t30\tprotein_coding\n"
                "G4\t40\tlncRNA\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_annotation_correlation(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                analysis_axis="columns",
                target_axis="features",
                summary_stat="mean",
                annotation_path=str(annotation_path),
                annotation_key_column="gene",
                annotation_value_column="length",
                annotation_group_column="biotype",
                annotation_group_value="protein_coding",
                method="pearson",
            )

            self.assertEqual(result["result"]["label_count"], 3)
            self.assertEqual(result["input"]["annotation_group_column"], "biotype")
            self.assertEqual(result["input"]["annotation_group_value"], "protein_coding")
            self.assertEqual(result["input"]["matched_annotation_rows"], 3)
            self.assertIn("protein_coding", result["summary"])

    def test_matrix_annotation_correlation_filters_samples_with_metadata_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.csv"
            annotation_path = root / "gene_lengths.csv"
            metadata_path = root / "sample_metadata.csv"
            matrix_path.write_text(
                ",S1CD4,S2CD4,S3CD8,S4CD8\n"
                "G1,1,3,100,100\n"
                "G2,2,6,50,50\n"
                "G3,3,9,25,25\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "Geneid,Length,gene_biotype\n"
                "G1,10,protein_coding\n"
                "G2,20,protein_coding\n"
                "G3,30,protein_coding\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample,celltype\n"
                "S1CD4,CD4\n"
                "S2CD4,CD4\n"
                "S3CD8,CD8\n"
                "S4CD8,CD8\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_annotation_correlation(
                matrix_path=str(matrix_path),
                row_id_column="Unnamed: 0",
                analysis_axis="columns",
                target_axis="features",
                summary_stat="mean",
                annotation_path=str(annotation_path),
                annotation_key_column="Geneid",
                annotation_value_column="Length",
                annotation_group_column="gene_biotype",
                annotation_group_value="protein_coding",
                metadata_path=str(metadata_path),
                metadata_group_column="celltype",
                metadata_group_value="CD4",
                method="pearson",
            )

            self.assertAlmostEqual(float(result["result"]["correlation"]), 1.0, places=6)
            self.assertEqual(result["input"]["metadata_group_column"], "celltype")
            self.assertEqual(result["input"]["metadata_group_value"], "CD4")
            self.assertEqual(result["input"]["selected_sample_count"], 2)
            self.assertEqual(result["input"]["matched_sample_count"], 4)

    def test_distribution_summary_and_group_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table_path = root / "values.tsv"
            table_path.write_text(
                "value\tgroup\n"
                "1\tA\n"
                "2\tA\n"
                "7\tB\n"
                "8\tB\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            distribution = backend.distribution_summary(
                source_path=str(table_path),
                value_column="value",
                scope="column",
            )
            group_test = backend.group_test_from_table(
                source_path=str(table_path),
                value_column="value",
                group_column="group",
                test="welch",
            )

            self.assertEqual(distribution["analysis"], "distribution")
            self.assertEqual(distribution["result"]["value_count"], 4)
            self.assertEqual(group_test["analysis"], "group_test")
            self.assertLess(float(group_test["result"]["p_value"]), 0.1)

    def test_featurewise_group_test_and_exact_binomial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table_path = root / "feature_table.tsv"
            table_path.write_text(
                "gene\tgroup\tf1\tf2\n"
                "g1\tA\t1\t2\n"
                "g2\tA\t2\t3\n"
                "g3\tB\t7\t8\n"
                "g4\tB\t8\t9\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            featurewise = backend.featurewise_group_test(
                source_path=str(table_path),
                row_id_column="gene",
                group_column="group",
                test="welch",
                alpha=0.1,
            )
            exact = backend.exact_binomial_test(success_count=7, trial_count=10, null_probability=0.5)

            self.assertEqual(featurewise["analysis"], "featurewise_group_test")
            self.assertEqual(featurewise["result"]["feature_count"], 2)
            self.assertGreaterEqual(featurewise["result"]["significant_feature_count_bh"], 1)
            self.assertEqual(exact["analysis"], "exact_binomial_test")
            self.assertEqual(exact["result"]["success_count"], 7)
            self.assertAlmostEqual(float(exact["result"]["observed_rate"]), 0.7, places=6)
            self.assertIn("confidence_interval", exact["result"])

    def test_proportion_interval_and_multiple_testing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pvalues_path = root / "pvalues.tsv"
            pvalues_path.write_text("p_value\n0.001\n0.01\n0.2\n0.8\n", encoding="utf-8")

            backend = ExpressionStatisticsBackend()
            interval = backend.proportion_interval(success_count=8, trial_count=10)
            multiple = backend.multiple_testing_summary(
                source_path=str(pvalues_path),
                p_value_column="p_value",
                methods=["bh", "bonferroni"],
            )

            self.assertEqual(interval["analysis"], "proportion_interval")
            self.assertAlmostEqual(float(interval["result"]["proportion"]), 0.8, places=6)
            self.assertEqual(multiple["analysis"], "multiple_testing_summary")
            self.assertEqual(len(multiple["methods"]), 2)

    def test_matrix_featurewise_group_test_with_metadata_and_feature_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            metadata_path = root / "metadata.tsv"
            feature_annotation_path = root / "feature_annotation.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\ts4\n"
                "G1\t1\t1\t10\t10\n"
                "G2\t2\t2\t20\t20\n"
                "G3\t5\t5\t5\t5\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tgroup\n"
                "s1\tA\n"
                "s2\tA\n"
                "s3\tB\n"
                "s4\tB\n",
                encoding="utf-8",
            )
            feature_annotation_path.write_text(
                "gene\tbiotype\n"
                "G1\tprotein_coding\n"
                "G2\tprotein_coding\n"
                "G3\tlncRNA\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_featurewise_group_test(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                metadata_path=str(metadata_path),
                metadata_key_column="sample",
                metadata_group_column="group",
                group_a="A",
                group_b="B",
                feature_annotation_path=str(feature_annotation_path),
                feature_annotation_key_column="gene",
                feature_annotation_group_column="biotype",
                feature_annotation_group_value="protein_coding",
                test="welch",
                alpha=0.1,
            )

            self.assertEqual(result["analysis"], "matrix_featurewise_group_test")
            self.assertEqual(result["result"]["feature_count"], 2)
            self.assertEqual(result["result"]["tested_feature_count"], 2)
            self.assertGreaterEqual(result["result"]["significant_feature_count_bh"], 1)

    def test_matrix_group_fold_change_summary_accepts_same_workbook_sheets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = root / "expression.xlsx"
            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    {
                        "gene": ["G1", "G2", "G3"],
                        "s1": [1, 2, 5],
                        "s2": [1, 2, 5],
                        "s3": [10, 20, 5],
                        "s4": [10, 20, 5],
                    }
                ).to_excel(writer, sheet_name="matrix", index=False)
                pd.DataFrame(
                    {
                        "sample": ["s1", "s2", "s3", "s4"],
                        "group": ["A", "A", "B", "B"],
                    }
                ).to_excel(writer, sheet_name="metadata", index=False)

            backend = ExpressionStatisticsBackend()
            result = backend.matrix_group_fold_change_summary(
                matrix_path=str(workbook_path),
                row_id_column="gene",
                sheet_name="matrix",
                metadata_path=str(workbook_path),
                metadata_sheet_name="metadata",
                metadata_key_column="sample",
                metadata_group_column="group",
                group_a="B",
                group_b="A",
                summary_stat="median",
            )

            self.assertEqual(result["analysis"], "matrix_group_fold_change_summary")
            self.assertEqual(result["result"]["feature_count"], 3)
            self.assertGreater(float(result["result"]["summary_value"]), 0.0)

    def test_matrix_group_test_supports_flattened_and_featurewise_anova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            metadata_path = root / "metadata.tsv"
            matrix_path.write_text(
                "gene\ta1\ta2\tb1\tb2\tc1\tc2\n"
                "G1\t1\t2\t2\t3\t4\t5\n"
                "G2\t2\t3\t3\t4\t5\t6\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tgroup\n"
                "a1\tA\n"
                "a2\tA\n"
                "b1\tB\n"
                "b2\tB\n"
                "c1\tC\n"
                "c2\tC\n",
                encoding="utf-8",
            )

            backend = ExpressionStatisticsBackend()
            flattened = backend.matrix_group_test(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                metadata_path=str(metadata_path),
                metadata_key_column="sample",
                metadata_group_column="group",
                metadata_group_values=["A", "B", "C"],
                test="anova",
                observation_unit="flattened_values",
            )
            featurewise = backend.matrix_group_test(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                metadata_path=str(metadata_path),
                metadata_key_column="sample",
                metadata_group_column="group",
                metadata_group_values=["A", "B", "C"],
                test="anova",
                observation_unit="featurewise",
            )

            self.assertEqual(flattened["analysis"], "matrix_group_test")
            self.assertEqual(flattened["input"]["observation_unit"], "flattened_values")
            self.assertEqual(flattened["result"]["statistic_name"], "F")
            self.assertGreater(float(flattened["result"]["statistic"]), 0.0)
            self.assertLess(float(flattened["result"]["p_value"]), 0.05)
            self.assertEqual(featurewise["result"]["tested_feature_count"], 2)

    def test_matrix_group_test_supports_pairwise_log2_fold_change_anova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            metadata_path = root / "metadata.tsv"
            matrix_path.write_text(
                "gene\ta1\ta2\tb1\tb2\tc1\tc2\td1\td2\n"
                "G1\t10\t12\t11\t13\t9\t10\t14\t15\n"
                "G2\t20\t21\t19\t20\t22\t24\t18\t17\n"
                "G3\t5\t6\t7\t8\t4\t5\t6\t7\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tcelltype\n"
                "a1\tA\n"
                "a2\tA\n"
                "b1\tB\n"
                "b2\tB\n"
                "c1\tC\n"
                "c2\tC\n"
                "d1\tD\n"
                "d2\tD\n",
                encoding="utf-8",
            )

            result = ExpressionStatisticsBackend().matrix_group_test(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                metadata_path=str(metadata_path),
                metadata_key_column="sample",
                metadata_group_column="celltype",
                metadata_group_values=["A", "B", "C", "D"],
                test="anova",
                observation_unit="pairwise_log2_fold_change",
                pseudocount=1.0,
            )

            self.assertEqual(result["input"]["observation_unit"], "pairwise_log2_fold_change")
            self.assertEqual(result["result"]["pair_count"], 6)
            self.assertEqual(result["result"]["observation_count"], 18)
            self.assertEqual(result["result"]["statistic_name"], "F")
            self.assertEqual(len(result["groups"]), 6)

    def test_matrix_log2_fold_change_distribution_uses_all_group_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_path = root / "matrix.tsv"
            metadata_path = root / "metadata.tsv"
            matrix_path.write_text(
                "gene\ta1\ta2\tb1\tb2\tc1\tc2\n"
                "G1\t1\t1\t2\t2\t4\t4\n"
                "G2\t2\t2\t4\t4\t8\t8\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tgroup\n"
                "a1\tA\n"
                "a2\tA\n"
                "b1\tB\n"
                "b2\tB\n"
                "c1\tC\n"
                "c2\tC\n",
                encoding="utf-8",
            )

            result = ExpressionStatisticsBackend().matrix_log2_fold_change_distribution(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                metadata_path=str(metadata_path),
                metadata_key_column="sample",
                metadata_group_column="group",
                metadata_group_values=["A", "B", "C"],
            )

            self.assertEqual(result["analysis"], "matrix_log2_fold_change_distribution")
            self.assertEqual(result["result"]["pair_count"], 3)
            self.assertEqual(result["result"]["value_count"], 6)

    def test_matrix_group_dataset_recovers_shifted_workbook_header_and_metadata_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = root / "expression.xlsx"

            from openpyxl import Workbook

            workbook = Workbook()
            matrix_sheet = workbook.active
            matrix_sheet.title = "NormCount"
            matrix_sheet.append(["", "", "baseline blood"])
            matrix_sheet.append(["", "", ""])
            matrix_sheet.append(["", "", ""])
            matrix_sheet.append(
                [
                    "Index",
                    "GeneID",
                    "Control_baseline_blood1",
                    "Control_baseline_blood2",
                    "Control_final_blood1",
                    "Control_final_blood2",
                    "Control_dentate_gyrus1",
                    "DEG",
                    "baseMean",
                ]
            )
            matrix_sheet.append([1, "G1", 1, 1, 10, 10, 4, "-", 5.5])
            matrix_sheet.append([2, "G2", 2, 2, 12, 12, 5, "-", 6.5])
            matrix_sheet.append([3, "G3", 5, 5, 5, 5, 5, "-", 7.5])

            metadata_sheet = workbook.create_sheet("Mapping_statistics")
            metadata_sheet.append(["Index", "Sample ID", "Response", "Tissue", "Code", "Sample ID"])
            metadata_sheet.append([1, "internal_a", "Control", "baseline_blood", "Control_baseline_blood1", "unused_a"])
            metadata_sheet.append([2, "internal_b", "Control", "baseline_blood", "Control_baseline_blood2", "unused_b"])
            metadata_sheet.append([3, "internal_c", "Control", "final_blood", "Control_final_blood1", "unused_c"])
            metadata_sheet.append([4, "internal_d", "Control", "final_blood", "Control_final_blood2", "unused_d"])
            metadata_sheet.append([5, "internal_e", "Control", "dentate_gyrus", "Control_dentate_gyrus1", "unused_e"])
            workbook.save(workbook_path)

            backend = ExpressionStatisticsBackend()
            result = backend._prepare_matrix_group_dataset(
                matrix_path=str(workbook_path),
                table_text="",
                row_id_column="",
                delimiter="",
                sheet_name="NormCount",
                header_row=1,
                skip_rows=0,
                transpose=False,
                analysis_axis="columns",
                metadata_path=str(workbook_path),
                metadata_table_text="",
                metadata_delimiter="",
                metadata_sheet_name="Mapping_statistics",
                metadata_header_row=1,
                metadata_skip_rows=0,
                metadata_key_column="Sample ID",
                metadata_group_column="",
                group_a="Final blood",
                group_b="Baseline blood",
                feature_annotation_path="",
                feature_annotation_table_text="",
                feature_annotation_delimiter="",
                feature_annotation_sheet_name="",
                feature_annotation_header_row=1,
                feature_annotation_skip_rows=0,
                feature_annotation_key_column="",
                feature_annotation_group_column="",
                feature_annotation_group_value="",
            )

            self.assertEqual(result["input"]["row_id_column"], "GeneID")
            self.assertEqual(result["input"]["metadata_key_column"], "Code")
            self.assertEqual(result["input"]["metadata_group_column"], "Tissue")
            self.assertEqual(result["input"]["matched_sample_count"], 5)
            self.assertEqual(result["input"]["sample_count"], 4)
            self.assertEqual(result["values"].shape, (4, 3))
            self.assertIn("baseline_blood", result["input"]["available_groups"])
            self.assertIn("final_blood", result["input"]["available_groups"])
            self.assertTrue(any("Auto-detected matrix header row 4" in note for note in result["input"]["resolution_notes"]))
            self.assertTrue(any("used 'Code' instead" in note for note in result["input"]["resolution_notes"]))


if __name__ == "__main__":
    unittest.main()
