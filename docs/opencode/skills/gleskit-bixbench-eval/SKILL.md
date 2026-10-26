---
name: gleskit-bixbench-eval
description: Use for controlled single-question evaluation runs that must stay stateless, file-driven, and MCP-only. This skill is evaluation-only and must not inherit general workspace, report-writing, or literature-assistant behavior.
---

# GLESKit Controlled Evaluation Skill

This skill is the operating manual for controlled single-question evaluation runs.

Use it only for evaluation sessions where the agent must solve one frozen biomedical question from local files and GLESKit MCP tools.

Do not use this skill as the general user-facing GLESKit assistant.

## How To Use This Skill

1. Load this skill.
2. Read this `SKILL.md` from its concrete file path.
3. Resolve every `references/...` path relative to the exact directory that contains this `SKILL.md`.
4. Never resolve `references/...` relative to the repo root, the current working directory, or the episode directory.
5. Always read `references/00_eval_core_rules.md`.
6. Always read `references/12_tool_call_contract.md`.
7. For DEG, RNA-seq, result-table, workbook, enrichment, overlap, or statistics questions, also read `references/30_rnaseq_and_statistics.md`.
8. Read `references/40_finalization_and_abstention.md` before finalizing.

## Reference Router

| Reference file | Mandatory trigger |
|---|---|
| `references/00_eval_core_rules.md` | Every substantive evaluation turn. |
| `references/12_tool_call_contract.md` | Every GLESKit tool-backed analysis turn. |
| `references/30_rnaseq_and_statistics.md` | Any DEG, RNA-seq, result-table, workbook, pathway, overlap, or matrix/statistics question. |
| `references/40_finalization_and_abstention.md` | Every final answer turn and every insufficiency decision. |

## Scope

This skill exists to:

- solve one question per session
- stay stateless
- use local file paths and GLESKit MCP tools
- avoid unnecessary exploration
- finalize as soon as the answer-bearing tool output exists

This skill must never introduce:

- workspace/session onboarding behavior
- repo inspection behavior
- report-writing behavior
- literature-search fallback unless explicitly requested by the question
- hidden reasoning publication

## MCP Contract Precedence

The MCP tool schema and docstring are the first layer of truth; these references explain how the evaluation persona
must apply them. Preserve exact metadata labels from inspection, pass `0.0` when no LFC cutoff is stated, choose a
matrix observation unit explicitly, and preserve named Enrichr library/backend identity. Do not infer an answer from a
benchmark ideal or audit-only artifact.

## Analysis Routing Guardrails

- Inspect ambiguous workbooks first and treat the returned header row, identifier column, sample columns, metadata key,
  group column, confidence, and exact labels as authoritative.
- Route continuous, qPCR, and Ct matrices to matrix statistics with an explicit `sample_group_map` or verified mapping,
  use `missing_value_policy="omit"` when inspection reports missing assay measurements, and use `value_transform="log2"`
  only when requested. Never send continuous values to DESeq2.
- Route `.rds` differential-expression results directly to `analyze_rnaseq_result`; never inspect or decode them as text.
- For pathway-set comparisons after RDS analysis, pass each generated `all_significant_genes_path` to `enrich_gene_set` and compare returned significant term IDs. Use `database="KEGG"` for Pseudomonas `PA14_...` locus tags.
- Route raw counts through `prepare_rnaseq_inputs` and DESeq2; use `analyze_stratified_deseq2` for exact within-stratum
  contrasts and separate target/reference levels.
- Use `threshold_count` for direct result-column counts such as dispersion and use generated handoff paths for
  enrichment backgrounds. Stop after repeated non-retryable tool errors instead of guessing or bypassing GLESKit.
