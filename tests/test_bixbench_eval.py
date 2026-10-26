from __future__ import annotations

import base64
import html
import json
import os
import signal
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gleskit_mcp.eval.bixbench import (
    build_first_move_hint,
    build_question_prompt,
    build_visible_transcript,
    load_opencode_session_export,
    load_opencode_session_stats,
    opencode_activity_kind,
    numeric_for_row,
    parse_final_answer_contract,
    score_bixbench_answer,
    select_bixbench_rows,
    summarize_rows,
    stage_capsule_bundle,
)
from gleskit_mcp.eval.bixbench_runner import (
    BixBenchRunConfig,
    _capture_runner_interruptions,
    _allow_eval_read_paths,
    _validate_selected_runtime_dependencies,
    build_overall_summary,
    build_question_result,
    configure_opencode_eval_agent,
    diagnose_opencode_runtime_failure,
    effective_opencode_tool_timeout_ms,
    harden_opencode_eval_runtime,
    merge_question_attempt_history,
    normalize_continue_mode,
    normalize_skills_mode,
    opencode_subprocess_environment,
    parse_args,
    question_has_finalized_answer,
    question_needs_retry,
    repeated_nonretryable_tool_error,
    resolve_opencode_model,
    sync_native_openai_oauth_from_codex,
    link_episode_capsule_alias,
    should_retry_attempt,
    write_html_summary_report,
)


