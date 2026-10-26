---
name: gleskit-repobench-eval
description: Use for controlled single-task RepoBench evaluation runs that must stay stateless, use the provided report session, and finish only after a real saved report artifact exists.
---

# GLESKit RepoBench Controlled Evaluation Skill

This skill is the operating manual for controlled single-task RepoBench evaluation runs.

Use it only for report-centric evaluation sessions where the agent must solve one frozen biomedical task and save a real report artifact through GLESKit report tools.

Do not use this skill as the general user-facing GLESKit assistant.

## How To Use This Skill

1. Load this skill.
2. Read this `SKILL.md` from its concrete file path.
3. Resolve every `references/...` path relative to the exact directory that contains this `SKILL.md`.
4. Never resolve `references/...` relative to the repo root, the current working directory, or the episode directory.
5. Always read `references/00_core_rules.md`.
6. Always read `references/12_tool_call_contract.md`.
7. For gene-panel interpretation or evidence gathering, read `references/20_gene_sets_and_signatures.md`.
8. For evidence grounding and provenance, read `references/40_literature_and_provenance.md`.
9. For report structure, template use, and save/verify behavior, read `references/60_reports_and_notes.md`.

## Reference Router

| Reference file | Mandatory trigger |
|---|---|
| `references/00_core_rules.md` | Every substantive RepoBench turn. |
| `references/12_tool_call_contract.md` | Every GLESKit tool-backed analysis turn. |
| `references/20_gene_sets_and_signatures.md` | Any gene-panel, biomarker, mechanism, or disease-context synthesis task. |
| `references/40_literature_and_provenance.md` | Any evidence-gathering, attribution, or claim-grounding turn. |
| `references/60_reports_and_notes.md` | Any template load, report authoring, report save, or report verification turn. |

## MCP Contract Precedence

The MCP tool schema and docstring are the first layer of truth. When a RepoBench task uses analysis tools, preserve
exact metadata labels, reuse returned manifests and derived paths, avoid inventing LFC filters, choose matrix
observation units explicitly, and preserve the actual provider/library identity in the saved report.

## Analysis Routing Guardrails

- Inspect ambiguous workbooks first and preserve the returned header row, identifier/sample columns, metadata key,
  group column, confidence, and exact metadata labels in subsequent tool calls.
- Route qPCR/Ct/continuous matrices to matrix statistics with explicit groups and transformations; route raw counts to
  input preparation plus DESeq2, and use `analyze_stratified_deseq2` for composite strata. Choose its explicit
  focus-gene, adjusted-count, or annotation-group endpoint for those requested outputs.
- Use `threshold_count` for direct dispersion/result-column counts, reuse only generated handoff paths, and stop after
  repeated non-retryable schema/data errors rather than guessing or using shell, Python, or direct file reads.
