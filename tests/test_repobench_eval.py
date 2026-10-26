from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gleskit_mcp.config import Settings
from gleskit_mcp.eval.config import load_report_type_specs
from gleskit_mcp.eval.repobench import (
    agent_visible_task_payload,
    build_repobench_prompt,
    build_repobench_rows_from_specs,
    select_repobench_rows,
    stage_repobench_input_artifacts,
)
from gleskit_mcp.eval.repobench_runner import (
    _allow_eval_read_paths,
    _assert_isolated_eval_roots,
    _summary_row,
    render_judge_summary_html,
    RepoBenchRunConfig,
    _next_watchdog_state,
    build_input_payload,
    build_overall_summary,
    merge_episode_attempt_history,
    normalize_continue_mode,
    parse_args,
    should_retry_attempt,
    task_is_completed,
    task_needs_retry,
)


class RepoBenchEvalTests(unittest.TestCase):
    def test_watchdog_does_not_treat_active_mcp_work_as_idle(self) -> None:
        no_progress, active_tool, pending_provider = _next_watchdog_state(
            activity_kind="mcp_tool",
            current_signature=(5, 4, 10),
            last_signature=(5, 4, 10),
            no_progress=3,
        )

        self.assertEqual(no_progress, 0)
        self.assertTrue(active_tool)
        self.assertFalse(pending_provider)

    def test_judge_summary_renders_quality_flags(self) -> None:
        record = {
            "judge": {
                "committee_category": "mostly_supported",
                "committee_level_mean_final": 3.0,
                "quality_flags": ["evidence_below_weighted_mean:mostly_supported"],
                "specialists": {
                    "biomedical_content_specialist": {"category": "mostly_supported", "summary": "Content is useful."},
                    "evidence_support_specialist": {"category": "mostly_supported", "summary": "Evidence is useful."},
                    "template_compliance_specialist": {"category": "mostly_supported", "summary": "Template is useful."},
                    "provenance_quality_specialist": {"category": "mostly_supported", "summary": "Sources are useful."},
                },
                "judge": {"short_review": "Useful report.", "rationale": "All roles completed."},
            }
        }

        html = render_judge_summary_html(record)

        self.assertIn("evidence_below_weighted_mean:mostly_supported", html)
        self.assertIn("Judge Committee", html)

    def test_normalize_continue_mode_supports_resume_alias(self) -> None:
        self.assertEqual(normalize_continue_mode("fresh", resume_flag=True), "resume")
        self.assertEqual(normalize_continue_mode("retry_failed"), "retry_failed")
        with self.assertRaises(ValueError):
            normalize_continue_mode("append", resume_flag=True)

    def test_parse_args_exposes_continuation_controls(self) -> None:
        args = parse_args([])
        self.assertEqual(args.random_count, 0)
        self.assertEqual(args.continue_mode, "fresh")

        custom = parse_args(["--random-count", "3", "--random-seed", "11", "--continue-mode", "append"])
        self.assertEqual(custom.random_count, 3)
        self.assertEqual(custom.random_seed, 11)
        self.assertEqual(custom.continue_mode, "append")

    def test_assert_isolated_eval_roots_accepts_run_local_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_root = root / "run" / "workspace"
            runtime_root = root / "run" / "runtime"
            settings = Settings(
                project_root=root,
                defaults_config_path=root / "config" / "defaults.json",
                local_config_path=root / "config" / "local.json",
                workspace_default_root=workspace_root,
                runtime_root=runtime_root,
                runtime_manifest_path=runtime_root / "workspace.json",
                reference_root=root / "references",
                reference_manifest_path=root / "references" / "manifest.json",
                r_lib_root=root / "r_lib",
                rscript_bin="Rscript",
            )

            _assert_isolated_eval_roots(
                settings=settings,
                workspace_root=workspace_root,
                runtime_root=runtime_root,
            )

    def test_assert_isolated_eval_roots_rejects_live_workspace_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_root = root / "run" / "workspace"
            runtime_root = root / "run" / "runtime"
            settings = Settings(
                project_root=root,
                defaults_config_path=root / "config" / "defaults.json",
                local_config_path=root / "config" / "local.json",
                workspace_default_root=root / "var" / "gleskit_mcp",
                runtime_root=root / "var" / "gleskit_runtime",
                runtime_manifest_path=(root / "var" / "gleskit_runtime" / "workspace.json"),
                reference_root=root / "references",
                reference_manifest_path=root / "references" / "manifest.json",
                r_lib_root=root / "r_lib",
                rscript_bin="Rscript",
            )

            with self.assertRaises(RuntimeError):
                _assert_isolated_eval_roots(
                    settings=settings,
                    workspace_root=workspace_root,
                    runtime_root=runtime_root,
                )

    def test_build_repobench_rows_from_specs_bootstraps_real_cases(self) -> None:
        specs = load_report_type_specs()
        rows = build_repobench_rows_from_specs(specs)

        self.assertEqual(len(rows), 15)
        self.assertEqual(len({row["task_id"] for row in rows}), 15)
        self.assertEqual(len({row["short_id"] for row in rows}), 15)
        self.assertEqual(len({row["question_id"] for row in rows}), 15)
        self.assertEqual(rows[0]["short_id"], "repo-1")
        self.assertEqual(rows[0]["question_id"], "repo-1-q1")
        self.assertEqual({row["report_type"] for row in rows}, {"biomarker", "mechanism", "disease_context"})
        first = rows[0]
        self.assertIn("task_prompt", first)
        self.assertIn("kickoff_context", first)
        self.assertIn("expected_nuggets", first)
        self.assertIn("grounding_requirements", first)
        self.assertIn("section_grounding_expectations", first)
        self.assertIn("input_artifacts", first)
        self.assertTrue(first["input_artifacts"])
        self.assertTrue(first["grounding_requirements"])
        self.assertTrue(first["section_grounding_expectations"])

    def test_select_repobench_rows_preserves_requested_task_order(self) -> None:
        rows = [
            {"task_id": "b", "scope": 1, "report_type": "mechanism"},
            {"task_id": "a", "scope": 1, "report_type": "biomarker"},
            {"task_id": "c", "scope": 2, "report_type": "biomarker"},
        ]

        selected = select_repobench_rows(rows, task_ids=["a", "b"], scope="1")

        self.assertEqual([row["task_id"] for row in selected], ["a", "b"])

    def test_select_repobench_rows_accepts_bixbench_style_question_id(self) -> None:
        rows = [
            {"task_id": "descriptive-a", "short_id": "repo-1", "question_id": "repo-1-q1", "scope": 1, "report_type": "biomarker"},
            {"task_id": "descriptive-b", "short_id": "repo-2", "question_id": "repo-2-q1", "scope": 1, "report_type": "mechanism"},
        ]

        selected = select_repobench_rows(rows, task_ids=["repo-2-q1", "repo-1"], scope="1")

        self.assertEqual([row["task_id"] for row in selected], ["descriptive-b", "descriptive-a"])

    def test_stage_repobench_input_artifacts_materializes_inline_files(self) -> None:
        row = {
            "task_id": "demo-task",
            "input_artifacts": [
                {
                    "artifact_id": "kickoff_context",
                    "kind": "inline_json",
                    "filename": "kickoff_context.json",
                    "payload": {"genes": ["STAT1", "MX1"]},
                },
                {
                    "artifact_id": "gene_panel",
                    "kind": "inline_text",
                    "filename": "genes.txt",
                    "payload": "STAT1\nMX1\n",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            staged = stage_repobench_input_artifacts(project_root=root, run_root=root / "run", row=row)
            self.assertEqual(len(staged["artifacts"]), 2)
            kickoff_path = Path(staged["artifacts"][0]["staged_path"])
            genes_path = Path(staged["artifacts"][1]["staged_path"])
            self.assertTrue(kickoff_path.exists())
            self.assertTrue(genes_path.exists())
            self.assertEqual(json.loads(kickoff_path.read_text(encoding="utf-8")), {"genes": ["STAT1", "MX1"]})
            self.assertEqual(genes_path.read_text(encoding="utf-8"), "STAT1\nMX1\n")

    def test_stage_repobench_input_artifacts_replaces_a_read_only_prior_stage(self) -> None:
        row = {
            "task_id": "demo-task",
            "input_artifacts": [
                {
                    "artifact_id": "gene_panel",
                    "kind": "inline_text",
                    "filename": "genes.txt",
                    "payload": "STAT1\nMX1\n",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = stage_repobench_input_artifacts(project_root=root, run_root=root / "run", row=row)
            second = stage_repobench_input_artifacts(project_root=root, run_root=root / "run", row=row)

            self.assertEqual(first["stage_root"], second["stage_root"])
            staged_path = Path(second["artifacts"][0]["staged_path"])
            self.assertEqual(staged_path.read_text(encoding="utf-8"), "STAT1\nMX1\n")

    def test_build_overall_summary_scores_only_completed_saved_reports(self) -> None:
        records = [
            {
                "task_id": "saved-1",
                "report_saved": True,
                "status": "completed",
                "turn_count": 4,
                "tool_call_count": 3,
                "duration_seconds": 90.0,
                "deterministic_metrics": {
                    "reader_facing_provenance_score": 0.8,
                    "mean_nugget_text_overlap": 0.7,
                },
                "judge": {"committee_level_mean_final": 3.6},
            },
            {
                "task_id": "failed-1",
                "report_saved": False,
                "status": "failed",
                "turn_count": 9,
                "tool_call_count": 7,
                "duration_seconds": 140.0,
                "deterministic_metrics": {
                    "reader_facing_provenance_score": 0.1,
                    "mean_nugget_text_overlap": 0.2,
                },
                "judge": {"committee_level_mean_final": 0.4},
            },
        ]

        summary = build_overall_summary(records)

        self.assertEqual(summary["task_count"], 2)
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["incomplete_count"], 1)
        self.assertEqual(summary["committee_level_mean_count"], 1)
        self.assertEqual(summary["committee_level_mean_mean"], 3.6)
        self.assertEqual(summary["committee_score_count"], 1)
        self.assertEqual(summary["committee_score_mean"], 0.9)
        self.assertEqual(summary["reader_facing_provenance_score_mean"], 0.8)
        self.assertEqual(summary["mean_nugget_text_overlap_mean"], 0.7)

    def test_task_retry_logic_requires_saved_report(self) -> None:
        completed = {"report_saved": True}
        failed = {"report_saved": False}

        self.assertTrue(task_is_completed(completed))
        self.assertFalse(task_needs_retry(completed))
        self.assertFalse(task_is_completed(failed))
        self.assertTrue(task_needs_retry(failed))

    def test_should_retry_attempt_allows_retryable_zero_exit_runtime_failures(self) -> None:
        attempt_record = {
            "report_saved": False,
            "returncode": 0,
            "watchdog_triggered": False,
            "retryable": True,
        }
        self.assertTrue(should_retry_attempt(attempt_record, attempt_index=0, max_retries=1))

    def test_merge_episode_attempt_history_preserves_prior_attempts(self) -> None:
        existing = {"attempts": [{"attempt": 1, "duration_seconds": 11.0}]}
        current = {
            "attempts": [{"attempt": 2, "duration_seconds": 13.5}],
            "attempt_count": 1,
            "retry_count": 0,
            "total_duration_seconds": 13.5,
        }

        merged = merge_episode_attempt_history(existing, current)

        self.assertEqual(merged["attempt_count"], 2)
        self.assertEqual(merged["retry_count"], 1)
        self.assertEqual(merged["total_duration_seconds"], 24.5)

    def test_summary_row_exposes_committee_category_and_level_mean(self) -> None:
        row = _summary_row(
            {
                "task_id": "demo",
                "report_type": "mechanism",
                "scope": 1,
                "status": "completed",
                "report_saved": True,
                "judge": {
                    "committee_category": "mostly_supported",
                    "committee_level_mean_final": 3.0,
                    "review_required": True,
                    "judge": {"short_review": "Grounded but still caveated."},
                },
            }
        )

        self.assertEqual(row["committee_category"], "mostly_supported")
        self.assertEqual(row["committee_category_label"], "Mostly Supported")
        self.assertEqual(row["committee_level_mean_final"], 3.0)
        self.assertEqual(row["committee_level_mean_normalized"], 0.75)
        self.assertTrue(row["review_required"])
        self.assertEqual(row["judge_short_review"], "Grounded but still caveated.")

    def test_allow_eval_read_paths_grants_stage_root_to_repobench_agent(self) -> None:
        config = {
            "agent": {
                "repobench-eval": {
                    "permission": {
                        "read": {"*": "deny"},
                        "external_directory": {"*": "deny"},
                    }
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "opencode.json"
            stage_root = root / "staged_inputs" / "demo"
            stage_root.mkdir(parents=True)
            config_path.write_text(json.dumps(config), encoding="utf-8")

            _allow_eval_read_paths(config_path, [stage_root])

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            permission = updated["agent"]["repobench-eval"]["permission"]
            resolved = str(stage_root.resolve())
            self.assertEqual(permission["read"][resolved], "allow")
            self.assertEqual(permission["read"][f"{resolved}/**"], "allow")
            self.assertEqual(permission["external_directory"][resolved], "allow")
            self.assertEqual(permission["external_directory"][f"{resolved}/**"], "allow")

    def test_build_repobench_prompt_includes_grounding_contract(self) -> None:
        specs = load_report_type_specs()
        rows = build_repobench_rows_from_specs(specs)
        row = next(item for item in rows if item["task_id"] == "biomarker-inflammatory_serum_panel")
        spec = specs[row["report_type"]]
        template = Path("docs/opencode/prompts/eval/repobench-eval-prompt.md").read_text(encoding="utf-8")
        staged_inputs = {
            "stage_root": "/tmp/staged_inputs/biomarker-inflammatory_serum_panel",
            "artifacts": [
                {
                    "artifact_id": "gene_panel",
                    "staged_path": "/tmp/staged_inputs/biomarker-inflammatory_serum_panel/gene_panel.txt",
                    "description": "Gene panel extracted from kickoff context.",
                }
            ],
        }

        prompt = build_repobench_prompt(
            template_text=template,
            row=row,
            spec=spec,
            staged_inputs=staged_inputs,
            run_id="repobench_test",
            task_root=Path("/tmp/task_root"),
            session_id="session_test",
        )

        self.assertIn("Grounding requirements:", prompt)
        self.assertIn("Section grounding expectations:", prompt)
        self.assertIn("Marker Candidates", prompt)
        self.assertIn("visible `Sources:` line", prompt)
        self.assertIn("markdown link", prompt)
        self.assertIn("exact title returned by the tool output", prompt)
        self.assertIn("citation_audit", prompt)

    def test_agent_visible_task_payload_excludes_evaluator_rubric(self) -> None:
        specs = load_report_type_specs()
        rows = build_repobench_rows_from_specs(specs)
        row = next(item for item in rows if item["task_id"] == "biomarker-inflammatory_serum_panel")

        payload = agent_visible_task_payload(row)

        self.assertIn("task_prompt", payload)
        self.assertIn("grounding_requirements", payload)
        self.assertNotIn("expected_content", payload)
        self.assertNotIn("expected_nuggets", payload)
        self.assertNotIn("forbidden_overclaims", payload)

    def test_attempt_input_payload_excludes_evaluator_rubric(self) -> None:
        specs = load_report_type_specs()
        rows = build_repobench_rows_from_specs(specs)
        row = next(item for item in rows if item["task_id"] == "biomarker-inflammatory_serum_panel")

        payload = build_input_payload(
            row=row,
            run_id="repobench_test",
            episode_root=Path("/tmp/episode"),
            session_title="session title",
            prompt_text="Solve the reporting task.",
            opencode_command=["opencode", "run"],
            staged_inputs={"stage_root": "/tmp/staged_inputs", "artifacts": []},
            gleskit_session={"session_id": "session_test"},
            config=RepoBenchRunConfig(run_id="repobench_test"),
        )

        task = payload["task"]
        self.assertNotIn("expected_content", task)
        self.assertNotIn("expected_nuggets", task)
        self.assertNotIn("forbidden_overclaims", task)

    def test_build_repobench_prompt_excludes_evaluator_rubric(self) -> None:
        specs = load_report_type_specs()
        rows = build_repobench_rows_from_specs(specs)
        row = next(item for item in rows if item["task_id"] == "biomarker-inflammatory_serum_panel")
        spec = specs[row["report_type"]]
        template = Path("docs/opencode/prompts/eval/repobench-eval-prompt.md").read_text(encoding="utf-8")
        prompt = build_repobench_prompt(
            template_text=template,
            row=row,
            spec=spec,
            staged_inputs={"stage_root": "/tmp/staged_inputs", "artifacts": []},
            run_id="repobench_test",
            task_root=Path("/tmp/task_root"),
            session_id="session_test",
        )

        self.assertNotIn("Expected nuggets:", prompt)
        self.assertNotIn("Forbidden overclaims:", prompt)
        for nugget in row["expected_nuggets"]:
            self.assertNotIn(nugget, prompt)
