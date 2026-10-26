from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from gleskit_mcp.eval.results import EvaluationResults, RunRef


class EvaluationResultsTests(unittest.TestCase):
    def _write_bixbench_run(self, root: Path, name: str, rows: list[dict[str, object]]) -> Path:
        run_root = root / "var" / "bixbench" / "runs" / name
        summary_root = run_root / "summary"
        summary_root.mkdir(parents=True)
        pd.DataFrame(rows).to_csv(summary_root / "questions.csv", index=False)
        return run_root

    def _write_repobench_run(self, root: Path, name: str, rows: list[dict[str, object]]) -> Path:
        run_root = root / "var" / "repobench" / "runs" / name
        summary_root = run_root / "summary"
        summary_root.mkdir(parents=True)
        pd.DataFrame(rows).to_csv(summary_root / "tasks.csv", index=False)
        return run_root

    def _write_bixbench_metadata(self, root: Path, rows: list[dict[str, object]]) -> None:
        metadata_path = root / "var" / "bixbench" / "hf_repo" / "BixBench.gleskit_scope.jsonl"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )

    def _bix_rows(self, q1_score: float, q2_score: float) -> list[dict[str, object]]:
        return [
            {
                "question_id": "q1",
                "status": "completed",
                "answer_present": True,
                "abstain": False,
                "committee_category": "accept",
                "committee_level_mean_final": q1_score,
                "judge_score": q1_score / 4,
            },
            {
                "question_id": "q2",
                "status": "completed",
                "answer_present": True,
                "abstain": False,
                "committee_category": "partial",
                "committee_level_mean_final": q2_score,
                "judge_score": q2_score / 4,
            },
            {
                "question_id": "q3",
                "status": "completed",
                "answer_present": False,
                "abstain": True,
                "committee_category": None,
                "committee_level_mean_final": None,
                "judge_score": None,
            },
        ]

    def test_list_runs_reports_available_and_unfinished_run_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_bixbench_run(root, "bix-ready", self._bix_rows(4.0, 1.0))
            (root / "var" / "bixbench" / "runs" / "bix-unfinished").mkdir(parents=True)
            self._write_repobench_run(
                root,
                "repo-ready",
                [{"task_id": "task-1", "committee_level_mean_final": 3.5, "status": "completed", "report_saved": True}],
            )

            catalog = EvaluationResults(root).list_runs()

            self.assertEqual(set(catalog["source_run"]), {
                "bixbench:bix-ready",
                "bixbench:bix-unfinished",
                "repobench:repo-ready",
            })
            unfinished = catalog.loc[catalog["run_name"].eq("bix-unfinished")].iloc[0]
            self.assertFalse(unfinished["summary_available"])
            self.assertTrue(pd.isna(unfinished["item_count"]))
            bix_ready = catalog.loc[catalog["run_name"].eq("bix-ready")].iloc[0]
            self.assertEqual(bix_ready["item_count"], 3)
            self.assertEqual(bix_ready["answered_count"], 2)
            self.assertEqual(bix_ready["rejected_count"], 1)
            self.assertEqual(bix_ready["unanswered_count"], 1)
            self.assertEqual(bix_ready["judge_scored_count"], 2)
            self.assertEqual(bix_ready["judge_missing_count"], 1)

    def test_load_run_normalizes_scores_and_bixbench_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_root = self._write_bixbench_run(root, "bix-ready", self._bix_rows(4.0, 1.0))
            episode_root = run_root / "episodes" / "q1"
            session_path = episode_root / "private" / "attempts" / "attempt_01" / "session_export.json"
            session_path.parent.mkdir(parents=True)
            session_path.write_text(
                json.dumps(
                    {
                        "session": {
                            "tokens_input": 100,
                            "tokens_output": 50,
                            "tokens_reasoning": 25,
                            "tokens_cache_read": 20,
                            "cost": 0.004,
                        }
                    }
                ),
                encoding="utf-8",
            )
            (episode_root / "episode.json").write_text(
                json.dumps({"episode_id": "q1", "private_session_export_path": str(session_path)}),
                encoding="utf-8",
            )

            loaded = EvaluationResults(root).load_run("bix-ready", benchmark="bixbench")

            self.assertEqual(loaded["item_id"].tolist(), ["q1", "q2", "q3"])
            self.assertEqual(loaded["source_run"].iloc[0], "bixbench:bix-ready")
            self.assertEqual(loaded.loc[loaded["item_id"].eq("q1"), "normalized_score"].iloc[0], 1.0)
            self.assertTrue(loaded.loc[loaded["item_id"].eq("q3"), "score_missing"].iloc[0])
            self.assertEqual(loaded.loc[loaded["item_id"].eq("q1"), "tokens_input"].iloc[0], 100.0)

            priced = EvaluationResults.add_token_costs(
                loaded,
                {"input_per_mtok": 1.0, "output_per_mtok": 2.0, "cache_read_per_mtok": 0.5},
            )
            self.assertAlmostEqual(priced.loc[priced["item_id"].eq("q1"), "computed_cost"].iloc[0], 0.00026)

    def test_load_run_can_enrich_bixbench_metadata_and_judge_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_root = self._write_bixbench_run(root, "bix-ready", self._bix_rows(4.0, 1.0))
            self._write_bixbench_metadata(
                root,
                [
                    {
                        "id": "case-uuid-1",
                        "question_id": "q1",
                        "species": "Homo sapiens",
                        "categories": "RNA-seq, Differential Expression Analysis",
                        "paper": "https://example.test/paper",
                        "hypothesis": "A useful biological hypothesis.",
                        "tag": "bixbench",
                        "version": "1.5",
                        "data_folder": "case-1.zip",
                    },
                    {
                        "id": "case-uuid-2",
                        "question_id": "q2",
                        "categories": "['Single-Cell Analysis', 'Transcriptomics']",
                    },
                ],
            )
            judge_path = run_root / "episodes" / "q1" / "judge.json"
            judge_path.parent.mkdir(parents=True)
            judge_path.write_text(
                json.dumps(
                    {
                        "question_id": "q1",
                        "specialist_disagreement_span": 2,
                        "review_required": True,
                        "review_reasons": ["material_evidence_failure"],
                        "judge": {
                            "major_issues": ["Wrong direction"],
                            "major_strengths": ["Correct method"],
                            "disagreement_flags": ["Specialists disagree"],
                        },
                        "specialists": {
                            "stats_specialist": {
                                "summary": "Statistical issue.",
                                "failure_codes": ["numeric_mismatch"],
                            },
                            "biology_specialist": {
                                "summary": "Biology issue.",
                                "failure_codes": ["biological_misinterpretation"],
                            },
                            "evidence_checker": {
                                "summary": "Evidence issue.",
                                "failure_codes": ["evidence_gap"],
                                "evidence_gaps": ["Truncated output"],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            results = EvaluationResults(root)
            loaded = results.load_run(
                "bix-ready",
                benchmark="bixbench",
                include_usage=False,
                include_bixbench_metadata=True,
                include_judge_details=True,
            )

            q1 = loaded.loc[loaded["item_id"].eq("q1")].iloc[0]
            self.assertEqual(q1["source_case_uuid"], "case-uuid-1")
            self.assertEqual(q1["species"], "Homo sapiens")
            self.assertEqual(q1["task_categories"], "RNA-seq, Differential Expression Analysis")
            self.assertEqual(q1["judge_disagreement_span"], 2.0)
            self.assertEqual(q1["judge_stats_failure_codes"], "numeric_mismatch")
            self.assertEqual(q1["judge_evidence_gaps"], "Truncated output")
            self.assertTrue(q1["judge_review_required"])

            exploded = EvaluationResults.explode_bixbench_categories(loaded)
            q2_categories = exploded.loc[exploded["item_id"].eq("q2"), "task_category"].tolist()
            self.assertEqual(q2_categories, ["Single-Cell Analysis", "Transcriptomics"])

    def test_load_run_can_enrich_repobench_judge_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_root = self._write_repobench_run(
                root,
                "repo-ready",
                [{"task_id": "task-1", "committee_level_mean_final": 3.5, "status": "completed"}],
            )
            judge_path = run_root / "episodes" / "task-1" / "judge.json"
            judge_path.parent.mkdir(parents=True)
            judge_path.write_text(
                json.dumps(
                    {
                        "task_id": "task-1",
                        "specialists": {
                            "biomedical_content_specialist": {"category": "accept", "summary": "Sound."},
                            "evidence_support_specialist": {
                                "category": "mostly_supported",
                                "failure_codes": ["minor_gap"],
                            },
                            "provenance_quality_specialist": {"category": "accept"},
                            "template_compliance_specialist": {"category": "accept"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = EvaluationResults(root).load_run(
                "repo-ready",
                benchmark="repobench",
                include_usage=False,
                include_judge_details=True,
            )

            row = loaded.iloc[0]
            self.assertEqual(row["biomedical_content_category"], "accept")
            self.assertEqual(row["evidence_support_category"], "mostly_supported")
            self.assertEqual(row["evidence_support_failure_codes"], "minor_gap")

    def test_compare_runs_creates_per_item_dataset_and_basic_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_bixbench_run(root, "left", self._bix_rows(4.0, 1.0))
            self._write_bixbench_run(root, "right", self._bix_rows(2.0, 4.0))

            comparison = EvaluationResults(root).compare_runs("left", "right", benchmark="bixbench")

            self.assertEqual(comparison.paired["item_id"].tolist(), ["q1", "q2", "q3"])
            self.assertEqual(comparison.paired.loc[comparison.paired["item_id"].eq("q1"), "delta_score_0_4"].iloc[0], 2.0)
            self.assertEqual(comparison.paired.loc[comparison.paired["item_id"].eq("q2"), "delta_score_0_4"].iloc[0], -3.0)
            self.assertEqual(comparison.paired_statistics["scored_pairs"].iloc[0], 2)
            self.assertEqual(comparison.paired_statistics["left_higher"].iloc[0], 1)
            self.assertEqual(comparison.paired_statistics["right_higher"].iloc[0], 1)
            self.assertEqual(comparison.paired_statistics["exact_ties"].iloc[0], 0)

    def test_merge_runs_keeps_provenance_and_supports_benchmark_qualified_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_bixbench_run(root, "bix-a", self._bix_rows(4.0, 1.0))
            self._write_repobench_run(
                root,
                "repo-a",
                [
                    {
                        "task_id": "task-1",
                        "status": "completed",
                        "committee_category": "mostly_supported",
                        "committee_level_mean_final": 3.0,
                    }
                ],
            )

            results = EvaluationResults(root)
            merged = results.merge_runs([RunRef("bixbench", "bix-a"), RunRef("repobench", "repo-a")])

            self.assertEqual(len(merged), 4)
            self.assertEqual(set(merged["source_run"]), {"bixbench:bix-a", "repobench:repo-a"})
            repo_row = merged.loc[merged["benchmark"].eq("repobench")].iloc[0]
            self.assertEqual(repo_row["item_id"], "task-1")
            self.assertEqual(repo_row["normalized_score"], 0.75)

    def test_score_summary_can_exclude_zero_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_bixbench_run(root, "scores", self._bix_rows(4.0, 0.0))

            loaded = EvaluationResults(root).load_run("scores", benchmark="bixbench", include_usage=False)
            summary = EvaluationResults.score_summary(loaded, positive_only=True)

            self.assertEqual(summary["items"].iloc[0], 1)
            self.assertEqual(summary["mean_normalized_score"].iloc[0], 1.0)


if __name__ == "__main__":
    unittest.main()
