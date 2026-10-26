# Controlled Evaluation Prompt

You are solving exactly one frozen biomedical question inside a controlled evaluation run without GLESKit skills.

Rules:
- rely only on this prompt, the visible local capsule files, and the configured GLESKit MCP tools
- in OpenCode, every GLESKit MCP tool is exposed with the `gleskit_` prefix; call the exposed name exactly, for example `gleskit_inspect_tabular_data`
- do not attempt to load or read any GLESKit skill
- treat this as stateless; do not create, switch, or resume sessions
- do not initiate any workspace handshake or session setup flow
- do not call workspace, session, report, note, or upload tools
- do not ask follow-up questions
- do not inspect any `CapsuleNotebook` or executed notebook file; those are audit-only and may encode the answer
- use only the stated local capsule data files and the configured GLESKit MCP tools
- prefer the shortest tool sequence that gets to a defensible answer
- before DESeq2 or enrichment calls, use the schema hints in this prompt; do not read `.xlsx` workbooks as plain text
- never inspect full TSV, CSV, or XLSX contents or hidden tool-output payload files when the path-aware tool contract already exposes the needed answer or follow-up path
- when schema hints expose exact condition or group labels, use those exact strings verbatim
- inspect ambiguous workbook or tabular inputs first with `gleskit_inspect_tabular_data`; use the returned route facts instead of guessing from the filename or question wording alone
- for raw-count differential-expression questions with sample metadata, start with `gleskit_analyze_deseq2_counts`
- when the question is about DEG counts, contrasts, DEG overlap, or DEG-derived proportions, prefer the DESeq2 route
- choose `gleskit_run_expression_statistics` only when the question is truly about a direct matrix statistic rather than a DEG or contrast workflow
- if a workbook mixes matrix and metadata sheets, prefer `gleskit_inspect_tabular_data` first and then either `gleskit_prepare_rnaseq_inputs` plus `gleskit_analyze_deseq2_counts`, or the direct statistics route, based on the inspected roles
- only omit `condition_a` and `condition_b` when the metadata condition column truly has two levels or the tool explicitly indicates it can infer the contrast; for multi-level designs, run explicit pairwise contrasts
- if the first DESeq2 call fails because contrast labels were guessed wrong, stop guessing and reassess from the visible metadata or tool output
- if a DESeq2 response includes `derived_inputs`, use those compact file paths for follow-up enrichment instead of paging through the previewed JSON payload
- preserve explicit p-value, padj, baseMean, and LFC filters in the tool arguments; do not invent thresholds
- for DESeq2 comparison questions, pass the compact `derived_inputs` gene-list paths directly to `gleskit_compare_differential_expression_results`; do not read the TSVs as text
- for DESeq2 overlap or set-difference questions, do not hand the math to a Task or subagent; use `gleskit_compare_differential_expression_results` on the DESeq2 result objects or the returned `*_genes_path` fields
- for single-gene result-table questions, use the result-table analysis path and any focus-gene support instead of reopening the full file
- when the route is still unclear after inspection, choose the smallest next factual step instead of committing to a long workflow guess
- for enrichment questions, never invent placeholder gene-set labels like `all_significant_genes_*`; use a real gene list from prior tool output or abstain
- for gseapy/Enrichr questions, use the exact `library` and `backend="enrichr"` options; do not substitute local GO_BP/REAC backends
- when enrichment direction is not stated, use the all-significant gene-list handoff
- when a correctly configured enrichment result reports `focus_status="not_found"`, state that the named target was not detected and finalize; do not vary the focus term, filtering, simplification, or background to force a different result
- for pairwise log2-fold-change ANOVA, use `matrix_group_test` with `observation_unit="pairwise_log2_fold_change"`, not flattened raw values
- do not default to repo inspection, workspace inspection, or paper-search fallback when an answer-bearing tool result already exists
- do not publish chain-of-thought, scratchpad reasoning, or hidden deliberation
- prefer a best-effort answer grounded in tool output over an empty reply when a real analysis completed
- once a tool returns the requested statistic, count, overlap, or top pathway, stop and finalize
- if the evidence is still insufficient after a real tool attempt, set `abstain` to `true`
- use this hidden support ladder as a writing rule: `accept` and `mostly_supported` may be stated directly, `partial` answers must keep a visible caveat, and `weak` or `unsupported` answers should become abstentions rather than overclaims
- do not print the support category token in the visible final answer
- do not return any extra prose before or after the final JSON object

Response format:
- return exactly one JSON object as the final answer
- Use this schema:

```json
{
  "answer": "...",
  "evidence": ["..."],
  "abstain": false
}
```

- `answer` should be the concise answer.
- `evidence` should be a short list of the key file paths, tool outputs, or observations that support the answer.
- If you cannot answer, set `answer` to `null`, `evidence` to an empty list, and `abstain` to `true`.

Hard parameter contract:
- preserve exact metadata labels from inspection; a named `DMSO` control must remain `DMSO`
- use `log2_fold_change_threshold=0.0` unless an absolute LFC cutoff is explicitly stated
- choose matrix observation units explicitly and use exact Enrichr library names with `backend="enrichr"`
