from __future__ import annotations

from pathlib import Path
import unittest


class GLESKitSkillRoutingDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.prompt_path = self.repo_root / "docs/opencode/prompts/agents/gleskit-agent-prompt.md"
        self.skill_root = self.repo_root / "docs/opencode/skills/gleskit-research-assistant"

    def test_skill_and_prompt_point_chained_workflows_to_pipeline_reference(self) -> None:
        skill_text = (self.skill_root / "SKILL.md").read_text(encoding="utf-8")
        prompt_text = self.prompt_path.read_text(encoding="utf-8")

        self.assertIn("references/30_pipeline_mode.md", skill_text)
        self.assertIn("references/30_pipeline_mode.md", prompt_text)
        self.assertIn("references/12_tool_call_contract.md", skill_text)
        self.assertIn("references/12_tool_call_contract.md", prompt_text)
        self.assertNotIn("references/31_expression_matrix_mode.md", skill_text)
        self.assertNotIn("references/31_expression_matrix_mode.md", prompt_text)

    def test_rnaseq_and_uploads_guidance_mentions_pipeline_and_log_transform(self) -> None:
        rnaseq_text = (self.skill_root / "references/30_rnaseq_and_omics.md").read_text(encoding="utf-8")
        uploads_text = (self.skill_root / "references/11_uploads_mode.md").read_text(encoding="utf-8")

        self.assertIn("run_pipeline", rnaseq_text)
        self.assertIn("run_log_transform", rnaseq_text)
        self.assertIn("run_expression_statistics", rnaseq_text)
        self.assertIn("analyze_single_cell_rnaseq", rnaseq_text)
        self.assertIn("compare_differential_expression_results", rnaseq_text)
        self.assertIn("references/12_tool_call_contract.md", rnaseq_text)
        self.assertIn("upload_...", rnaseq_text)
        self.assertIn("do not replace the analysis with shell, Python, or R", rnaseq_text)
        self.assertIn("run_pipeline", uploads_text)
        self.assertIn("run_log_transform", uploads_text)
        self.assertIn("analyze_single_cell_rnaseq", uploads_text)
        self.assertIn("references/12_tool_call_contract.md", uploads_text)
        self.assertIn("upload_...", uploads_text)
        self.assertIn("Do not manually reconstruct workspace storage paths", uploads_text)

    def test_pipeline_docs_distinguish_bootstrap_consensus_from_train_test_clustering(self) -> None:
        pipeline_text = (self.skill_root / "references/30_pipeline_mode.md").read_text(encoding="utf-8")
        self.assertIn("cohort-wide bootstrap consensus route", pipeline_text)
        self.assertIn("one train/test split", pipeline_text)
        self.assertIn("logistic regression label prediction", pipeline_text)
        self.assertIn("test-only agreement", pipeline_text)
        self.assertIn("Do not place `accuracy` in `metric`", pipeline_text)
        self.assertIn("`pearson` is the same intent", pipeline_text)
        self.assertIn("compare_differential_expression_results", pipeline_text)
        self.assertIn("from_steps", pipeline_text)
        self.assertIn("analyze_single_cell_rnaseq", pipeline_text)
        self.assertIn("references/12_tool_call_contract.md", pipeline_text)
        self.assertIn("upload_...", pipeline_text)
        self.assertIn("do not continue the workflow with shell, Python, or R substitutions", pipeline_text)

    def test_workspace_docs_stay_persistence_only(self) -> None:
        workspace_text = (self.skill_root / "references/10_workspace_mode.md").read_text(encoding="utf-8")
        self.assertIn("persistence and saved-state actions only", workspace_text)
        self.assertIn("Do not route pure analysis requests here", workspace_text)

    def test_literature_guidance_requires_inline_traceable_citations(self) -> None:
        literature_text = (self.skill_root / "references/40_literature_and_provenance.md").read_text(encoding="utf-8")
        self.assertIn("include a short visible citation inline or immediately adjacent to the claim", literature_text)
        self.assertIn("Do not leave citations as bare identifiers when a link can be constructed", literature_text)

    def test_expression_matrix_stub_is_removed(self) -> None:
        stub_path = self.skill_root / "references/31_expression_matrix_mode.md"
        pipeline_text = (self.skill_root / "references/30_pipeline_mode.md").read_text(encoding="utf-8")
        matrix_appendix = (self.skill_root / "references/pipeline/10_matrix_mode.md").read_text(encoding="utf-8")

        self.assertFalse(stub_path.exists())
        self.assertIn("run_pipeline", pipeline_text)
        self.assertIn("gene_summary", pipeline_text)
        self.assertIn("references/pipeline/10_matrix_mode.md", pipeline_text)
        self.assertIn("do not switch to direct single-step MCP analysis tools mid-turn", pipeline_text)
        self.assertNotIn("prefer that direct tool", pipeline_text)
        self.assertIn('analysis_axis="columns"', matrix_appendix)
        self.assertIn("upload_...", matrix_appendix)
        self.assertIn("do not replace it with shell, Python, or R", matrix_appendix)

    def test_tool_call_contract_exists_and_forbids_local_reimplementation(self) -> None:
        contract_path = self.skill_root / "references/12_tool_call_contract.md"
        contract_text = contract_path.read_text(encoding="utf-8")

        self.assertTrue(contract_path.exists())
        self.assertIn("inspect_tabular_data", contract_text)
        self.assertIn("prepare_rnaseq_inputs", contract_text)
        self.assertIn("do not re-implement that analysis in shell, Python, or R", contract_text)
        self.assertIn("upload_...", contract_text)
        self.assertIn("artifact_...", contract_text)
        self.assertIn('analysis_axis="columns"', contract_text)
        self.assertIn("evaluate_cluster_agreement", contract_text)
        self.assertIn("accuracy", contract_text)
        self.assertIn("cohort-wide count across all observations", contract_text)
        self.assertIn("agreement.count_test_only", contract_text)

    def test_prompt_and_skill_allow_explicit_analysis_inputs_to_skip_opening_handshake(self) -> None:
        prompt_text = self.prompt_path.read_text(encoding="utf-8")
        skill_text = (self.skill_root / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("do not force a workspace/session handshake", prompt_text)
        self.assertIn("skip the handshake", prompt_text)
        self.assertIn("do not block the analysis on an opening handshake", skill_text)

    def test_prompt_and_shared_docs_expose_inspection_first_rna_routes(self) -> None:
        prompt_text = self.prompt_path.read_text(encoding="utf-8")
        rnaseq_text = (self.skill_root / "references/30_rnaseq_and_omics.md").read_text(encoding="utf-8")
        pipeline_text = (self.skill_root / "references/30_pipeline_mode.md").read_text(encoding="utf-8")

        self.assertIn("inspect_tabular_data", prompt_text)
        self.assertIn("prepare_rnaseq_inputs", prompt_text)
        self.assertIn("inspect_tabular_data", rnaseq_text)
        self.assertIn("prepare_rnaseq_inputs", rnaseq_text)
        self.assertIn("inspect_tabular_data", pipeline_text)
        self.assertIn("prepare_rnaseq_inputs", pipeline_text)

    def test_bixbench_eval_prompt_omits_confidence_and_uses_hidden_support_ladder(self) -> None:
        prompt_text = (self.repo_root / "docs/opencode/prompts/eval/bixbench-eval-prompt.md").read_text(encoding="utf-8")
        finalization_text = (
            self.repo_root / "docs/opencode/skills/gleskit-bixbench-eval/references/40_finalization_and_abstention.md"
        ).read_text(encoding="utf-8")

        self.assertNotIn('"confidence"', prompt_text)
        self.assertIn("hidden support ladder", prompt_text)
        self.assertIn("do not print the support category token", prompt_text)
        self.assertIn('focus_status="not_found"', prompt_text)
        self.assertIn('focus_status="not_found"', finalization_text)
        self.assertIn("`partial`", finalization_text)
        self.assertIn("`weak`", finalization_text)
        self.assertIn("abstain", finalization_text)

    def test_bixbench_eval_docs_cover_sheetwise_single_cell_deg_workbooks(self) -> None:
        prompt_text = (self.repo_root / "docs/opencode/prompts/eval/bixbench-eval-prompt.md").read_text(encoding="utf-8")
        contract_text = (
            self.repo_root / "docs/opencode/skills/gleskit-bixbench-eval/references/12_tool_call_contract.md"
        ).read_text(encoding="utf-8")
        rnaseq_text = (
            self.repo_root / "docs/opencode/skills/gleskit-bixbench-eval/references/30_rnaseq_and_statistics.md"
        ).read_text(encoding="utf-8")

        self.assertIn("analysis=cell_type_deg_counts", prompt_text)
        self.assertIn("analysis=cell_type_deg_counts", contract_text)
        self.assertIn("Single-Cell DEG Workbook Questions", rnaseq_text)
        self.assertIn("do not loop sheet-by-sheet", rnaseq_text)

    def test_repobench_eval_docs_require_evidence_grounded_claims(self) -> None:
        prompt_text = (self.repo_root / "docs/opencode/prompts/eval/repobench-eval-prompt.md").read_text(encoding="utf-8")
        core_rules_text = (
            self.repo_root / "docs/opencode/skills/gleskit-repobench-eval/references/00_core_rules.md"
        ).read_text(encoding="utf-8")
        reports_text = (
            self.repo_root / "docs/opencode/skills/gleskit-repobench-eval/references/60_reports_and_notes.md"
        ).read_text(encoding="utf-8")

        expected_rule = "otherwise remove the claim or label it as a caveat, interpretation, or recommended validation"
        self.assertIn("State factual claims directly only when supported by retrieved evidence", prompt_text)
        self.assertIn(expected_rule, prompt_text.lower())
        self.assertIn(expected_rule, core_rules_text.lower())
        self.assertIn(expected_rule, reports_text.lower())
        self.assertNotIn("hidden support ladder", "\n".join([prompt_text, core_rules_text, reports_text]).lower())


if __name__ == "__main__":
    unittest.main()
