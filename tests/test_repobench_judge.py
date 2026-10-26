from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gleskit_mcp.eval.repobench_judge import (
    EvidenceSupportAssessment,
    RepoBenchJudgeConfig,
    _arbiter_unavailable_result,
    _arbiter_task_context,
    _compute_repobench_committee_result,
    _context_text,
    _extract_visible_paper_title,
    _offline_provenance_audit,
    _sources_lines,
    _verify_report_freeze,
    _weighted_level_mean,
    recompute_repobench_scores,
    write_human_audit_package,
)
from gleskit_mcp.eval.bixbench import sha256_text, write_json_atomic, write_text_atomic
from gleskit_mcp.eval.judge_bands import judge_mean_level_category
from gleskit_mcp.eval.scoring import visible_sources_summary
from gleskit_mcp.store import WorkspaceStore


class RepoBenchJudgeTests(unittest.TestCase):
    def test_judge_context_preserves_the_complete_saved_report(self) -> None:
        report_text = "opening\n" + ("evidence " * 4000) + "\nfinal validated section"

        context = _context_text({"report_saved": True, "report": {"body_markdown": report_text}})
        payload = json.loads(context)

        self.assertEqual(payload["saved_report"], report_text)
        self.assertTrue(payload["saved_report"].endswith("final validated section"))

    def test_title_parser_ignores_descriptive_prose_and_pmc_labels(self) -> None:
        descriptive = (
            'Gao et al. (2026), review describing ISGs as "context-dependent immune regulators" '
            '[Europe PMC](https://europepmc.org/article/MED/41859060).'
        )
        pmc_label = "Kawczak et al. describes AXL signaling ([PMC12525067](https://europepmc.org/article/MED/41095933))."
        explicit_title = (
            'Ruan et al. 2025 "APOE-TREM2 signaling axis" '
            '[Europe PMC](https://europepmc.org/article/MED/41363028).'
        )

        self.assertIsNone(_extract_visible_paper_title(descriptive))
        self.assertIsNone(_extract_visible_paper_title(pmc_label))
        self.assertEqual(_extract_visible_paper_title(explicit_title), "APOE-TREM2 signaling axis")

    def test_markdown_emphasized_sources_lines_are_reader_facing_sources(self) -> None:
        report_text = (
            "Claim one.\n\n"
            "**Sources:** [Reactome](https://reactome.org).\n\n"
            "Claim two.\n\n"
            "- **Sources:** [Europe PMC](https://europepmc.org/article/MED/1).\n"
        )

        summary = visible_sources_summary(report_text)
        sources = _sources_lines(report_text)

        self.assertEqual(summary["source_line_count"], 2)
        self.assertEqual(summary["provider_source_line_count"], 2)
        self.assertEqual(sources[0][1], "Sources: [Reactome](https://reactome.org).")
        self.assertEqual(sources[1][1], "Sources: [Europe PMC](https://europepmc.org/article/MED/1).")

    def test_markdown_sources_header_collects_following_bullets(self) -> None:
        report_text = (
            "Claim one.\n\n"
            "**Sources:**\n"
            "- Reactome pathway analysis [Reactome](https://reactome.org).\n"
            "- Europe PMC evidence [Study](https://europepmc.org/article/MED/1).\n\n"
            "## Next section\n"
        )

        summary = visible_sources_summary(report_text)
        sources = _sources_lines(report_text)

        self.assertEqual(summary["source_line_count"], 1)
        self.assertEqual(summary["provider_source_line_count"], 1)
        self.assertIn("Europe PMC evidence", sources[0][1])

    def test_evidence_specialist_schema_bounds_list_outputs(self) -> None:
        schema = EvidenceSupportAssessment.model_json_schema()

        self.assertEqual(schema["properties"]["supported_claims"]["maxItems"], 6)
        self.assertEqual(schema["properties"]["unsupported_claims"]["maxItems"], 6)
        self.assertEqual(schema["properties"]["evidence_gaps"]["maxItems"], 6)

    def test_arbiter_unavailable_keeps_specialist_score_and_requires_review(self) -> None:
        committee_result = {
            "committee_level_mean_final": 2.9,
            "committee_level_mean_normalized": 0.725,
            "review_required": False,
            "review_reasons": [],
        }

        verdict, updated = _arbiter_unavailable_result(committee_result, ValueError("provider 504"))

        self.assertEqual(updated["committee_level_mean_final"], 2.9)
        self.assertEqual(updated["committee_level_mean_normalized"], 0.725)
        self.assertTrue(updated["review_required"])
        self.assertIn("arbiter_unavailable", updated["review_reasons"])
        self.assertEqual(verdict["status"], "unavailable")

    def test_arbiter_context_excludes_report_body(self) -> None:
        context = _arbiter_task_context(
            {
                "task_id": "demo-task",
                "report_type": "mechanism",
                "title": "Demo",
                "report_saved": True,
                "report": {"body_markdown": "This must not be repeated to the arbiter."},
            }
        )

        self.assertIn("demo-task", context)
        self.assertNotIn("must not be repeated", context)

    def test_report_freeze_verification_detects_post_capture_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            episode_root = Path(tmpdir) / "episode"
            episode_root.mkdir(parents=True)
            report_text = "## Saved report\n\nSources: [Example](https://example.org)\n"
            write_text_atomic(episode_root / "report.md", report_text)
            record = {
                "report": {"body_markdown": report_text},
                "report_freeze": {"content_sha256": sha256_text(report_text)},
            }

            verified = _verify_report_freeze(episode_root, record)
            self.assertEqual(verified["status"], "verified")

            write_text_atomic(episode_root / "report.md", "Changed after capture\n")
            changed = _verify_report_freeze(episode_root, record)
            self.assertEqual(changed["status"], "report_file_mismatch")

    def test_human_audit_package_is_blinded_and_non_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            summary_root = Path(tmpdir) / "summary"
            report_text = (
                "## Findings\n\n"
                "CXCL10 is a candidate that needs validation.\n\n"
                "Sources: [Example study](https://example.org/paper).\n"
            )
            records = [
                {
                    "task_id": "biomarker-demo",
                    "report_type": "biomarker",
                    "title": "Demo report",
                    "task_prompt": "Assess the panel as a practical reporting task.",
                    "report_saved": True,
                    "report": {"body_markdown": report_text},
                    "skills_mode": "on",
                    "judge": {"committee_level_mean_final": 4.0},
                    "expected_nuggets": ["This must remain evaluator-only."],
                }
            ]

            result = write_human_audit_package(summary_root, records)
            report_path = Path(result["root"]) / "reports" / "R01.md"
            audit_csv = Path(result["claim_citation_audit"])

            self.assertEqual(result["report_count"], 1)
            self.assertTrue(report_path.exists())
            self.assertTrue(audit_csv.exists())
            self.assertNotIn("committee_level_mean_final", report_path.read_text(encoding="utf-8"))
            self.assertNotIn("evaluator-only", report_path.read_text(encoding="utf-8"))
            self.assertIn("reported_link_url", audit_csv.read_text(encoding="utf-8"))

    def test_offline_provenance_audit_detects_paper_title_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_root = root / "run"
            workspace_root = run_root / "workspace"
            episode_root = run_root / "episodes" / "demo-task"
            episode_root.mkdir(parents=True, exist_ok=True)
            session_store = WorkspaceStore(workspace_root)
            session = session_store.create_session(
                title="Demo",
                description="Demo session",
                species="human",
            )
            paper_artifact = session_store.create_artifact(
                session.session_id,
                kind="paper_search",
                title="demo literature",
                summary="demo",
                provider="europe_pmc",
                payload={
                    "papers": [
                        {
                            "pmid": "41363028",
                            "doi": "10.1002/cns.70689",
                            "title": "Decoding the Therapeutic Effects of Acupuncture in Hemorrhagic Stroke Using Single-Cell RNA Sequencing.",
                            "url": "https://europepmc.org/article/MED/41363028",
                        }
                    ]
                },
            )
            report_payload = {
                "session_id": session.session_id,
                "linked_artifact_ids": [paper_artifact.artifact_id],
                "body_markdown": (
                    "## Supporting Molecular Findings\n\n"
                    'Sources: Ruan et al. 2025 "APOE-TREM2 signaling axis" '
                    "[Europe PMC](https://europepmc.org/article/MED/41363028).\n"
                ),
            }
            record = {"report": report_payload}
            audit = _offline_provenance_audit(run_root, record)

            self.assertEqual(audit["status"], "ok")
            self.assertIn("citation_title_mismatch", audit["issue_codes"])
            self.assertEqual(audit["major_issue_count"], 1)
            self.assertEqual(audit["paper_checks"][0]["pmid"], "41363028")
            self.assertFalse(audit["paper_checks"][0]["matched"])

    def test_resolved_model_map_strips_provider_prefixes_for_openai(self) -> None:
        config = RepoBenchJudgeConfig(
            run_root=Path("/tmp/demo-run"),
            provider="openai",
            model="openai/gpt-4.1-mini",
            provenance_model="openrouter/deepseek/deepseek-v4-flash",
        )

        self.assertEqual(config.model_map["biomedical_content_specialist"], "openai/gpt-4.1-mini")
        self.assertEqual(config.resolved_model_map["biomedical_content_specialist"], "gpt-4.1-mini")
        self.assertEqual(config.resolved_model_map["provenance_quality_specialist"], "deepseek/deepseek-v4-flash")

    def test_resolved_model_map_keeps_openrouter_slugs(self) -> None:
        config = RepoBenchJudgeConfig(
            run_root=Path("/tmp/demo-run"),
            provider="openrouter",
            model="deepseek/deepseek-v4-flash",
            arbiter_model="qwen/qwen3-next-80b-a3b-instruct:free",
        )

        self.assertEqual(config.resolved_model_map["biomedical_content_specialist"], "deepseek/deepseek-v4-flash")
        self.assertEqual(config.resolved_model_map["arbiter"], "qwen/qwen3-next-80b-a3b-instruct:free")

    def test_weighted_level_mean_uses_fixed_specialist_weights(self) -> None:
        state = {
            "content_assessment": {"category": "accept"},
            "evidence_assessment": {"category": "partial"},
            "template_assessment": {"category": "weak"},
            "provenance_assessment": {"category": "unsupported"},
        }

        score = _weighted_level_mean(state)

        self.assertEqual(score, 2.2)

    def test_shared_category_midpoint_mapping_uses_ordinal_ladder(self) -> None:
        self.assertEqual((judge_mean_level_category(0.49) or {}).get("key"), "unsupported")
        self.assertEqual((judge_mean_level_category(0.50) or {}).get("key"), "weak")
        self.assertEqual((judge_mean_level_category(1.50) or {}).get("key"), "partial")
        self.assertEqual((judge_mean_level_category(2.50) or {}).get("key"), "mostly_supported")
        self.assertEqual((judge_mean_level_category(3.50) or {}).get("key"), "accept")

    def test_repobench_committee_result_preserves_weighted_mean_and_flags_weak_template(self) -> None:
        state = {
            "content_assessment": {"category": "accept"},
            "evidence_assessment": {"category": "accept", "failure_codes": [], "unsupported_claims": []},
            "template_assessment": {"category": "weak"},
            "provenance_assessment": {"category": "accept"},
        }

        result = _compute_repobench_committee_result(state)

        self.assertEqual(result["committee_level_mean_raw"], 3.4)
        self.assertEqual(result["committee_level_mean_final"], 3.4)
        self.assertEqual(result["committee_level_mean_normalized"], 0.85)
        self.assertEqual(result["committee_category"], "mostly_supported")
        self.assertFalse(result["cap_applied"])
        self.assertEqual(result["cap_reasons"], [])
        self.assertEqual(result["quality_flags"], ["template_quality_weak:weak"])
        self.assertTrue(result["review_required"])
        self.assertIn("quality_flag", result["review_reasons"])

    def test_repobench_committee_result_preserves_weighted_mean_and_flags_evidence_risk(self) -> None:
        state = {
            "content_assessment": {"category": "accept"},
            "evidence_assessment": {"category": "unsupported", "failure_codes": ["unsupported_claim"], "unsupported_claims": ["claim"]},
            "template_assessment": {"category": "accept"},
            "provenance_assessment": {"category": "weak"},
        }

        result = _compute_repobench_committee_result(state)

        self.assertEqual(result["committee_level_mean_raw"], 2.35)
        self.assertEqual(result["committee_level_mean_final"], 2.35)
        self.assertEqual(result["committee_level_mean_normalized"], 0.5875)
        self.assertEqual(result["committee_category"], "partial")
        self.assertEqual(result["quality_flags"], ["evidence_below_weighted_mean:unsupported"])
        self.assertEqual(result["specialist_disagreement_span"], 4)
        self.assertTrue(result["review_required"])
        self.assertIn("quality_flag", result["review_reasons"])
        self.assertIn("specialist_disagreement_span", result["review_reasons"])
        self.assertIn("material_evidence_failure", result["review_reasons"])

    def test_repobench_committee_result_preserves_weighted_mean_and_flags_provenance_issue(self) -> None:
        state = {
            "content_assessment": {"category": "accept"},
            "evidence_assessment": {"category": "accept", "failure_codes": [], "unsupported_claims": []},
            "template_assessment": {"category": "accept"},
            "provenance_assessment": {"category": "mostly_supported"},
            "offline_provenance_audit": {
                "status": "ok",
                "issue_codes": ["citation_title_mismatch"],
                "major_issue_count": 1,
            },
        }

        result = _compute_repobench_committee_result(state)

        self.assertEqual(result["committee_level_mean_raw"], 3.85)
        self.assertEqual(result["committee_level_mean_final"], 3.85)
        self.assertEqual(result["committee_level_mean_normalized"], 0.9625)
        self.assertEqual(result["committee_category"], "accept")
        self.assertEqual(result["quality_flags"], ["offline_provenance_major_issue"])
        self.assertIn("offline_provenance_audit", result["review_reasons"])
        self.assertEqual(result["offline_provenance_issue_codes"], ["citation_title_mismatch"])

    def test_recompute_repobench_scores_migrates_saved_specialists_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_root = Path(tmpdir) / "run"
            episode_root = run_root / "episodes" / "demo-task"
            episode_root.mkdir(parents=True)
            write_json_atomic(run_root / "run_manifest.json", {"run_id": "demo-run"})
            write_json_atomic(
                episode_root / "episode.json",
                {
                    "task_id": "demo-task",
                    "report_type": "biomarker",
                    "scope": 1,
                    "status": "completed",
                    "report_saved": True,
                    "report": {"body_markdown": "## Findings\n\nSources: none.\n"},
                },
            )
            write_json_atomic(
                episode_root / "judge.json",
                {
                    "judge_schema_version": 4,
                    "status": "scored",
                    "judge": {"review_reasons": ["cap_applied"]},
                    "offline_provenance_audit": {"status": "ok", "issue_codes": [], "major_issue_count": 0},
                    "specialists": {
                        "biomedical_content_specialist": {"category": "accept"},
                        "evidence_support_specialist": {"category": "mostly_supported", "failure_codes": [], "unsupported_claims": []},
                        "template_compliance_specialist": {"category": "accept"},
                        "provenance_quality_specialist": {"category": "accept"},
                    },
                },
            )

            result = recompute_repobench_scores(run_root)
            migrated = json.loads((episode_root / "judge.json").read_text(encoding="utf-8"))

            self.assertEqual(result["updated_count"], 1)
            self.assertEqual(migrated["judge_schema_version"], 6)
            self.assertEqual(migrated["committee_level_mean_raw"], 3.7)
            self.assertEqual(migrated["committee_level_mean_final"], 3.7)
            self.assertEqual(migrated["judge_score"], 0.925)
            self.assertEqual(migrated["score_policy"], "continuous_weighted_mean_exact_v1")
            self.assertFalse(migrated["cap_applied"])
            self.assertEqual(migrated["quality_flags"], ["evidence_below_weighted_mean:mostly_supported"])
            self.assertFalse(migrated["score_recomputation"]["provider_called"])
            self.assertTrue((run_root / "summary" / "judge" / "judge_metrics.json").exists())
