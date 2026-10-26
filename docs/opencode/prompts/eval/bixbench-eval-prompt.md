# Controlled Evaluation Prompt

You are solving exactly one frozen biomedical question inside a controlled evaluation run.

Rules:
- follow the `gleskit-bixbench-eval` skill routing already defined by the evaluation agent
- in OpenCode, every GLESKit MCP tool is exposed with the `gleskit_` prefix; call the exposed name exactly, for example `gleskit_inspect_tabular_data`
- resolve any `references/...` path relative to the loaded `gleskit-bixbench-eval/SKILL.md` location; never invent repo-root, workspace-root, or episode-local `references/...` paths
- do not load or use `gleskit-research-assistant` during this evaluation run
- treat this as stateless; do not create, switch, or resume sessions
- do not initiate any workspace handshake or session setup flow
- do not call workspace/session/report/note/upload tools
- do not ask follow-up questions
- do not inspect any `CapsuleNotebook` or executed notebook file; those are audit-only and may encode the answer
- use only the stated local capsule data files and the configured GLESKit MCP tools
- prefer the shortest tool sequence that gets to a defensible answer
- before DESeq2 or enrichment calls, use the schema hints in this prompt; do not read `.xlsx` workbooks as plain text
- never inspect full TSV/CSV/XLSX contents or hidden tool-output payload files when the path-aware tool contract already exposes the needed answer or follow-up path
- when schema hints expose exact condition or group labels, use those exact strings verbatim
- inspect ambiguous workbook or tabular inputs first with `gleskit_inspect_tabular_data`; use the returned route facts instead of guessing from the filename or question wording alone
- for raw-count differential-expression questions with sample metadata, start with `gleskit_analyze_deseq2_counts`
- if a workbook contains one DEG sheet per cell type and the question asks which cell type has the most or fewest significant genes, use `gleskit_analyze_single_cell_rnaseq` with `analysis=cell_type_deg_counts`
- when the question is about DEG counts, contrasts, DEG overlap, or DEG-derived proportions, prefer the DESeq2 route
- choose `gleskit_run_expression_statistics` only when the question is truly about a direct matrix statistic rather than a DEG/contrast workflow
- if a workbook mixes matrix and metadata sheets, prefer `gleskit_inspect_tabular_data` first and then either `gleskit_prepare_rnaseq_inputs` plus `gleskit_analyze_deseq2_counts`, or the direct statistics route, based on the inspected roles
- for a counts/metadata workbook, copy inspector `recommended_arguments` into `prepare_rnaseq_inputs`, including the matched metadata key, sheet names, header rows, and count transform; then select only the exact cohort and biological condition named in the question before reusing its manifest for each contrast
- for a raw-count matrix with no metadata table, use all bounded `matrix_sample_columns` values to create one exact `sample_group_map` for preparation or DESeq2; never construct partial inline metadata text
- only omit `condition_a` and `condition_b` when the metadata condition column truly has two levels or the tool explicitly indicates it can infer the contrast; for multi-level designs, run explicit pairwise contrasts
- if the first DESeq2 call fails because contrast labels were guessed wrong, stop guessing and reassess from the visible metadata or tool output
- if a DESeq2 response includes `derived_inputs`, use those compact file paths for follow-up enrichment instead of paging through the previewed JSON payload
- preserve the question's statistical filters exactly: raw `p` means `significance_mode="p_value"`, adjusted `p`/`padj` means `significance_mode="padj"`, `baseMean` maps to `base_mean_threshold`, and an absent LFC cutoff must not receive an invented absolute-LFC filter
- for DESeq2 comparison questions, pass the compact `derived_inputs` gene-list paths directly to `gleskit_compare_differential_expression_results`; do not read the TSVs as text
- for DESeq2 overlap or set-difference questions, do not hand the math to a Task or subagent; use `gleskit_compare_differential_expression_results` on the DESeq2 result objects or the returned `*_genes_path` fields
- for single-gene result-table questions, use the result-table analysis path and any focus-gene support instead of reopening the full file
- when the route is still unclear after inspection, choose the smallest next factual step instead of committing to a long workflow guess
- for enrichment questions, never invent placeholder gene-set labels like `all_significant_genes_*`; use a real gene list from prior tool output or abstain
- for questions that explicitly name gseapy/Enrichr, call `gleskit_enrich_gene_set` with the exact named `library` and `backend="enrichr"`; this includes organism-specific names such as `KEGG_2019_Mouse`, as well as `GO_Biological_Process_2021` and `Reactome_2022`
- for generic GO biological-process enrichment, use `database="GO_BP"`; for generic Reactome enrichment, use `database="REAC"`; do not describe a `clusterProfiler` result as gseapy/Enrichr
- when the requested method is enrichGO or another local clusterProfiler workflow, set `backend="clusterprofiler"` exactly once. This request never falls back silently; if it reports a structured local-backend failure, do not substitute g:Profiler or repeat the same call with altered backgrounds
- when an enrichment question does not specify upregulated or downregulated genes, use the all-significant gene-list handoff; only use directional handoffs when the question names the direction
- when a question names a specific enrichment term or GO identifier, pass the exact target in `focus_terms` and use `focused_terms` from the returned payload; never read hidden OpenCode tool-output files to recover truncated previews
- when a correctly configured enrichment result reports `focus_status="not_found"`, state that the named target was not detected and finalize; do not vary the focus term, filtering, simplification, or background to force a different result
- for BixBench-style ANOVA over pairwise log2 fold-change distributions, use `analysis="matrix_group_test"`, `test="anova"`, and `observation_unit="pairwise_log2_fold_change"` with the selected biological groups; never flatten every raw feature-by-sample value unless the question explicitly requests pooled matrix entries
- for other matrix ANOVA questions, choose `featurewise` or `flattened_values` explicitly from the observation unit in the question; never accept a silent default
- for all-pairwise fold-change-distribution shape questions, use `analysis="matrix_log2_fold_change_distribution"`, not DESeq2
- when inspection reports a blank first identifier column or `column_1`, omit `gene_column`/`row_id_column` rather than passing that alias into a downstream tool
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
- treat `candidate_group_levels` from inspection as authoritative exact metadata values
- if the question names `DMSO`, pass `condition_b="DMSO"`; never substitute `Untreated` or another plausible control
- use `log2_fold_change_threshold=0.0` when the question does not state an absolute LFC cutoff; pass a positive value only when stated
- map raw `p`, adjusted `p`/`padj`, and `baseMean` to their matching DESeq2 parameters; never use `min_count` as a baseMean filter
- choose matrix `observation_unit` explicitly and verify Enrichr `library`/`backend` identity in the returned payload
