from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gleskit_mcp.eval.bixbench_judge import (
    BixBenchJudgeConfig,
    JudgeStructuredOutputError,
    SpecialistAssessment,
    _coerce_structured_result,
    _compute_bixbench_committee_result,
    _env_value,
    _is_retryable_judge_error,
    _invoke_structured,
    _invoke_structured_once,
    judge_bixbench_run,
    recompute_bixbench_scores,
)
from gleskit_mcp.eval.bixbench_runner import _run_auto_bixbench_judge
from gleskit_mcp.eval.judge_bands import judge_mean_level_category


class BixBenchJudgeTests(unittest.TestCase):
    def test_env_value_unquotes_direct_environment_values(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": '"quoted-key"'}, clear=False):
            self.assertEqual(_env_value("OPENROUTER_API_KEY"), "quoted-key")

    def test_shared_category_midpoint_mapping_uses_ordinal_ladder(self) -> None:
        self.assertEqual((judge_mean_level_category(0.49) or {}).get("key"), "unsupported")
        self.assertEqual((judge_mean_level_category(0.50) or {}).get("key"), "weak")
        self.assertEqual((judge_mean_level_category(1.49) or {}).get("key"), "weak")
        self.assertEqual((judge_mean_level_category(1.50) or {}).get("key"), "partial")
        self.assertEqual((judge_mean_level_category(2.50) or {}).get("key"), "mostly_supported")
        self.assertEqual((judge_mean_level_category(3.50) or {}).get("key"), "accept")

    def test_resolved_model_map_strips_provider_prefixes_for_openai(self) -> None:
        config = BixBenchJudgeConfig(
            run_root=Path("/tmp/demo-run"),
            provider="openai",
            model="openai/gpt-5.4-mini",
            biology_model="customprovider/gpt-4.1-mini",
        )

        self.assertEqual(config.model_map["stats_specialist"], "openai/gpt-5.4-mini")
        self.assertEqual(config.resolved_model_map["stats_specialist"], "gpt-5.4-mini")
        self.assertEqual(config.resolved_model_map["biology_specialist"], "gpt-4.1-mini")

    def test_resolved_model_map_keeps_openrouter_slugs(self) -> None:
        config = BixBenchJudgeConfig(
            run_root=Path("/tmp/demo-run"),
            provider="openrouter",
            model="nvidia/nemotron-3-ultra-550b-a55b:free",
            biology_model="qwen/qwen3-next-80b-a3b-instruct:free",
        )

        self.assertEqual(
            config.resolved_model_map["stats_specialist"],
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        )
        self.assertEqual(
            config.resolved_model_map["biology_specialist"],
            "qwen/qwen3-next-80b-a3b-instruct:free",
        )

    def test_coerce_structured_result_normalizes_none_lists(self) -> None:
        payload = _coerce_structured_result(
            SpecialistAssessment,
            {
                "category": "partial",
                "summary": "Looks broadly reasonable.",
                "key_observations": None,
                "satisfied_criteria": None,
                "failed_criteria": None,
                "failure_codes": None,
            },
        )
        self.assertEqual(payload["key_observations"], [])
        self.assertEqual(payload["satisfied_criteria"], [])
        self.assertEqual(payload["failed_criteria"], [])
        self.assertEqual(payload["failure_codes"], [])

    def test_coerce_structured_result_flattens_nested_text_list_items(self) -> None:
        payload = _coerce_structured_result(
            SpecialistAssessment,
            {
                "category": "partial",
                "summary": "Some support is present.",
                "key_observations": {"item": {"item": "Visible tool evidence supports part of the answer.</item>"}},
                "satisfied_criteria": [],
                "failed_criteria": [],
                "failure_codes": [],
            },
        )
        self.assertEqual(payload["key_observations"], ["Visible tool evidence supports part of the answer."])

    def test_invoke_structured_retries_on_empty_structured_output(self) -> None:
        async def _run() -> dict:
            with patch(
                "gleskit_mcp.eval.bixbench_judge._invoke_structured_once",
                side_effect=[
                    JudgeStructuredOutputError("empty"),
                    {
                        "category": "mostly_supported",
                        "summary": "Recovered",
                        "key_observations": [],
                        "satisfied_criteria": [],
                        "failed_criteria": [],
                        "failure_codes": [],
                    },
                ],
            ):
                with patch("gleskit_mcp.eval.bixbench_judge.asyncio.sleep", new=AsyncMock(return_value=None)):
                    return await _invoke_structured(
                        provider="openrouter",
                        base_url="https://openrouter.ai/api/v1",
                        model_name="nvidia/nemotron-3-ultra-550b-a55b:free",
                        schema=SpecialistAssessment,
                        prompt="test",
                        api_key="key",
                    )

        payload = __import__("asyncio").run(_run())
        self.assertEqual(payload["summary"], "Recovered")
        self.assertEqual(payload["category"], "mostly_supported")

    def test_retryable_judge_error_recognizes_openrouter_valueerror_status(self) -> None:
        error = ValueError({"message": "The operation was aborted", "code": 504})

        self.assertTrue(_is_retryable_judge_error(error))

    def test_retryable_judge_error_limits_generic_provider_400_to_openrouter(self) -> None:
        error = ValueError({"message": "Provider returned error", "code": 400})

        self.assertTrue(_is_retryable_judge_error(error, provider="openrouter"))
        self.assertFalse(_is_retryable_judge_error(error, provider="openai"))

    def test_openrouter_structured_invocation_uses_forced_function_calling(self) -> None:
        captured: dict[str, object] = {}

        class FakeRunnable:
            async def ainvoke(self, prompt: str) -> dict[str, object]:
                captured["prompt"] = prompt
                return {
                    "parsed": {
                        "category": "accept",
                        "summary": "The trace supports the answer.",
                    },
                    "raw": None,
                    "parsing_error": None,
                }

        class FakeChatOpenAI:
            def __init__(self, **kwargs: object) -> None:
                captured["model_kwargs"] = kwargs

            def with_structured_output(self, schema: object, **kwargs: object) -> FakeRunnable:
                captured["schema"] = schema
                captured["structured_kwargs"] = kwargs
                return FakeRunnable()

        async def _run() -> dict:
            with patch("gleskit_mcp.eval.bixbench_judge.ChatOpenAI", FakeChatOpenAI):
                return await _invoke_structured_once(
                    provider="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    model_name="minimax/minimax-m3",
                    schema=SpecialistAssessment,
                    prompt="Assess this episode.",
                    api_key="test-key",
                )

        payload = __import__("asyncio").run(_run())
        self.assertEqual(payload["category"], "accept")
        self.assertEqual(captured["structured_kwargs"], {"method": "function_calling", "include_raw": True})
        self.assertEqual(
            captured["model_kwargs"].get("extra_body"),
            {"reasoning": {"effort": "none", "exclude": True}},
        )
        self.assertIn("Call the supplied `SpecialistAssessment` function now", str(captured["prompt"]))

    def test_openrouter_structured_invocation_forwards_completion_bound(self) -> None:
        captured: dict[str, object] = {}

        class FakeRunnable:
            async def ainvoke(self, _prompt: str) -> dict[str, object]:
                return {
                    "parsed": {"category": "accept", "summary": "Bounded completion."},
                    "raw": None,
                    "parsing_error": None,
                }

        class FakeChatOpenAI:
            def __init__(self, **kwargs: object) -> None:
                captured["model_kwargs"] = kwargs

            def with_structured_output(self, _schema: object, **_kwargs: object) -> FakeRunnable:
                return FakeRunnable()

        async def _run() -> dict:
            with patch("gleskit_mcp.eval.bixbench_judge.ChatOpenAI", FakeChatOpenAI):
                return await _invoke_structured_once(
                    provider="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    model_name="minimax/minimax-m3",
                    schema=SpecialistAssessment,
                    prompt="Assess this episode.",
                    api_key="test-key",
                    max_tokens=123,
                )

        __import__("asyncio").run(_run())
        self.assertEqual(captured["model_kwargs"].get("max_tokens"), 123)

    def test_bixbench_committee_result_preserves_weighted_mean_and_flags_weak_numeric_stats(self) -> None:
        result = _compute_bixbench_committee_result(
            numeric=1,
            weights={"stats": 0.55, "biology": 0.20, "evidence": 0.25},
            stats={"category": "weak"},
            biology={"category": "accept"},
            evidence={"category": "accept", "failure_codes": [], "unsupported_claims": []},
            benchmark_correct=False,
        )

        self.assertEqual(result["committee_level_mean_raw"], 2.35)
        self.assertEqual(result["committee_level_mean_final"], 2.35)
        self.assertEqual(result["committee_category"], "partial")
        self.assertFalse(result["cap_applied"])
        self.assertEqual(result["quality_flags"], ["numeric_stats_weak:weak"])
        self.assertTrue(result["review_required"])
        self.assertIn("quality_flag", result["review_reasons"])

    def test_bixbench_committee_result_tracks_evidence_failures_without_score_capping(self) -> None:
        result = _compute_bixbench_committee_result(
            numeric=0,
            weights={"stats": 0.25, "biology": 0.40, "evidence": 0.35},
            stats={"category": "accept"},
            biology={"category": "accept"},
            evidence={"category": "unsupported", "failure_codes": ["unsupported_claim"], "unsupported_claims": ["claim"]},
            benchmark_correct=True,
        )

        self.assertEqual(result["committee_level_mean_raw"], 2.6)
        self.assertEqual(result["committee_level_mean_final"], 2.6)
        self.assertEqual(result["committee_category"], "mostly_supported")
        self.assertEqual(result["specialist_disagreement_span"], 4)
        self.assertTrue(result["review_required"])
        self.assertIn("quality_flag", result["review_reasons"])
        self.assertIn("specialist_disagreement_span", result["review_reasons"])
        self.assertIn("material_evidence_failure", result["review_reasons"])
        self.assertNotIn("benchmark_divergence_audit", result["review_reasons"])

    def _write_episode_bundle(self, run_root: Path) -> None:
        episode_root = run_root / "episodes" / "bix-16-q2"
        episode_root.mkdir(parents=True, exist_ok=True)
        row = {
            "episode_id": "bix-16-q2",
            "question_id": "bix-16-q2",
            "short_id": "bix-16",
            "scope": 1,
            "numeric": 1,
            "eval_mode": "range_verifier",
            "capsule_uuid": "capsule-1",
            "run_id": "run-1",
            "status": "completed",
            "stop_reason": "completed",
            "turns": 2,
            "tool_calls": 1,
            "duration_seconds": 1.0,
            "total_duration_seconds": 1.0,
            "retry_count": 0,
            "question": "What is the skewness characteristic of the gene expression distribution?",
            "ideal": "Right-skewed with a long tail",
            "score": 0.0,
            "correct": False,
            "match_reason": "text_mismatch",
            "manual_review_required": False,
            "answer": "The distribution is right-skewed.",
            "confidence": None,
            "abstain": False,
            "input": {
                "prompt": {"text": "Base prompt"},
                "question": {"question_id": "bix-16-q2", "scope": 1, "numeric": 1},
            },
            "transcript": [{"role": "user", "text": "Question"}],
            "tool_trace": [{"tool": "resolve_gene", "status": "ok", "input": {}, "output": {}}],
            "final_answer": {
                "raw_text": "{\"answer\": \"The distribution is right-skewed.\", \"evidence\": [\"tool\"], \"abstain\": false}",
                "answer": "The distribution is right-skewed.",
                "evidence": ["tool"],
                "confidence": None,
                "abstain": False,
            },
            "score_details": {"score": 0.0, "correct": False, "match_reason": "text_mismatch"},
        }
        (episode_root / "episode.json").write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")
        (episode_root / "input.json").write_text(json.dumps(row["input"], indent=2, sort_keys=True), encoding="utf-8")
        (episode_root / "transcript.json").write_text(json.dumps(row["transcript"], indent=2, sort_keys=True), encoding="utf-8")
        (episode_root / "tool_calls.json").write_text(json.dumps(row["tool_trace"], indent=2, sort_keys=True), encoding="utf-8")
        (episode_root / "final_answer.json").write_text(json.dumps(row["final_answer"], indent=2, sort_keys=True), encoding="utf-8")
        (episode_root / "score.json").write_text(json.dumps(row["score_details"], indent=2, sort_keys=True), encoding="utf-8")

        (run_root / "planned_matrix.json").write_text(
            json.dumps(
                [
                    {
                        "question_id": "bix-16-q2",
                        "scope": 1,
                        "numeric": 1,
                        "eval_mode": "range_verifier",
                        "short_id": "bix-16",
                        "ideal": "Right-skewed with a long tail",
                        "question": "What is the skewness characteristic of the gene expression distribution?",
                    }
                ],
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (run_root / "run_manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "selection": {"scope": "1", "question_ids": [], "limit": 0, "resume": False},
                    "dataset": {"path": "/tmp/BixBench.gleskit_scope.jsonl"},
                    "planned_episode_count": 1,
                    "created_at": "2026-06-26T00:00:00+00:00",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def test_recompute_bixbench_scores_preserves_exact_weighted_values_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "run"
            self._write_episode_bundle(run_root)
            episode_root = run_root / "episodes" / "bix-16-q2"
            (episode_root / "judge.json").write_text(
                json.dumps(
                    {
                        "judge_schema_version": 3,
                        "status": "scored",
                        "numeric": 1,
                        "weight_hint": {"stats": 0.55, "biology": 0.20, "evidence": 0.25},
                        "benchmark": {"correct": False},
                        "judge": {"review_reasons": ["cap_applied"]},
                        "specialists": {
                            "stats_specialist": {"category": "mostly_supported"},
                            "biology_specialist": {"category": "accept"},
                            "evidence_checker": {"category": "partial", "failure_codes": [], "unsupported_claims": []},
                        },
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            result = recompute_bixbench_scores(run_root)
            migrated = json.loads((episode_root / "judge.json").read_text(encoding="utf-8"))

            self.assertEqual(result["updated_count"], 1)
            self.assertEqual(migrated["judge_schema_version"], 4)
            self.assertEqual(migrated["committee_level_mean_raw"], 2.95)
            self.assertEqual(migrated["committee_level_mean_final"], 2.95)
            self.assertEqual(migrated["committee_level_mean_normalized"], 0.7375)
            self.assertEqual(migrated["judge_score"], 0.7375)
            self.assertEqual(migrated["score_policy"], "continuous_weighted_mean_exact_v1")
            self.assertFalse(migrated["score_recomputation"]["provider_called"])
            self.assertTrue((run_root / "summary" / "questions.csv").exists())

    def test_judge_run_writes_rubric_first_artifacts_and_regenerates_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "run"
            self._write_episode_bundle(run_root)
            (run_root / "summary").mkdir(parents=True, exist_ok=True)
            (run_root / "portal").mkdir(parents=True, exist_ok=True)
            (run_root / "summary" / "report.html").write_text("SOLVE-SENTINEL", encoding="utf-8")
            (run_root / "portal" / "index.html").write_text("PORTAL-SENTINEL", encoding="utf-8")

            structured_assessments = [
                {
                    "category": "mostly_supported",
                    "summary": "The numeric answer is close to the benchmark meaning.",
                    "key_observations": ["The answer is quantitative and directionally correct."],
                    "satisfied_criteria": ["answers the stated question"],
                    "failed_criteria": [],
                    "failure_codes": [],
                },
                {
                    "category": "accept",
                    "summary": "The biology is fully aligned.",
                    "key_observations": ["Captures the biological direction."],
                    "satisfied_criteria": ["keeps the biological interpretation intact"],
                    "failed_criteria": [],
                    "failure_codes": [],
                },
                {
                    "category": "partial",
                    "summary": "The answer is only partly supported by visible evidence.",
                    "key_observations": ["There is some grounding, but not enough for a stronger verdict."],
                    "satisfied_criteria": ["has a visible tool reference"],
                    "failed_criteria": ["does not fully trace the answer wording"],
                    "failure_codes": ["evidence_gap"],
                    "matching_evidence": ["tool"],
                    "unsupported_claims": [],
                    "evidence_gaps": ["The visible trace does not fully support the phrasing."],
                },
                {
                    "short_review": "The committee found the answer directionally strong but evidence-limited.",
                    "rationale": "The answer is close to the benchmark, but the visible trace only partly supports the exact wording.",
                    "major_strengths": ["Uses the right direction"],
                    "major_issues": ["Evidence trace is incomplete"],
                    "disagreement_flags": ["evidence_limits_strength"],
                    "review_required": False,
                    "review_reasons": [],
                },
            ]

            structured_outputs = [
                ConnectionError("Connection error."),
                structured_assessments[0],
                structured_assessments[1],
                structured_assessments[2],
                structured_assessments[3],
            ]

            with patch("gleskit_mcp.eval.bixbench_judge._invoke_structured_once", side_effect=structured_outputs):
                with patch("gleskit_mcp.eval.bixbench_judge.asyncio.sleep", new=AsyncMock(return_value=None)):
                    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
                        judge_bixbench_run(
                            BixBenchJudgeConfig(
                                run_root=run_root,
                                scope="all",
                                question_ids=(),
                                limit=0,
                                resume=False,
                                timeout_seconds=15,
                            )
                        )

            judge_path = run_root / "episodes" / "bix-16-q2" / "judge.json"
            self.assertTrue(judge_path.exists())
            judge_payload = json.loads(judge_path.read_text(encoding="utf-8"))
            self.assertEqual(judge_payload["judge_schema_version"], 4)
            self.assertEqual(judge_payload["status"], "scored")
            self.assertEqual(judge_payload["committee_category"], "mostly_supported")
            self.assertEqual(judge_payload["committee_level_mean_raw"], 2.95)
            self.assertEqual(judge_payload["committee_level_mean_final"], 2.95)
            self.assertEqual(judge_payload["committee_level_mean_normalized"], 0.7375)
            self.assertEqual(judge_payload["judge_score"], 0.7375)
            self.assertFalse(judge_payload["cap_applied"])
            self.assertEqual(judge_payload["cap_reasons"], [])
            self.assertEqual(judge_payload["quality_flags"], ["evidence_below_weighted_mean:partial"])
            self.assertTrue(judge_payload["review_required"])
            self.assertEqual(judge_payload["review_reasons"], ["quality_flag", "benchmark_divergence_audit"])
            self.assertEqual(judge_payload["specialist_disagreement_span"], 2)
            self.assertEqual(judge_payload["specialists"]["stats_specialist"]["category"], "mostly_supported")
            self.assertEqual(judge_payload["specialists"]["biology_specialist"]["category"], "accept")
            self.assertEqual(judge_payload["specialists"]["evidence_checker"]["category"], "partial")
            self.assertNotIn("confidence", judge_payload["specialists"]["stats_specialist"])
            self.assertNotIn("hallucination_risk", judge_payload["specialists"]["evidence_checker"])
            self.assertEqual(judge_payload["model_map"]["stats_specialist"], "openai/gpt-5.4-mini")
            self.assertEqual(judge_payload["resolved_model_map"]["stats_specialist"], "gpt-5.4-mini")
            self.assertEqual(
                judge_payload["weight_hint"],
                {"stats": 0.55, "biology": 0.20, "evidence": 0.25},
            )
            self.assertIn("tool_provenance", judge_payload)
            self.assertGreaterEqual(len(judge_payload["tool_provenance"]), 1)

            run_report_html = (run_root / "summary" / "report.html").read_text(encoding="utf-8")
            run_portal_html = (run_root / "portal" / "index.html").read_text(encoding="utf-8")
            self.assertIn("BixBench Run Report", run_report_html)
            self.assertIn("Committee category", run_report_html)
            self.assertIn("Committee level mean (0-4)", run_report_html)
            self.assertNotIn("Committee score", run_report_html)
            self.assertNotIn("Judge confidence", run_report_html)
            self.assertIn("BixBench run-1", run_portal_html)
            self.assertIn("Committee category", run_portal_html)
            self.assertIn("Committee level mean (0-4)", run_portal_html)
            self.assertNotIn("Committee band", run_portal_html)

            judge_summary_root = run_root / "summary" / "judge"
            self.assertTrue((judge_summary_root / "report.html").exists())
            self.assertTrue((judge_summary_root / "report.md").exists())
            self.assertTrue((judge_summary_root / "results.json").exists())
            self.assertTrue((judge_summary_root / "judge_manifest.json").exists())
            self.assertTrue((judge_summary_root / "progress.json").exists())
            self.assertTrue((judge_summary_root / "judge_metrics.csv").exists())
            self.assertTrue((judge_summary_root / "judge_agreement.csv").exists())

            progress_payload = json.loads((judge_summary_root / "progress.json").read_text(encoding="utf-8"))
            question_progress = progress_payload["questions"]["bix-16-q2"]
            self.assertEqual(progress_payload["status"], "completed")
            self.assertEqual(question_progress["status"], "completed")
            self.assertEqual(question_progress["stages"]["stats_specialist"]["retries"], 1)
            self.assertEqual(question_progress["stages"]["arbiter"]["status"], "completed")

            report_html = (judge_summary_root / "report.html").read_text(encoding="utf-8")
            self.assertIn("BixBench Judge Report", report_html)
            self.assertIn("Committee category", report_html)
            self.assertIn("Committee level mean (0-4)", report_html)
            self.assertIn("Judge agreement", report_html)
            self.assertIn("partial", report_html)
            self.assertIn("Statistics specialist", report_html)
            self.assertIn("Major strengths", report_html)
            self.assertIn("Weight hint", report_html)
            self.assertIn("Arbiter synthesis", report_html)
            self.assertIn("Tool provenance", report_html)
            self.assertNotIn("Judge confidence", report_html)
            self.assertNotIn("Arbiter raw score", report_html)
            self.assertNotIn("Judge support level", report_html)

            report_md = (judge_summary_root / "report.md").read_text(encoding="utf-8")
            self.assertIn("Judge Details", report_md)
            self.assertIn("Statistics specialist", report_md)
            self.assertIn("Evidence checker", report_md)
            self.assertIn("Arbiter synthesis", report_md)
            self.assertIn("Tool provenance", report_md)
            self.assertNotIn("Judge confidence", report_md)

            portal_html = (run_root / "portal" / "judge" / "questions" / "bix-16-q2.html").read_text(encoding="utf-8")
            self.assertIn("Committee category", portal_html)
            self.assertIn("Judge rationale", portal_html)
            self.assertIn("Arbiter synthesis", portal_html)
            self.assertIn("Key observations", portal_html)
            self.assertIn("Tool provenance", portal_html)
            self.assertNotIn("Arbiter raw score", portal_html)

    def test_auto_judge_failure_is_best_effort_and_preserves_solve_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "run"
            self._write_episode_bundle(run_root)

            with patch("gleskit_mcp.eval.bixbench_judge.judge_bixbench_run", side_effect=RuntimeError("boom")):
                _run_auto_bixbench_judge(run_root)

            error_path = run_root / "summary" / "judge" / "auto_judge_error.json"
            self.assertTrue(error_path.exists())
            error_payload = json.loads(error_path.read_text(encoding="utf-8"))
            self.assertEqual(error_payload["status"], "auto_judge_failed")
            self.assertIn("boom", error_payload["error"])

    def test_missing_openai_api_key_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "run"
            self._write_episode_bundle(run_root)

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY is required"):
                    judge_bixbench_run(
                        BixBenchJudgeConfig(
                            run_root=run_root,
                            scope="all",
                            question_ids=(),
                            limit=0,
                            resume=False,
                            timeout_seconds=15,
                        )
                    )

    def test_missing_openrouter_api_key_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "run"
            self._write_episode_bundle(run_root)

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "OPENROUTER_API_KEY is required"):
                    judge_bixbench_run(
                        BixBenchJudgeConfig(
                            run_root=run_root,
                            scope="all",
                            question_ids=(),
                            limit=0,
                            resume=False,
                            timeout_seconds=15,
                            provider="openrouter",
                            model="nvidia/nemotron-3-ultra-550b-a55b:free",
                        )
                    )


if __name__ == "__main__":
    unittest.main()