class BixBenchEvalTests(unittest.TestCase):
    def test_runner_never_promotes_tool_output_after_agent_abstention(self) -> None:
        attempt_record = {
            "attempt": 1,
            "session_title": "session",
            "session_id": "ses-test",
            "returncode": 0,
            "stop_reason": "completed",
            "duration_seconds": 1.0,
            "retryable": False,
            "transcript": [],
            "tool_trace": [
                {
                    "tool": "gleskit_enrich_gene_set",
                    "status": "completed",
                    "output": {
                        "structured_data": {
                            "focus_status": "not_requested",
                            "top_term": {"term_name": "Validated pathway"},
                        }
                    },
                }
            ],
            "final_answer": {
                "raw_text": '{"answer": null, "evidence": [], "abstain": true}',
                "parsed": True,
                "answer": None,
                "evidence": [],
                "confidence": None,
                "abstain": True,
                "parse_error": "",
            },
        }

        record = build_question_result(
            row={
                "question_id": "question-1",
                "short_id": "question",
                "scope": 1,
                "numeric": 0,
                "eval_mode": "str_verifier",
                "capsule_uuid": "capsule-1",
                "question": "Which pathway is the most significantly enriched?",
                "ideal": "Validated pathway",
            },
            run_id="run-1",
            capsule_bundle={"bundle_sha256": "bundle-123"},
            attempt_records=[attempt_record],
            config=BixBenchRunConfig(run_id="run-1"),
            prompt_text="prompt",
            episode_root=Path("/tmp/episode"),
        )

        self.assertTrue(record["abstain"])
        self.assertEqual(record["answer_source"], "agent")
        self.assertIsNone(record["answer"])

    def test_capture_runner_interruptions_records_sigterm(self) -> None:
        with _capture_runner_interruptions() as state:
            os.kill(os.getpid(), signal.SIGTERM)

        self.assertEqual(state["signal"], signal.SIGTERM)

    def test_normalize_skills_mode_accepts_common_tokens(self) -> None:
        self.assertEqual(normalize_skills_mode("on"), "on")
        self.assertEqual(normalize_skills_mode("true"), "on")
        self.assertEqual(normalize_skills_mode("1"), "on")
        self.assertEqual(normalize_skills_mode("off"), "off")
        self.assertEqual(normalize_skills_mode("false"), "off")
        self.assertEqual(normalize_skills_mode("0"), "off")

    def test_normalize_continue_mode_supports_resume_alias(self) -> None:
        self.assertEqual(normalize_continue_mode("fresh", resume_flag=True), "resume")
        self.assertEqual(normalize_continue_mode("append", resume_flag=False), "append")
        with self.assertRaises(ValueError):
            normalize_continue_mode("append", resume_flag=True)

    def test_configure_opencode_eval_agent_can_disable_skill_mode(self) -> None:
        config_payload = {
            "agent": {
                "bixbench-eval": {
                    "prompt": "{file:/tmp/original.md}",
                    "permission": {
                        "skill": {
                            "*": "deny",
                            "gleskit-bixbench-eval": "allow",
                        },
                        "read": {
                            "*": "deny",
                            "/tmp/gleskit-bixbench-eval/**": "allow",
                        },
                        "external_directory": {
                            "*": "deny",
                            "/tmp/gleskit-bixbench-eval/**": "allow",
                        },
                    },
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "opencode.json"
            prompt_path = root / "bixbench-no-skills.md"
            prompt_path.write_text("no-skills", encoding="utf-8")
            config_path.write_text(json.dumps(config_payload), encoding="utf-8")

            configure_opencode_eval_agent(
                config_path,
                agent_name="bixbench-eval",
                agent_prompt_path=prompt_path,
                skill_name="gleskit-bixbench-eval",
                skill_root_name="gleskit-bixbench-eval",
                skills_mode="off",
            )

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            agent_payload = updated["agent"]["bixbench-eval"]
            self.assertEqual(agent_payload["prompt"], f"{{file:{prompt_path.resolve()}}}")
            self.assertEqual(agent_payload["permission"]["skill"]["gleskit-bixbench-eval"], "deny")
            self.assertEqual(agent_payload["permission"]["read"]["/tmp/gleskit-bixbench-eval/**"], "deny")
            self.assertEqual(agent_payload["permission"]["external_directory"]["/tmp/gleskit-bixbench-eval/**"], "deny")

    def test_allow_eval_read_paths_grants_runtime_handoff_roots(self) -> None:
        config = {
            "agent": {
                "bixbench-eval": {
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
            runtime_root = root / "runtime"
            runtime_root.mkdir(parents=True)
            config_path.write_text(json.dumps(config), encoding="utf-8")

            _allow_eval_read_paths(config_path, [runtime_root])

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            permission = updated["agent"]["bixbench-eval"]["permission"]
            resolved = str(runtime_root.resolve())
            self.assertEqual(permission["read"][resolved], "allow")
            self.assertEqual(permission["read"][f"{resolved}/**"], "allow")
            self.assertEqual(permission["external_directory"][resolved], "allow")
            self.assertEqual(permission["external_directory"][f"{resolved}/**"], "allow")

    def test_harden_opencode_eval_runtime_disables_snapshots_and_ignores_git(self) -> None:
        config = {
            "watcher": {
                "ignore": ["tmp/**"],
            },
            "snapshot": True,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "opencode.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            harden_opencode_eval_runtime(config_path)

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(updated["snapshot"])
            self.assertEqual(
                updated["watcher"]["ignore"],
                ["tmp/**", ".git", ".git/**", "**/.git/**"],
            )

    def test_write_html_summary_report_emits_compact_kpi_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_root = root / "runs" / "bixbench_demo"
            full_question = (
                "What is the skewness characteristic of the gene expression "
                "(log2(TPM+1)) distribution across all cell lines in the dataset?"
            )
            raw_response = (
                "```json\n"
                "{\n"
                '  "answer": "The gene expression (log2(TPM+1)) distribution across all cell lines in the dataset is minimally right skewed with a skewness of approximately 0.69.",\n'
                '  "evidence": [\n'
                '    "/tmp/CapsuleData.csv",\n'
                '    "Result from expression statistics with skewness 0.6897 labeled as minimally right skewed"\n'
                "  ],\n"
                '  "abstain": false\n'
                "}\n"
                "```"
            )
            run_manifest = {
                "run_id": "bixbench_demo",
                "created_at": "2026-06-25T00:00:00+00:00",
                "dataset": {"path": "/tmp/BixBench.gleskit_scope.jsonl"},
                "selection": {
                    "scope": "1",
                    "question_ids": ["bix-16-q2"],
                    "limit": 0,
                    "resume": False,
                    "dry_run": False,
                },
                "opencode": {
                    "binary": "opencode",
                    "model": "openai/gpt-4.1-mini",
                    "timeout_seconds": 600,
                    "poll_interval_seconds": 60,
                    "tool_timeout_ms": 300000,
                },
                "planned_episode_count": 1,
            }
            summary_bundle = {
                "overall_summary": {
                    "question_count": 1,
                    "answered_count": 1,
                    "unanswered_count": 0,
                    "accuracy": 0.0,
                    "completed_count": 1,
                    "stopped_count": 0,
                    "failed_count": 0,
                    "manual_review_count": 1,
                    "mean_turns": 3.0,
                    "mean_tool_calls": 2.0,
                    "mean_duration_seconds": 90.0,
                    "mean_total_duration_seconds": 90.0,
                },
                "question_rows": [
                    {
                        "question_id": "bix-16-q2",
                        "question": full_question,
                        "ideal": "Right-skewed with a long tail",
                        "answer": "The gene expression (log2(TPM+1)) distribution across all cell lines in the dataset is minimally right skewed with a skewness of approximately 0.69.",
                        "correct": False,
                        "score": 0.0,
                        "status": "completed",
                        "stop_reason": "completed",
                        "turns": 3,
                        "tool_calls": 2,
                        "duration_seconds": 90.0,
                        "retry_count": 0,
                    }
                ],
                "scope_summary": [
                    {
                        "scope": "1",
                        "question_count": 1,
                        "answered_count": 1,
                        "unanswered_count": 0,
                        "accuracy": 0.0,
                        "manual_review_rate": 1.0,
                        "mean_turns": 3.0,
                        "mean_tool_calls": 2.0,
                        "mean_duration_seconds": 90.0,
                    }
                ],
                "eval_mode_summary": [
                    {
                        "eval_mode": "llm_verifier",
                        "question_count": 1,
                        "answered_count": 1,
                        "unanswered_count": 0,
                        "accuracy": 0.0,
                        "manual_review_rate": 1.0,
                        "mean_turns": 3.0,
                        "mean_tool_calls": 2.0,
                        "mean_duration_seconds": 90.0,
                    }
                ],
                "failure_summary": [
                    {"stop_reason": "completed", "count": 1, "share": 1.0}
                ],
                "latency_rows": [
                    {
                        "question_id": "bix-16-q2",
                        "duration_seconds": 90.0,
                        "turns": 3,
                        "tool_calls": 2,
                        "status": "completed",
                        "stop_reason": "completed",
                    }
                ],
            }
            question_records = [
                {
                    "question_id": "bix-16-q2",
                    "scope": 1,
                    "eval_mode": "llm_verifier",
                    "status": "completed",
                    "correct": False,
                    "manual_review_required": True,
                    "score": 0.0,
                    "turns": 3,
                    "tool_calls": 2,
                    "retry_count": 0,
                    "duration_seconds": 90.0,
                    "question": full_question,
                    "ideal": "Right-skewed with a long tail",
                    "final_answer": {
                        "raw_text": raw_response,
                        "answer": "The gene expression (log2(TPM+1)) distribution across all cell lines in the dataset is minimally right skewed with a skewness of approximately 0.69.",
                    },
                }
            ]

            report_path = write_html_summary_report(
                run_root=run_root,
                run_manifest=run_manifest,
                summary_bundle=summary_bundle,
                question_records=question_records,
            )

            report_text = report_path.read_text(encoding="utf-8")
            self.assertEqual(report_path.name, "report.html")
            self.assertIn("BixBench Run Report", report_text)
            self.assertIn("1m 30s", report_text)
            self.assertIn('class="code-block"', report_text)
            self.assertIn("Aggregate scores below use only answered episodes.", report_text)
            self.assertIn("Answered", report_text)
            self.assertIn("Unanswered", report_text)
            self.assertIn(full_question, report_text)
            self.assertIn(html.escape(raw_response), report_text)
            self.assertIn("Right-skewed with a long tail", report_text)
            self.assertIn("minimally right skewed with a skewness of approximately 0.69", report_text)

    def test_build_overall_summary_scores_only_answered_questions(self) -> None:
        records = [
            {
                "question_id": "answered-1",
                "status": "completed",
                "answer_present": True,
                "score": 1.0,
                "agent_only_score": 1.0,
                "correct": True,
                "turns": 4,
                "tool_calls": 3,
                "duration_seconds": 80.0,
                "total_duration_seconds": 80.0,
                "judge_score": 0.9,
                "committee_level_mean_final": 3.6,
                "judge_agreement_with_benchmark": "agree",
                "manual_review_required": False,
                "answer_source": "agent",
                "numeric": 0,
            },
            {
                "question_id": "failed-1",
                "status": "failed",
                "answer_present": False,
                "score": 0.0,
                "agent_only_score": 0.0,
                "correct": False,
                "turns": 8,
                "tool_calls": 6,
                "duration_seconds": 120.0,
                "total_duration_seconds": 120.0,
                "judge_score": 0.0,
                "judge_agreement_with_benchmark": "disagree",
                "manual_review_required": False,
                "answer_source": "abstain",
                "numeric": 1,
            },
        ]

        overall = build_overall_summary(records)

        self.assertEqual(overall["question_count"], 2)
        self.assertEqual(overall["answered_count"], 1)
        self.assertEqual(overall["unanswered_count"], 1)
        self.assertEqual(overall["benchmark_score_count"], 1)
        self.assertEqual(overall["committee_score_count"], 1)
        self.assertEqual(overall["benchmark_score_mean"], 1.0)
        self.assertEqual(overall["committee_level_mean_count"], 1)
        self.assertEqual(overall["committee_level_mean_mean"], 3.6)
        self.assertEqual(overall["committee_score_mean"], 0.9)

    def test_summarize_rows_exposes_answered_and_unanswered_counts(self) -> None:
        rows = [
            {
                "scope": "1",
                "question_id": "q1",
                "answer_present": True,
                "correct": True,
                "manual_review_required": False,
                "turns": 2,
                "tool_calls": 1,
                "duration_seconds": 30.0,
            },
            {
                "scope": "1",
                "question_id": "q2",
                "answer_present": False,
                "correct": False,
                "manual_review_required": False,
                "turns": 5,
                "tool_calls": 4,
                "duration_seconds": 90.0,
            },
        ]

        summary = summarize_rows(rows, ["scope"])

        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["question_count"], 2)
        self.assertEqual(summary[0]["answered_count"], 1)
        self.assertEqual(summary[0]["unanswered_count"], 1)
        self.assertEqual(summary[0]["accuracy"], 1.0)

    def test_parse_args_exposes_adjustable_timeout_defaults(self) -> None:
        args = parse_args([])
        self.assertEqual(args.timeout_seconds, 600)
        self.assertEqual(args.opencode_tool_timeout_ms, 0)
        self.assertEqual(args.poll_interval_seconds, 60)
        self.assertEqual(args.random_count, 0)
        self.assertEqual(args.continue_mode, "fresh")
        self.assertEqual(parse_args(["--max-retries", "0"]).max_retries, 0)
        self.assertEqual(
            effective_opencode_tool_timeout_ms(
                BixBenchRunConfig(run_id="budget", timeout_seconds=1200, poll_interval_seconds=60)
            ),
            1_140_000,
        )

        custom = parse_args(
            [
                "--timeout-seconds",
                "420",
                "--opencode-tool-timeout-ms",
                "45000",
                "--poll-interval-seconds",
                "15",
                "--random-count",
                "12",
                "--random-seed",
                "7",
                "--continue-mode",
                "append",
            ]
        )
        self.assertEqual(custom.timeout_seconds, 420)
        self.assertEqual(custom.opencode_tool_timeout_ms, 45000)
        self.assertEqual(custom.poll_interval_seconds, 15)
        self.assertEqual(custom.random_count, 12)
        self.assertEqual(custom.random_seed, 7)
        self.assertEqual(custom.continue_mode, "append")

    def test_question_retry_logic_requires_finalized_answer(self) -> None:
        completed = {
            "status": "completed",
            "final_answer": {"answer": "107", "abstain": False},
        }
        stopped = {
            "status": "stopped",
            "final_answer": {},
        }
        abstained = {
            "status": "completed",
            "final_answer": {"answer": "", "abstain": True},
        }
        malformed_abstain = {
            "status": "completed",
            "final_answer": {"answer": "I need to inspect more files.", "abstain": True},
        }
        parse_failed = {
            "status": "completed",
            "final_answer": {"answer": "I need to inspect more files.", "abstain": False, "parsed": False},
        }

        self.assertTrue(question_has_finalized_answer(completed))
        self.assertFalse(question_needs_retry(completed))
        self.assertTrue(question_has_finalized_answer(abstained))
        self.assertFalse(question_needs_retry(abstained))
        self.assertFalse(question_has_finalized_answer(malformed_abstain))
        self.assertTrue(question_needs_retry(malformed_abstain))
        self.assertFalse(question_has_finalized_answer(parse_failed))
        self.assertTrue(question_needs_retry(parse_failed))
        self.assertFalse(question_has_finalized_answer(stopped))
        self.assertTrue(question_needs_retry(stopped))

    def test_merge_question_attempt_history_preserves_prior_attempts(self) -> None:
        existing = {
            "attempts": [
                {"attempt": 1, "duration_seconds": 10.0},
            ]
        }
        current = {
            "attempts": [
                {"attempt": 2, "duration_seconds": 12.5},
                {"attempt": 3, "duration_seconds": 9.0},
            ],
            "attempt_count": 2,
            "retry_count": 1,
            "total_duration_seconds": 21.5,
        }

        merged = merge_question_attempt_history(existing, current)

        self.assertEqual(merged["attempt_count"], 3)
        self.assertEqual(merged["retry_count"], 2)
        self.assertEqual(merged["total_duration_seconds"], 31.5)

    def test_resolve_opencode_model_maps_bare_id_to_unique_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "opencode.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "customprovider": {
                                "models": {
                                    "gpt-4.1-mini": {"name": "gpt-4.1-mini"},
                                    "gpt-5.4-mini": {"name": "gpt-5.4-mini"},
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            resolved, note = resolve_opencode_model("gpt-4.1-mini", config_path)
            self.assertEqual(resolved, "customprovider/gpt-4.1-mini")
            self.assertIn("Resolved bare model id", note or "")

    def test_resolve_opencode_model_rejects_explicit_unconfigured_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "opencode.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider": {
                            "customprovider": {
                                "models": {
                                    "gpt-4.1-mini": {"name": "gpt-4.1-mini"},
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "openai/gpt-5.4-mini"):
                resolve_opencode_model("openai/gpt-5.4-mini", config_path)

    def test_opencode_subprocess_environment_uses_oauth_for_native_openai(self) -> None:
        env = opencode_subprocess_environment(
            "openai/gpt-5.6-terra",
            base_environment={
                "OPENAI_API_KEY": "direct-api-key",
                "OPENAI_BASE_URL": "https://api.openai.com/v1",
                "OPENAI_ORGANIZATION": "org-direct",
                "OPENAI_PROJECT": "project-direct",
                "OPENROUTER_API_KEY": "needed-for-judges",
            },
        )

        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertNotIn("OPENAI_ORGANIZATION", env)
        self.assertNotIn("OPENAI_PROJECT", env)
        self.assertEqual(env["OPENROUTER_API_KEY"], "needed-for-judges")

    def test_sync_native_openai_oauth_from_codex_preserves_other_provider_credentials(self) -> None:
        def jwt_with_expiry(expiry_seconds: int) -> str:
            encoded = json.dumps({"exp": expiry_seconds}).encode("utf-8")
            payload = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
            return f"header.{payload}.signature"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_auth = root / "codex-auth.json"
            target_auth = root / "opencode-auth.json"
            codex_auth.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": jwt_with_expiry(2_000_000_000),
                            "refresh_token": "fresh-refresh",
                            "account_id": "account-123",
                        }
                    }
                ),
                encoding="utf-8",
            )
            target_auth.write_text(
                json.dumps({"myprovider": {"type": "api", "key": "preserved"}}),
                encoding="utf-8",
            )

            synced = sync_native_openai_oauth_from_codex(
                target_auth,
                codex_auth_path=codex_auth,
                now_seconds=1_700_000_000,
            )

            result = json.loads(target_auth.read_text(encoding="utf-8"))
            self.assertTrue(synced)
            self.assertEqual(result["myprovider"]["key"], "preserved")
            self.assertEqual(result["openai"]["type"], "oauth")
            self.assertEqual(result["openai"]["refresh"], "fresh-refresh")
            self.assertEqual(result["openai"]["accountId"], "account-123")

    def test_diagnose_opencode_runtime_failure_detects_provider_connection_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            (log_dir / "runtime.log").write_text(
                "INFO service=llm providerID=customprovider modelID=gpt-4.1-mini stream\n"
                "ERROR service=llm providerID=customprovider modelID=gpt-4.1-mini "
                'error={"error":{"code":"ConnectionRefused","path":"https://api.openai.com/v1/responses","errno":0}} stream error\n',
                encoding="utf-8",
            )
            diagnostic = diagnose_opencode_runtime_failure(log_dir)
            self.assertIsNotNone(diagnostic)
            self.assertEqual(diagnostic["stop_reason"], "provider_connection_refused")
            self.assertEqual(diagnostic["provider_id"], "customprovider")
            self.assertEqual(diagnostic["model_id"], "gpt-4.1-mini")
            self.assertTrue(diagnostic["retryable"])

    def test_should_retry_attempt_allows_retryable_zero_exit_runtime_failures(self) -> None:
        attempt_record = {
            "returncode": 0,
            "watchdog_triggered": False,
            "retryable": True,
        }
        self.assertTrue(should_retry_attempt(attempt_record, attempt_index=0, max_retries=1))

    def test_runtime_dependency_check_accepts_available_apeglm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            fake_settings = SimpleNamespace(rscript_bin="Rscript", r_lib_root=project_root / "var" / "r_lib")

            class _Completed:
                stdout = "true"
                stderr = ""
                returncode = 0

            with patch("gleskit_mcp.eval.bixbench_runner.load_settings", return_value=fake_settings):
                with patch("gleskit_mcp.eval.bixbench_runner.RJsonScriptRunner") as runner_cls:
                    runner_cls.return_value._runtime_env.return_value = {"R_LIBS_USER": str(fake_settings.r_lib_root)}
                    with patch("gleskit_mcp.eval.bixbench_runner.subprocess.run", return_value=_Completed()):
                        _validate_selected_runtime_dependencies(
                            project_root,
                            [{"question_id": "q1", "question": "Run apeglm shrinkage on this contrast."}],
                        )

    def test_select_rows_preserves_explicit_order_and_applies_scope_filter(self) -> None:
        rows = [
            {"question_id": "q1", "scope": 0},
            {"question_id": "q2", "scope": 1},
            {"question_id": "q3", "scope": 1},
        ]
        selected = select_bixbench_rows(rows, question_ids=["q3", "q1", "q2"], scope="1")
        self.assertEqual([row["question_id"] for row in selected], ["q3", "q2"])

    def test_numeric_flag_covers_quantitative_but_semantic_question_forms(self) -> None:
        self.assertEqual(numeric_for_row({"ideal": "4.81-fold increase in tumor"}), 1)
        self.assertEqual(numeric_for_row({"ideal": "2x larger in fungi"}), 1)
        self.assertEqual(numeric_for_row({"ideal": "Right-skewed with a long tail"}), 0)

    def test_build_question_prompt_flags_audit_only_notebook_files(self) -> None:
        prompt = build_question_prompt(
            template_text="Base instructions.",
            row={
                "question_id": "bix-27-q2",
                "short_id": "bix-27",
                "scope": 1,
                "eval_mode": "range_verifier",
                "capsule_uuid": "capsule-123",
                "paper": "paper",
                "categories": ["gene-level"],
                "question": "How many?",
            },
            staged_bundle={
                "capsule_root": "/tmp/run/capsules/capsule-123",
                "files": {
                    "visible": [{"path": "/tmp/run/capsules/capsule-123/visible/CapsuleData/file.csv"}],
                    "audit": [{"path": "/tmp/run/capsules/capsule-123/audit/CapsuleNotebook/notebook.ipynb"}],
                },
            },
            run_id="run-1",
            question_root=Path("/tmp/run/episodes/bix-27-q2"),
        )
        self.assertIn("Do not inspect the audit notebook files.", prompt)
        self.assertIn("/tmp/run/capsules/capsule-123/visible/CapsuleData/file.csv", prompt)
        self.assertIn("Audit-only files", prompt)

    def test_build_question_prompt_suggests_deseq2_first_move_for_counts_and_metadata_workbook(self) -> None:
        prompt = build_question_prompt(
            template_text="Base instructions.",
            row={
                "question_id": "bix-1-q1",
                "short_id": "bix-1",
                "scope": 1,
                "eval_mode": "str_verifier",
                "capsule_uuid": "capsule-123",
                "paper": "paper",
                "categories": ["RNA-seq"],
                "question": "Using the provided RNA-seq count data and metadata files, perform DESeq2 analysis.",
            },
            staged_bundle={
                "capsule_root": "/tmp/run/capsules/capsule-123",
                "files": {
                    "visible": [
                        {"path": "/tmp/run/capsules/capsule-123/visible/CapsuleData/counts_table.txt"},
                        {"path": "/tmp/run/capsules/capsule-123/visible/CapsuleData/metadata.xlsx"},
                    ],
                    "audit": [],
                },
            },
            run_id="run-1",
            question_root=Path("/tmp/run/episodes/bix-1-q1"),
        )
        self.assertIn("Suggested First Move", prompt)
        self.assertIn("analyze_deseq2_counts", prompt)
        self.assertIn("counts_path=/tmp/run/capsules/capsule-123/visible/CapsuleData/counts_table.txt", prompt)
        self.assertIn("sample_metadata_path=/tmp/run/capsules/capsule-123/visible/CapsuleData/metadata.xlsx", prompt)
        self.assertIn("do not read workbook or TSV contents manually", prompt)

    def test_build_question_prompt_prefers_flat_prompt_visible_files_when_available(self) -> None:
        prompt = build_question_prompt(
            template_text="Base instructions.",
            row={
                "question_id": "bix-13-q2",
                "short_id": "bix-13",
                "scope": 1,
                "eval_mode": "str_verifier",
                "capsule_uuid": "capsule-123",
                "paper": "paper",
                "categories": ["RNA-seq"],
                "question": "Use DESeq2 on the visible files.",
            },
            staged_bundle={
                "capsule_root": "/tmp/run/capsules/capsule-123",
                "prompt_visible_files": [
                    "/tmp/run/capsules/capsule-123/visible/experiment_metadata.csv",
                    "/tmp/run/capsules/capsule-123/visible/raw_counts.csv",
                ],
                "files": {
                    "visible": [
                        {"path": "/tmp/run/capsules/capsule-123/visible/CapsuleData-capsule-123/experiment_metadata.csv"},
                        {"path": "/tmp/run/capsules/capsule-123/visible/CapsuleData-capsule-123/raw_counts.csv"},
                    ],
                    "audit": [],
                },
            },
            run_id="run-1",
            question_root=Path("/tmp/run/episodes/bix-13-q2"),
        )
        self.assertIn("/tmp/run/capsules/capsule-123/visible/raw_counts.csv", prompt)
        self.assertIn("/tmp/run/capsules/capsule-123/visible/experiment_metadata.csv", prompt)
        self.assertNotIn("/tmp/run/capsules/capsule-123/visible/CapsuleData-capsule-123/raw_counts.csv", prompt)

    def test_build_question_prompt_adds_deseq2_sample_and_pairwise_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            metadata_path = root / "experiment_metadata.csv"
            counts_path = root / "raw_counts.csv"
            metadata_path.write_text(
                "SampleNumber,AzentaName,Media,Strain,Replicate\n"
                "1,resub-36,M1,JBX1,1\n"
                "2,resub-35,M1,JBX97,1\n"
                "3,resub-34,M1,JBX98,1\n"
                "4,resub-33,M1,JBX99,1\n",
                encoding="utf-8",
            )
            counts_path.write_text(
                ",resub-36,resub-35,resub-34,resub-33\n"
                "geneA,10,20,30,40\n",
                encoding="utf-8",
            )
            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-13-q2",
                    "short_id": "bix-13",
                    "scope": 1,
                    "eval_mode": "str_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "Using DESeq2 relative to strain JBX1, how many genes are unique in JBX98 but not JBX97 or JBX99?",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(metadata_path), str(counts_path)],
                    "files": {"visible": [{"path": str(metadata_path)}, {"path": str(counts_path)}], "audit": []},
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-13-q2",
            )
            self.assertIn("sample_column=AzentaName", prompt)
            self.assertIn("`Strain` levels: [JBX1, JBX97, JBX98, JBX99]", prompt)
            self.assertIn("Run separate pairwise `gleskit_analyze_deseq2_counts` calls with explicit `condition_a` and `condition_b`", prompt)

    def test_build_question_prompt_detects_coldata_workbook_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            counts_path = root / "Issy_ASXL1_blood_featureCounts_GeneTable_final.txt"
            metadata_path = root / "Issy_ASXL1_blood_coldata_gender.xlsx"
            counts_path.write_text(
                "Geneid\tMGD1567B\tMGD1569B\tMGD1613B\n"
                "ENSG00000105737.9\t10\t20\t30\n",
                encoding="utf-8",
            )
            import pandas as pd

            pd.DataFrame(
                {
                    "sample": ["MGD1567B", "MGD1569B", "MGD1613B"],
                    "condition": ["ASXL1", "Control", "ASXL1"],
                    "sex": ["M", "F", "M"],
                }
            ).to_excel(metadata_path, index=False)

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-49-q2",
                    "short_id": "bix-49",
                    "scope": 1,
                    "eval_mode": "str_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the adjusted p-value for gene GRIK5 when performing differential gene expression analysis with lfc shrinkage (type=apeglm) comparing disease (ASXL1 mutation) vs control?",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(metadata_path), str(counts_path)],
                    "files": {"visible": [{"path": str(metadata_path)}, {"path": str(counts_path)}], "audit": []},
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-49-q2",
            )

            self.assertIn("Suggested First Move", prompt)
            self.assertIn("analyze_deseq2_counts", prompt)
            self.assertIn(f"sample_metadata_path={metadata_path}", prompt)
            self.assertIn("`condition` levels: [ASXL1, Control]", prompt)
            self.assertIn("focus_genes=['GRIK5']", prompt)

    def test_build_question_prompt_adds_matrix_correlation_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            matrix_path = root / "BatchCorrectedReadCounts_Zenodo.csv"
            annotation_path = root / "GeneMetaInfo_Zenodo.csv"
            sample_metadata_path = root / "Sample_annotated_Zenodo.csv"
            matrix_path.write_text(
                ",CHC096CD4,CHC096CD8\n"
                "DDX11L1,10,11\n"
                "WASH7P,20,21\n"
                "MIR6859-1,30,31\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "Unnamed: 0,Geneid,Chr,Length,gene_biotype\n"
                "DDX11L1,DDX11L1,1,1735,protein_coding\n"
                "WASH7P,WASH7P,1,1351,protein_coding\n"
                "MIR6859-1,MIR6859-1,1,68,miRNA\n",
                encoding="utf-8",
            )
            sample_metadata_path.write_text(
                "sample,samp,samtype,celltype,batch,sex\n"
                "CHC096CD4,CHC096,refsamp,CD4,1,M\n"
                "CHC096CD8,CHC096,refsamp,CD8,1,M\n",
                encoding="utf-8",
            )

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-22-q2",
                    "short_id": "bix-22",
                    "scope": 1,
                    "eval_mode": "range_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the Pearson correlation coefficient between gene length and mean expression (across samples) for protein-coding genes in CD4 immune cells?",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(matrix_path), str(annotation_path), str(sample_metadata_path)],
                    "files": {
                        "visible": [
                            {"path": str(matrix_path)},
                            {"path": str(annotation_path)},
                            {"path": str(sample_metadata_path)},
                        ],
                        "audit": [],
                    },
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-22-q2",
            )

            self.assertIn("matrix_annotation_correlation", prompt)
            self.assertIn(f"source_path={matrix_path}", prompt)
            self.assertIn(f"annotation_path={annotation_path}", prompt)
            self.assertIn("annotation_key_column=Geneid", prompt)
            self.assertIn("annotation_value_column=Length", prompt)
            self.assertIn("gene_biotype=protein_coding", prompt)
            self.assertIn("celltype=CD4", prompt)
            self.assertIn(f"metadata_path={sample_metadata_path}", prompt)
            self.assertIn("metadata_group_column=celltype", prompt)
            self.assertIn("metadata_group_value=CD4", prompt)
            self.assertNotIn("row_id_column=Unnamed: 0", prompt)
            self.assertIn("Omit `row_id_column`", prompt)

    def test_build_question_prompt_routes_sheetwise_single_cell_deg_workbook_to_single_cell_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workbook_path = root / "Supplemental Data File 1 PostvsPre_DEG_tables_ANNOTATED by_cell_type.xlsx"

            import pandas as pd

            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    {
                        "Unnamed: 0": ["TP53", "BRCA1"],
                        "p_val": [0.0001, 0.002],
                        "avg_log2FC": [2.1, -1.5],
                        "pct.1": [0.8, 0.2],
                        "pct.2": [0.1, 0.7],
                        "p_val_adj": [0.001, 0.02],
                    }
                ).to_excel(writer, sheet_name="CD14 Mono", index=False)
                pd.DataFrame(
                    {
                        "Unnamed: 0": ["LYZ"],
                        "p_val": [0.01],
                        "avg_log2FC": [1.2],
                        "pct.1": [0.4],
                        "pct.2": [0.2],
                        "p_val_adj": [0.03],
                    }
                ).to_excel(writer, sheet_name="NK", index=False)

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-33-q1",
                    "short_id": "bix-33",
                    "scope": 1,
                    "eval_mode": "str_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["Single-Cell Analysis", "Differential Expression Analysis"],
                    "question": "Which immune cell type has the highest number of significantly differentially expressed genes after treatment?",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(workbook_path)],
                    "files": {"visible": [{"path": str(workbook_path)}], "audit": []},
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-33-q1",
            )

            self.assertIn("Suggested First Move", prompt)
            self.assertIn("analysis=cell_type_deg_counts", prompt)
            self.assertIn("analyze_single_cell_rnaseq", prompt)
            self.assertIn("do not loop sheet-by-sheet with `gleskit_analyze_rnaseq_result`", prompt)
            self.assertIn("omit `gene_column` and let the tool use the first column automatically", prompt)

    def test_build_question_prompt_adds_matrix_fold_change_summary_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            matrix_path = root / "BatchCorrectedReadCounts_Zenodo.csv"
            annotation_path = root / "GeneMetaInfo_Zenodo.csv"
            sample_metadata_path = root / "Sample_annotated_Zenodo.csv"
            matrix_path.write_text(
                ",CHC096CD14,CHC096CD19\n"
                "MIR1,10,15\n"
                "MIR2,20,18\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "Unnamed: 0,Geneid,gene_biotype\n"
                "MIR1,MIR1,miRNA\n"
                "MIR2,MIR2,miRNA\n",
                encoding="utf-8",
            )
            sample_metadata_path.write_text(
                "sample,celltype\n"
                "CHC096CD14,CD14\n"
                "CHC096CD19,CD19\n",
                encoding="utf-8",
            )

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-36-q3",
                    "short_id": "bix-36",
                    "scope": 1,
                    "eval_mode": "range_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the median log2 fold change in global miRNA expression between CD14 and CD19 cell types?",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(matrix_path), str(annotation_path), str(sample_metadata_path)],
                    "files": {
                        "visible": [
                            {"path": str(matrix_path)},
                            {"path": str(annotation_path)},
                            {"path": str(sample_metadata_path)},
                        ],
                        "audit": [],
                    },
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-36-q3",
            )

            self.assertIn("matrix_group_fold_change_summary", prompt)
            self.assertIn(f"source_path={matrix_path}", prompt)
            self.assertIn(f"metadata_path={sample_metadata_path}", prompt)
            self.assertIn("metadata_group_column=celltype", prompt)
            self.assertIn("group_a=CD14", prompt)
            self.assertIn("group_b=CD19", prompt)
            self.assertIn("feature_annotation_group_column=gene_biotype", prompt)
            self.assertIn("feature_annotation_group_value=miRNA", prompt)
            self.assertIn("summary_stat=median", prompt)

    def test_build_question_prompt_does_not_invent_pairwise_lfc_estimand_for_anova(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            matrix_path = root / "BatchCorrectedReadCounts_Zenodo.csv"
            annotation_path = root / "GeneMetaInfo_Zenodo.csv"
            sample_metadata_path = root / "Sample_annotated_Zenodo.csv"
            matrix_path.write_text(
                ",CHC096CD4,CHC096CD8,CHC096CD14,CHC096CD19\n"
                "MIR1,10,11,12,13\n"
                "MIR2,20,21,22,23\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "Unnamed: 0,Geneid,gene_biotype\n"
                "MIR1,MIR1,miRNA\n"
                "MIR2,MIR2,miRNA\n",
                encoding="utf-8",
            )
            sample_metadata_path.write_text(
                "sample,celltype,sex\n"
                "CHC096CD4,CD4,M\n"
                "CHC096CD8,CD8,M\n"
                "CHC096CD14,CD14,M\n"
                "CHC096CD19,CD19,M\n",
                encoding="utf-8",
            )

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-36-q1",
                    "short_id": "bix-36",
                    "scope": 1,
                    "eval_mode": "range_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the F-statistic calculated from a one-way ANOVA test comparing the miRNA expression levels across immune cell types (excluding PBMCs)?",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(matrix_path), str(annotation_path), str(sample_metadata_path)],
                    "files": {
                        "visible": [
                            {"path": str(matrix_path)},
                            {"path": str(annotation_path)},
                            {"path": str(sample_metadata_path)},
                        ],
                        "audit": [],
                    },
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-36-q1",
            )

            self.assertNotIn("observation_unit=pairwise_log2_fold_change", prompt)
            self.assertIn("observation_unit=featurewise", prompt)
            self.assertIn("metadata_group_column=celltype", prompt)
            self.assertNotIn("observation_unit=flattened_values", prompt)
            self.assertNotIn("metadata_group_column=sex", prompt)

    def test_build_question_prompt_preserves_enrichr_library_and_de_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            counts_path = root / "counts_raw_unfiltered.csv"
            metadata_path = root / "sample_layout.csv"
            counts_path.write_text(",S1,S2\nTP53,10,20\n", encoding="utf-8")
            metadata_path.write_text("SampleID,Group\nS1,DMSO\nS2,Cisplatin_IC50_CBD_IC50\n", encoding="utf-8")

            hints = build_first_move_hint(
                {
                    "question_id": "bix-43-q5",
                    "question": "What is the primary cellular mechanism enriched using gseapy as method and Reactome_2022 as geneset library with all differentially expressed genes (p<0.05, absolute lfc>0.5, baseMean>10)?",
                    "categories": ["RNA-seq"],
                },
                [str(counts_path), str(metadata_path)],
                [],
            )
            rendered = "\n".join(hints)

            self.assertIn('library="Reactome_2022"', rendered)
            self.assertIn('backend="enrichr"', rendered)
            self.assertIn('significance_mode="p_value"', rendered)
            self.assertIn("base_mean_threshold=10", rendered)
            self.assertIn("log2_fold_change_threshold=0.5", rendered)
            self.assertIn("all-significant", rendered)

    def test_build_question_prompt_preserves_organism_specific_gseapy_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            counts_path = root / "counts.csv"
            metadata_path = root / "metadata.csv"
            counts_path.write_text("gene,mouse_1,mouse_2\nActb,10,12\nGapdh,9,11\n", encoding="utf-8")
            metadata_path.write_text("sample,condition\nmouse_1,KD\nmouse_2,Control\n", encoding="utf-8")

            hints = build_first_move_hint(
                {
                    "question": (
                        "Using GSEApy with the KEGG_2019_Mouse gene set library, identify the most "
                        "significantly enriched pathway after differential expression analysis."
                    ),
                    "categories": "RNA-seq,Differential Expression Analysis",
                },
                [str(counts_path), str(metadata_path)],
                [],
            )
            rendered = "\n".join(hints)

            self.assertIn('library="KEGG_2019_Mouse"', rendered)
            self.assertIn('backend="enrichr"', rendered)

    def test_build_first_move_preserves_explicit_gene_reference_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            counts_path = root / "raw_counts.csv"
            metadata_path = root / "sample_metadata.csv"
            reference_path = root / "gencode_gene_reference.csv"
            counts_path.write_text("gene,S1,S2\nTP53,10,20\n", encoding="utf-8")
            metadata_path.write_text("sample,condition\nS1,Case\nS2,Control\n", encoding="utf-8")
            reference_path.write_text("gene_id\nENSG000001\n", encoding="utf-8")

            hints = build_first_move_hint(
                {
                    "question": (
                        "What is the adjusted p-value for a named GO process using enrichGO after removing "
                        "terms with similarity > 0.7, with all genes from gencode as background after "
                        "differential expression?"
                    ),
                    "categories": ["RNA-seq"],
                },
                [str(counts_path), str(metadata_path), str(reference_path)],
                [],
            )
            rendered = "\n".join(hints)

            self.assertIn('backend="clusterprofiler"', rendered)
            self.assertIn("simplify_go_similarity=0.7", rendered)
            self.assertIn(f"background_genes_path={reference_path}", rendered)
            self.assertIn("do not replace it with the DESeq2 tested-gene handoff", rendered)

    def test_build_question_prompt_adds_wilson_pipeline_hints_for_workbook_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workbook_path = root / "Data_deposition_RNAseq_Paroxetine_2017.xlsx"

            import pandas as pd

            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    {
                        "gene": ["G1", "G2"],
                        "Final blood 1": [10, 12],
                        "Baseline blood 1": [9, 10],
                    }
                ).to_excel(writer, sheet_name="matrix", index=False)
                pd.DataFrame(
                    {
                        "sample": ["Final blood 1", "Baseline blood 1"],
                        "group": ["Final blood", "Baseline blood"],
                    }
                ).to_excel(writer, sheet_name="metadata", index=False)

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-3-q2",
                    "short_id": "bix-3",
                    "scope": 1,
                    "eval_mode": "range_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the 95% confidence interval for the proportion of differentially expressed genes when comparing Final blood and Baseline blood? Use the Wilson method to compute the confidence interval.",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(workbook_path)],
                    "files": {"visible": [{"path": str(workbook_path)}], "audit": []},
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-3-q2",
            )

            self.assertIn("inspect_tabular_data", prompt)
            self.assertIn("candidate_group_columns", prompt)
            self.assertIn("matrix_featurewise_group_test", prompt)
            self.assertIn("proportion_interval", prompt)
            self.assertIn("Expected matrix-like sheet candidate: `matrix`", prompt)
            self.assertIn("Expected metadata-like sheet candidate: `metadata`", prompt)
            self.assertIn("metadata_group_column=group", prompt)
            self.assertIn("group_a=Final blood", prompt)
            self.assertIn("group_b=Baseline blood", prompt)

    def test_build_question_prompt_falls_back_when_workbook_introspection_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workbook_path = root / "broken_workbook.xlsx"
            workbook_path.write_text("not an actual workbook", encoding="utf-8")

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-3-q2",
                    "short_id": "bix-3",
                    "scope": 1,
                    "eval_mode": "range_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the 95% confidence interval for the proportion of differentially expressed genes when comparing Final blood and Baseline blood? Use the Wilson method to compute the confidence interval.",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(workbook_path)],
                    "files": {"visible": [{"path": str(workbook_path)}], "audit": []},
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-3-q2",
            )

            self.assertIn("Start with `gleskit_inspect_tabular_data`", prompt)
            self.assertIn("Keep the workflow path-driven", prompt)

    def test_build_question_prompt_prefers_overlap_metadata_key_for_workbook_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workbook_path = root / "Data_deposition_RNAseq_Paroxetine_2017.xlsx"

            import pandas as pd

            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    [
                        ["ignore", "ignore", "ignore", "ignore"],
                        ["Index", "GeneID", "1_1", "1_2"],
                        [1, "GeneA", 10, 12],
                        [2, "GeneB", 8, 7],
                    ]
                ).to_excel(writer, sheet_name="NormCount", index=False, header=False)
                pd.DataFrame(
                    {
                        "Sample ID": ["Final blood 1", "Baseline blood 1"],
                        "Code": ["1_1", "1_2"],
                        "Tissue": ["final_blood", "baseline_blood"],
                    }
                ).to_excel(writer, sheet_name="metadata", index=False)

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-3-q2",
                    "short_id": "bix-3",
                    "scope": 1,
                    "eval_mode": "range_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the 95% confidence interval for the proportion of differentially expressed genes when comparing final_blood and baseline_blood? Use the Wilson method to compute the confidence interval.",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(workbook_path)],
                    "files": {"visible": [{"path": str(workbook_path)}], "audit": []},
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-3-q2",
            )

            self.assertIn("Start with `gleskit_inspect_tabular_data`", prompt)
            self.assertIn("Expected matrix-like sheet candidate: `NormCount`.", prompt)
            self.assertIn("Expected metadata-like sheet candidate: `metadata`.", prompt)
            self.assertIn("Likely sample key column after inspection: `Code`.", prompt)
            self.assertNotIn("Sample ID`.", prompt)

    def test_build_question_prompt_reads_full_workbook_metadata_for_group_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workbook_path = root / "Data_deposition_RNAseq_Paroxetine_2017.xlsx"

            import pandas as pd

            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    [
                        ["ignore", "ignore", "ignore", "ignore"],
                        ["Index", "GeneID", "Control_baseline_blood1", "Control_final_blood1"],
                        [1, "GeneA", 10, 12],
                        [2, "GeneB", 8, 7],
                    ]
                ).to_excel(writer, sheet_name="NormCount", index=False, header=False)

                rows = []
                for index in range(55):
                    rows.append(
                        {
                            "Sample ID": f"sample_{index}",
                            "Code": f"other_{index}",
                            "Tissue": "baseline_blood",
                        }
                    )
                rows.append(
                    {
                        "Sample ID": "sample_56",
                        "Code": "Control_final_blood1",
                        "Tissue": "final_blood",
                    }
                )
                rows.append(
                    {
                        "Sample ID": "sample_57",
                        "Code": "Control_baseline_blood1",
                        "Tissue": "baseline_blood",
                    }
                )
                pd.DataFrame(rows).to_excel(writer, sheet_name="metadata", index=False)

            prompt = build_question_prompt(
                template_text="Base instructions.",
                row={
                    "question_id": "bix-3-q2",
                    "short_id": "bix-3",
                    "scope": 1,
                    "eval_mode": "range_verifier",
                    "capsule_uuid": "capsule-123",
                    "paper": "paper",
                    "categories": ["RNA-seq"],
                    "question": "What is the 95% confidence interval for the proportion of differentially expressed genes when comparing final_blood and baseline_blood? Use the Wilson method to compute the confidence interval.",
                },
                staged_bundle={
                    "capsule_root": str(root),
                    "prompt_visible_files": [str(workbook_path)],
                    "files": {"visible": [{"path": str(workbook_path)}], "audit": []},
                },
                run_id="run-1",
                question_root=root / "episodes" / "bix-3-q2",
            )

            self.assertIn("metadata_group_column=Tissue", prompt)
            self.assertIn("final_blood", prompt)
            self.assertIn("baseline_blood", prompt)

    def test_parse_final_answer_contract_supports_new_and_legacy_json_and_plain_text(self) -> None:
        parsed = parse_final_answer_contract(
            "```json\n{\"answer\": 63, \"evidence\": [\"tool\"], \"abstain\": false}\n```"
        )
        self.assertTrue(parsed["parsed"])
        self.assertEqual(parsed["answer"], 63)
        self.assertIsNone(parsed["confidence"])

        legacy = parse_final_answer_contract(
            "```json\n{\"answer\": 63, \"evidence\": [\"tool\"], \"confidence\": 0.9, \"abstain\": false}\n```"
        )
        self.assertTrue(legacy["parsed"])
        self.assertEqual(legacy["confidence"], 0.9)

        fallback = parse_final_answer_contract("63\nHeld-out agreement")
        self.assertFalse(fallback["parsed"])
        self.assertEqual(fallback["answer"], "63")
        self.assertEqual(fallback["evidence"], ["Held-out agreement"])

    def test_score_bixbench_answer_handles_range_and_string_modes(self) -> None:
        range_score = score_bixbench_answer(
            {"ideal": "(10, 12)", "eval_mode": "range_verifier"},
            {"answer": 11, "evidence": [], "confidence": 0.8, "abstain": False},
        )
        self.assertTrue(range_score["correct"])
        self.assertEqual(range_score["score"], 1.0)

        text_score = score_bixbench_answer(
            {"ideal": "CLOCK", "eval_mode": "str_verifier"},
            {"answer": "clock", "evidence": [], "confidence": 0.8, "abstain": False},
        )
        self.assertTrue(text_score["correct"])
        self.assertEqual(text_score["score"], 1.0)

        alias_score = score_bixbench_answer(
            {"ideal": "CD14 Monocytes", "eval_mode": "str_verifier"},
            {"answer": "CD14 Mono (315 significant genes)", "evidence": [], "confidence": None, "abstain": False},
        )
        self.assertTrue(alias_score["correct"])
        self.assertEqual(alias_score["score"], 1.0)

    def test_stage_capsule_bundle_copies_files_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_root = root / "project"
            run_root = root / "run"
            visible_source = (
                project_root
                / "var"
                / "bixbench"
                / "extracted"
                / "bix-27-q2"
                / "CapsuleData-capsule-123"
            )
            audit_source = (
                project_root
                / "var"
                / "bixbench"
                / "extracted"
                / "bix-27-q2"
                / "CapsuleNotebook-capsule-123"
            )
            visible_source.mkdir(parents=True, exist_ok=True)
            audit_source.mkdir(parents=True, exist_ok=True)
            (visible_source / "file.csv").write_text("gene,value\nCLOCK,1\n", encoding="utf-8")
            (audit_source / "notebook.ipynb").write_text("{}", encoding="utf-8")

            bundle = stage_capsule_bundle(
                project_root=project_root,
                run_root=run_root,
                row={
                    "question_id": "bix-27-q2",
                    "capsule_uuid": "capsule-123",
                    "data_folder": "CapsuleFolder-capsule-123.zip",
                },
            )

            bundle_manifest = Path(bundle["capsule_root"]) / "bundle_manifest.json"
            copied_file = Path(bundle["visible_root"]) / "file.csv"
            flat_alias = run_root / "capsules" / "capsule-123" / "visible" / "file.csv"
            self.assertTrue(bundle_manifest.exists())
            self.assertTrue(copied_file.exists())
            self.assertTrue(flat_alias.exists())
            self.assertEqual(copied_file.read_text(encoding="utf-8"), "gene,value\nCLOCK,1\n")
            self.assertEqual(flat_alias.read_text(encoding="utf-8"), "gene,value\nCLOCK,1\n")
            self.assertEqual(Path(bundle["audit_root"]).joinpath("notebook.ipynb").read_text(encoding="utf-8"), "{}")
            self.assertEqual((bundle_manifest.stat().st_mode & 0o222), 0)

    def test_link_episode_capsule_alias_targets_only_the_staged_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            capsule_root = root / "run" / "capsules" / "capsule-123"
            capsule_root.mkdir(parents=True, exist_ok=True)
            episode_root = root / "run" / "episodes" / "bix-27-q2"

            alias = link_episode_capsule_alias(
                episode_root=episode_root,
                capsule_root=str(capsule_root),
            )

            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.resolve(), capsule_root.resolve())
            self.assertEqual(
                link_episode_capsule_alias(
                    episode_root=episode_root,
                    capsule_root=str(capsule_root),
                ),
                alias,
            )

    def test_build_question_result_derives_deseq2_unique_count_from_tool_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            episode_root = root / "episodes" / "bix-13-q2"
            episode_root.mkdir(parents=True, exist_ok=True)

            def write_deseq2_output(label: str, comparison: str, genes: list[str]) -> str:
                gene_path = root / f"{label.replace(' ', '_').lower()}.tsv"
                gene_path.write_text("gene\n" + "\n".join(genes) + "\n", encoding="utf-8")
                output_path = root / f"{label.replace(' ', '_').lower()}_output.json"
                output_path.write_text(
                    json.dumps(
                        {
                            "summary": f"{label} summary",
                            "structured_data": {
                                "label": label,
                                "comparison": comparison,
                                "derived_inputs": {
                                    "all_significant_genes_path": str(gene_path),
                                    "manifest_path": str(output_path.with_name(f"{label.replace(' ', '_').lower()}_manifest.json")),
                                },
                                "significant_gene_sets": {
                                    "all_significant": genes,
                                    "upregulated": [],
                                    "downregulated": [],
                                },
                            },
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                return f"tool output truncated\nFull output saved to: {output_path}"

            shared = ["g_common_1", "g_common_2"]
            jbx97 = shared + ["g_97_only"]
            jbx98 = shared + [f"g_98_{index}" for index in range(107)]
            jbx99 = shared + ["g_99_only"]

            attempt_record = {
                "attempt": 1,
                "session_title": "session",
                "session_id": "ses-test",
                "returncode": 0,
                "stop_reason": "completed",
                "watchdog_triggered": False,
                "duration_seconds": 12.0,
                "session_stats": {"assistant_message_count": 4, "tool_part_count": 4},
                "tool_trace": [
                    {
                        "tool": "gleskit_analyze_deseq2_counts",
                        "status": "completed",
                        "call_id": "call-1",
                        "input": {"comparison": "JBX97 vs JBX1", "label": "DESeq2 JBX97 vs JBX1"},
                        "output": write_deseq2_output("DESeq2 JBX97 vs JBX1", "JBX97 vs JBX1", jbx97),
                    },
                    {
                        "tool": "gleskit_analyze_deseq2_counts",
                        "status": "completed",
                        "call_id": "call-2",
                        "input": {"comparison": "JBX98 vs JBX1", "label": "DESeq2 JBX98 vs JBX1"},
                        "output": write_deseq2_output("DESeq2 JBX98 vs JBX1", "JBX98 vs JBX1", jbx98),
                    },
                    {
                        "tool": "gleskit_analyze_deseq2_counts",
                        "status": "completed",
                        "call_id": "call-3",
                        "input": {"comparison": "JBX99 vs JBX1", "label": "DESeq2 JBX99 vs JBX1"},
                        "output": write_deseq2_output("DESeq2 JBX99 vs JBX1", "JBX99 vs JBX1", jbx99),
                    },
                ],
                "final_answer": {
                    "raw_text": "{\"answer\": null, \"evidence\": [], \"confidence\": 0, \"abstain\": true}",
                    "parsed": True,
                    "answer": None,
                    "evidence": [],
                    "confidence": 0,
                    "abstain": True,
                    "parse_error": None,
                },
                "score": {
                    "score": 0.0,
                    "correct": False,
                    "predicted": None,
                    "predicted_text": "None",
                    "abstain": True,
                    "match_reason": "abstained",
                    "answer_present": False,
                    "manual_review_required": False,
                },
            }

            question_record = build_question_result(
                row={
                    "question_id": "bix-13-q2",
                    "short_id": "bix-13",
                    "scope": 1,
                    "numeric": 1,
                    "eval_mode": "str_verifier",
                    "capsule_uuid": "capsule-1",
                    "question": "Using DESeq2 to conduct differential expression analysis relative to strain JBX1, how many genes are uniquely and statistically significantly differentially expressed in JBX98 but not in either JBX97 or JBX99? Use a Benjamini-Hochberg False Discovery Rate correction threshold of 0.05.",
                    "ideal": "166",
                },
                run_id="run-1",
                capsule_bundle={"bundle_sha256": "bundle-123"},
                attempt_records=[attempt_record],
                config=BixBenchRunConfig(run_id="run-1"),
                prompt_text="prompt",
                episode_root=episode_root,
            )

            self.assertIsNone(question_record["answer"])
            self.assertEqual(question_record["score"], 0.0)
            self.assertEqual(question_record["answer_source"], "agent")
            self.assertEqual(question_record["answer_resolution"]["strategy"], "agent")
            self.assertTrue(question_record["agent_final_answer"]["abstain"])
            self.assertTrue(question_record["final_answer"]["abstain"])
            self.assertNotIn("runner_suggestion", question_record)
            self.assertNotIn("runner_backfill", question_record)

    def test_build_question_result_does_not_use_direct_tool_output_as_an_answer(self) -> None:
        attempt_record = {
            "attempt": 1,
            "session_title": "session",
            "session_id": "ses-test",
            "returncode": 0,
            "stop_reason": "turn_limit",
            "watchdog_triggered": True,
            "duration_seconds": 12.0,
            "session_stats": {"assistant_message_count": 8, "tool_part_count": 2},
            "tool_trace": [
                {
                    "tool": "gleskit_run_expression_statistics",
                    "status": "completed",
                    "call_id": "call-1",
                    "input": {"analysis": "distribution_summary"},
                    "output": {
                        "structured_data": {
                            "analysis": "distribution",
                            "result": {
                                "skewness": 0.6897,
                                "skewness_label": "minimally right skewed",
                            },
                        }
                    },
                }
            ],
            "final_answer": {
                "raw_text": "",
                "parsed": False,
                "answer": None,
                "evidence": [],
                "confidence": None,
                "abstain": True,
                "parse_error": "missing",
            },
            "score": {
                "score": 0.0,
                "correct": False,
                "predicted": None,
                "predicted_text": "",
                "abstain": True,
                "match_reason": "missing_session",
                "answer_present": False,
                "manual_review_required": False,
            },
        }
        question_record = build_question_result(
            row={
                "question_id": "bix-16-q2",
                "short_id": "bix-16",
                "scope": 1,
                "numeric": 1,
                "eval_mode": "range_verifier",
                "capsule_uuid": "capsule-1",
                "question": "What is the skewness characteristic of the gene expression distribution across all cell lines in the dataset?",
                "ideal": "0.69, 0.70",
            },
            run_id="run-1",
            capsule_bundle={"bundle_sha256": "bundle-123"},
            attempt_records=[attempt_record],
            config=BixBenchRunConfig(run_id="run-1"),
            prompt_text="prompt",
            episode_root=Path("/tmp/episode"),
        )

        self.assertEqual(question_record["answer_source"], "abstain")
        self.assertEqual(question_record["stop_reason"], "turn_limit")
        self.assertIsNone(question_record["effective_answer"])

    def test_build_question_result_marks_provider_failures_as_failed(self) -> None:
        question_record = build_question_result(
            row={
                "question_id": "bix-13-q2",
                "short_id": "bix-13",
                "scope": 1,
                "numeric": 1,
                "eval_mode": "str_verifier",
                "capsule_uuid": "capsule-1",
                "question": "Test question",
                "ideal": "107",
            },
            run_id="run-1",
            capsule_bundle={"bundle_sha256": "bundle-123"},
            attempt_records=[
                {
                    "attempt": 1,
                    "session_title": "session",
                    "session_id": "ses-test",
                    "returncode": 0,
                    "stop_reason": "provider_connection_refused",
                    "watchdog_triggered": False,
                    "duration_seconds": 1.0,
                    "session_stats": {"assistant_message_count": 0, "tool_part_count": 0},
                    "tool_trace": [],
                    "transcript": [],
                    "runtime_failure": {"stop_reason": "provider_connection_refused"},
                    "final_answer": {
                        "raw_text": "",
                        "parsed": False,
                        "answer": None,
                        "evidence": [],
                        "confidence": None,
                        "abstain": True,
                        "parse_error": "No assistant answer was captured.",
                    },
                    "score": {
                        "score": 0.0,
                        "correct": False,
                        "predicted": None,
                        "predicted_text": "",
                        "abstain": True,
                        "match_reason": "missing_session",
                        "answer_present": False,
                        "manual_review_required": False,
                    },
                }
            ],
            config=BixBenchRunConfig(run_id="run-1"),
            prompt_text="prompt",
            episode_root=Path("/tmp/episode"),
        )

        self.assertEqual(question_record["status"], "failed")
        self.assertEqual(question_record["stop_reason"], "provider_connection_refused")

    def test_build_question_result_marks_clean_exit_without_answer_as_failed(self) -> None:
        question_record = build_question_result(
            row={"question_id": "bix-test-q1", "scope": 1, "numeric": 1, "eval_mode": "str_verifier", "question": "Test", "ideal": "1"},
            run_id="run-1",
            capsule_bundle={},
            attempt_records=[
                {
                    "attempt": 1,
                    "returncode": 0,
                    "stop_reason": "completed",
                    "watchdog_triggered": False,
                    "duration_seconds": 1.0,
                    "session_stats": {"assistant_message_count": 0, "tool_part_count": 0},
                    "tool_trace": [],
                    "transcript": [],
                    "final_answer": {"parsed": False, "answer": None, "abstain": True, "parse_error": "No assistant answer was captured."},
                }
            ],
            config=BixBenchRunConfig(run_id="run-1"),
            prompt_text="prompt",
            episode_root=Path("/tmp/episode"),
        )

        self.assertEqual(question_record["status"], "failed")
        self.assertEqual(question_record["stop_reason"], "no_answer")

    def test_build_question_result_preserves_interrupted_episode(self) -> None:
        question_record = build_question_result(
            row={"question_id": "bix-test-q1", "scope": 1, "numeric": 1, "eval_mode": "str_verifier", "question": "Test", "ideal": "1"},
            run_id="run-1",
            capsule_bundle={},
            attempt_records=[
                {
                    "attempt": 1,
                    "returncode": -15,
                    "stop_reason": "interrupted",
                    "interruption_signal": 15,
                    "watchdog_triggered": False,
                    "duration_seconds": 1.0,
                    "session_stats": {"assistant_message_count": 0, "tool_part_count": 0},
                    "tool_trace": [],
                    "transcript": [],
                    "final_answer": {"parsed": False, "answer": None, "abstain": True, "parse_error": "No assistant answer was captured."},
                }
            ],
            config=BixBenchRunConfig(run_id="run-1"),
            prompt_text="prompt",
            episode_root=Path("/tmp/episode"),
        )

        self.assertEqual(question_record["status"], "stopped")
        self.assertEqual(question_record["stop_reason"], "interrupted")

    def test_build_question_result_preserves_tool_timeout_without_answer(self) -> None:
        question_record = build_question_result(
            row={"question_id": "bix-test-q1", "scope": 1, "numeric": 1, "eval_mode": "str_verifier", "question": "Test", "ideal": "1"},
            run_id="run-1",
            capsule_bundle={},
            attempt_records=[
                {
                    "attempt": 1,
                    "returncode": -15,
                    "stop_reason": "tool_timeout",
                    "watchdog_triggered": True,
                    "duration_seconds": 1.0,
                    "session_stats": {"assistant_message_count": 0, "tool_part_count": 1},
                    "tool_trace": [],
                    "transcript": [],
                    "final_answer": {"parsed": False, "answer": None, "abstain": True, "parse_error": "No assistant answer was captured."},
                }
            ],
            config=BixBenchRunConfig(run_id="run-1"),
            prompt_text="prompt",
            episode_root=Path("/tmp/episode"),
        )

        self.assertEqual(question_record["status"], "stopped")
        self.assertEqual(question_record["stop_reason"], "tool_timeout")

    def test_repeated_nonretryable_tool_error_is_detected(self) -> None:
        diagnostic = repeated_nonretryable_tool_error(
            [
                {"tool": "gleskit_run_expression_statistics", "status": "error", "output": {"error_code": "invalid_input", "retryable": False, "diagnostic": "group column not found"}},
                {"tool": "gleskit_run_expression_statistics", "status": "error", "output": {"error_code": "invalid_input", "retryable": False, "diagnostic": "group column not found"}},
            ]
        )
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic["error_code"], "invalid_input")
        self.assertFalse(diagnostic["retryable"])

    def test_opencode_sqlite_reconstruction_returns_visible_transcript_and_tool_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "opencode.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE session (
                      id TEXT PRIMARY KEY,
                      project_id TEXT,
                      parent_id TEXT,
                      slug TEXT,
                      directory TEXT,
                      title TEXT,
                      version TEXT,
                      share_url TEXT,
                      summary_additions INTEGER,
                      summary_deletions INTEGER,
                      summary_files INTEGER,
                      summary_diffs TEXT,
                      revert TEXT,
                      permission TEXT,
                      time_created INTEGER,
                      time_updated INTEGER,
                      time_compacting INTEGER,
                      time_archived INTEGER,
                      workspace_id TEXT,
                      path TEXT,
                      agent TEXT,
                      model TEXT,
                      cost REAL,
                      tokens_input INTEGER,
                      tokens_output INTEGER,
                      tokens_reasoning INTEGER,
                      tokens_cache_read INTEGER,
                      tokens_cache_write INTEGER,
                      metadata TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
                )
                conn.execute(
                    "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)"
                )
                conn.execute(
                    "CREATE TABLE session_input (id TEXT PRIMARY KEY, session_id TEXT, prompt TEXT, delivery TEXT, admitted_seq INTEGER, promoted_seq INTEGER, time_created INTEGER)"
                )
                conn.execute(
                    "CREATE TABLE event (id TEXT PRIMARY KEY, aggregate_id TEXT, seq INTEGER, type TEXT, data TEXT)"
                )
                conn.execute(
                    """
                    INSERT INTO session VALUES (
                      'ses_1', 'project', NULL, 'slug', '/tmp', 'BixBench q2',
                      '1.0', NULL, 0, 0, 0, NULL, NULL, NULL, 1, 2, NULL, NULL, NULL,
                      NULL, NULL, NULL, 0.0, 0, 0, 0, 0, 0, NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (
                        "msg_1",
                        "ses_1",
                        1,
                        1,
                        json.dumps({"role": "user", "time": {"created": 1}, "agent": "gleskit"}),
                    ),
                )
                conn.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (
                        "msg_2",
                        "ses_1",
                        2,
                        2,
                        json.dumps({"role": "assistant", "time": {"created": 2, "completed": 3}, "agent": "gleskit"}),
                    ),
                )
                conn.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (
                        "msg_3",
                        "ses_1",
                        3,
                        4,
                        json.dumps({"role": "assistant", "time": {"created": 4}, "agent": "gleskit"}),
                    ),
                )
                conn.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "prt_1",
                        "msg_1",
                        "ses_1",
                        1,
                        1,
                        json.dumps({"type": "text", "text": "Proceed statelessly."}),
                    ),
                )
                conn.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "prt_2",
                        "msg_2",
                        "ses_1",
                        2,
                        2,
                        json.dumps(
                            {
                                "type": "tool",
                                "tool": "resolve_gene",
                                "callID": "call_1",
                                "state": {"status": "running", "input": {"query": "CLOCK"}},
                            }
                        ),
                    ),
                )
                conn.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "prt_3",
                        "msg_2",
                        "ses_1",
                        3,
                        3,
                        json.dumps({"type": "text", "text": '{"answer": 63, "evidence": ["tool"], "confidence": 0.9, "abstain": false}'}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            export = load_opencode_session_export(db_path, "ses_1")
            transcript = build_visible_transcript(export)
            stats = load_opencode_session_stats(db_path, "ses_1")

            self.assertEqual([item["role"] for item in transcript], ["user", "assistant"])
            self.assertIn("Proceed statelessly.", transcript[0]["text"])
            self.assertIn('"answer": 63', transcript[1]["text"])
            self.assertEqual(len(export["parts"]), 3)
            self.assertEqual(stats["assistant_message_count"], 2)
            self.assertEqual(stats["tool_part_count"], 1)
            self.assertEqual(stats["active_tool_count"], 1)
            self.assertEqual(stats["pending_provider_count"], 1)
            self.assertEqual(stats["pending_provider_started_at"], 4)
            self.assertEqual(opencode_activity_kind(stats), "mcp_tool")
            self.assertEqual(opencode_activity_kind({"active_tool_count": 0, "pending_provider_count": 1}), "provider")
            self.assertEqual(opencode_activity_kind({"active_tool_count": 0, "pending_provider_count": 0}), "idle")


if __name__ == "__main__":
    unittest.main()
