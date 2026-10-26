from __future__ import annotations

import ast
from pathlib import Path
import unittest


class MCPToolContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.mcp_source = cls.repo_root / "src/gleskit_mcp/mcp_server.py"
        cls.mcp_tree = ast.parse(cls.mcp_source.read_text(encoding="utf-8"))

    @classmethod
    def tool_function(cls, name: str) -> ast.FunctionDef:
        matches = [node for node in ast.walk(cls.mcp_tree) if isinstance(node, ast.FunctionDef) and node.name == name]
        if len(matches) != 1:
            raise AssertionError(f"Expected one MCP tool function named {name!r}, found {len(matches)}")
        return matches[0]

    @classmethod
    def tool_doc(cls, name: str) -> str:
        return ast.get_docstring(cls.tool_function(name)) or ""

    @classmethod
    def keyword_default(cls, name: str, argument: str) -> object:
        function = cls.tool_function(name)
        positional = list(function.args.posonlyargs) + list(function.args.args)
        defaults = [None] * (len(positional) - len(function.args.defaults)) + list(function.args.defaults)
        for item, default in zip(positional, defaults, strict=False):
            if item.arg == argument:
                return ast.literal_eval(default) if default is not None else None
        for item, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=False):
            if item.arg == argument:
                return ast.literal_eval(default) if default is not None else None
        raise AssertionError(f"Argument {argument!r} not found on {name!r}")

    def test_mcp_docstrings_explain_routing_and_parameter_contracts(self) -> None:
        inspection = self.tool_doc("inspect_tabular_data")
        self.assertIn("candidate_group_levels", inspection)
        self.assertIn("authoritative strings", inspection)

        preparation = self.tool_doc("prepare_rnaseq_inputs")
        self.assertIn("prepared_manifest_path", preparation)
        self.assertIn("sample_key_column", preparation)

        deseq = self.tool_doc("analyze_deseq2_counts")
        self.assertIn('significance_mode="p_value"', deseq)
        self.assertIn("condition_b=\"DMSO\"", deseq)
        self.assertIn("log2_fold_change_threshold=0.0", deseq)
        self.assertIn("derived_inputs", deseq)

        statistics = self.tool_doc("run_expression_statistics")
        self.assertIn("flattened_values", statistics)
        self.assertIn("featurewise", statistics)
        self.assertIn("pairwise_log2_fold_change", statistics)
        self.assertIn("matrix_log2_fold_change_distribution", statistics)
        self.assertIn("sample_group_map", statistics)
        self.assertIn("threshold_count", statistics)

        stratified = self.tool_doc("analyze_stratified_deseq2")
        self.assertIn("target_strata", stratified)
        self.assertIn("protein-coding", stratified)

        enrichment = self.tool_doc("enrich_gene_set")
        self.assertIn('backend="enrichr"', enrichment)
        self.assertIn("does not silently fall back", enrichment)
        self.assertIn("top_term", enrichment)

    def test_analysis_tool_defaults_do_not_invent_an_absolute_lfc_filter(self) -> None:
        self.assertEqual(self.keyword_default("analyze_deseq2_counts", "log2_fold_change_threshold"), 0.0)
        self.assertEqual(self.keyword_default("analyze_rnaseq_result", "log2_fold_change_threshold"), 0.0)

    def test_persona_docs_repeat_exact_label_and_provider_rules(self) -> None:
        paths = [
            self.repo_root / "docs/opencode/prompts/agents/gleskit-agent-prompt.md",
            self.repo_root / "docs/opencode/prompts/agents/bixbench-eval-agent-prompt.md",
            self.repo_root / "docs/opencode/prompts/agents/repobench-eval-agent-prompt.md",
            self.repo_root / "docs/opencode/prompts/eval/bixbench-eval-prompt.md",
            self.repo_root / "docs/opencode/prompts/eval/repobench-eval-prompt.md",
            self.repo_root / "docs/opencode/skills/gleskit-research-assistant/references/12_tool_call_contract.md",
            self.repo_root / "docs/opencode/skills/gleskit-bixbench-eval/references/12_tool_call_contract.md",
            self.repo_root / "docs/opencode/skills/gleskit-repobench-eval/references/12_tool_call_contract.md",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertIn("DMSO", text, path.as_posix())
            self.assertIn("log2_fold_change_threshold=0.0", text, path.as_posix())
        self.assertIn('backend="enrichr"', (paths[1]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
