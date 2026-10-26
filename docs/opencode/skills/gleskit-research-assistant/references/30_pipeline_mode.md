# Pipeline Mode

Use this file for any chained workflow where one GLESKit tool should feed the next.

Before assembling the pipeline call, also read `references/12_tool_call_contract.md`.

## Canonical Route

- `gleskit_run_pipeline` is the canonical entrypoint for multi-step workflows.
- For ambiguous workbook or tabular sources, inspect first with `gleskit_inspect_tabular_data` before you assemble the pipeline.
- For DESeq2-style work where counts and metadata need explicit sanitization or filtering, use `gleskit_prepare_rnaseq_inputs` before `gleskit_analyze_deseq2_counts`.
- Use direct single-step tools only outside `/pipeline` turns when the user explicitly wants one step only.
- Use `gleskit_run_log_transform` when the user explicitly wants a preprocessing log transform.
- `gleskit_run_log_transform` accepts explicit presets only: `custom`, `log2`, `log2p1`, `log10`, `log10p1`, `ln`, and `lnp1`.
- Use `custom` when the user wants to supply base/offset manually; the public result is a JSON-safe summary plus artifact metadata, including `derived_inputs.matrix_path` for reuse in later matrix steps, not the in-memory matrix object.
- `run_expression_pca` expects the selected observation axis to look like the sample axis; if the chosen observation axis is larger than the feature axis, switch `analysis_axis` or set `force=true` only when that orientation is intentional.
- `run_expression_consensus_clustering` is the cohort-wide bootstrap consensus route; it returns stability summaries across all observations, but it does not implement held-out logistic label transfer.
- `evaluate_cluster_agreement` is the held-out route for one train/test split; it returns test-only agreement after logistic regression label prediction and should not be treated as a cohort-wide all-sample count.
- For clustering tools, `metric` means clustering distance, not the downstream agreement score. Prefer `correlation` for gene-expression matrices. `pearson` is the same intent and will normalize to correlation distance. Do not place `accuracy` in `metric`.
- For path-aware pipeline inputs, pass the original absolute path, `upload_...`, or reusable `artifact_...` selector directly.
- Keep pipeline execution linear in v1: one source, an ordered list of stages, and each stage feeding the next.
- Supported stage tools include `gleskit_run_log_transform`, `gleskit_run_expression_pca`, `gleskit_run_expression_hierarchical_clustering`, `gleskit_run_expression_correlation`, `gleskit_run_expression_consensus_clustering`, `gleskit_run_supervised_expression_model`, `gleskit_evaluate_cluster_agreement`, `gleskit_run_expression_statistics`, `gleskit_analyze_single_cell_rnaseq`, `gleskit_gene_summary`, `gleskit_interaction_network`, `gleskit_pathway_context`, `gleskit_gene_disease_associations`, `gleskit_investigate_gene_set`, `gleskit_analyze_rnaseq_result`, `gleskit_analyze_deseq2_counts`, `gleskit_compare_differential_expression_results`, `gleskit_enrich_gene_set`, `gleskit_ranked_gene_set_gsea`, `gleskit_gene_set_disease_context`, and `gleskit_search_papers`.

## Turn Directives

- A user message that begins with `/pipeline` is an explicit hard signal to use pipeline mode for that turn.
- `/pipeline` overrides the usual opening workspace/session handshake. Do not ask whether to continue statelessly or connect a session before you start the pipeline unless a required input is genuinely missing.
- When `/pipeline` is present, do not spend the turn asking whether the user really wants a plan.
- Instead, produce a short plan, assemble every needed stage into one `run_pipeline` call, and do not call `run_pipeline` more than once in that turn.
- In `/pipeline` turns, do not switch to direct single-step MCP analysis tools mid-turn; keep every analysis stage inside the single `run_pipeline` call for that turn.
- After the pipeline result is returned, stop tool-calling and answer the user from that result unless a genuinely separate non-pipeline tool is still required.
- The user may optionally add `/data <id> <id> ...` before the question text.
- Treat those ids as authoritative input data references for the turn.
- Accept technical references such as session upload ids, artifact ids, cohort ids, and session ids when they are explicitly supplied.

## Named Modes

### Matrix mode

- source: gene-level expression matrix or raw counts table already represented as a matrix
- common sequence: `gleskit_run_log_transform -> gleskit_run_expression_pca` / clustering / correlation
- reuse the transformed matrix through `derived_inputs.matrix_path` or the saved `artifact_...` selector rather than reusing the raw matrix path by hand
- matrix-specific orientation and step-order rules live in `references/pipeline/10_matrix_mode.md`

### DESeq2 mode

- source: raw counts plus sample metadata
- common sequence: `gleskit_inspect_tabular_data -> gleskit_prepare_rnaseq_inputs -> gleskit_analyze_deseq2_counts -> gleskit_enrich_gene_set` or `gleskit_ranked_gene_set_gsea`
- for multi-contrast questions, repeat `gleskit_analyze_deseq2_counts` with different stage ids and then use `gleskit_compare_differential_expression_results`

### Single-cell mode

- source: one `.h5ad` object or one DEG workbook/table
- common sequence: `gleskit_analyze_single_cell_rnaseq` with `analysis="pseudobulk"` -> `gleskit_analyze_deseq2_counts`
- use `analysis="summary"` for object inspection and `analysis="cell_type_deg_counts"` / `analysis="gene_cell_type_lookup"` for direct cell-type questions

### Supervised matrix mode

- source: gene-level matrix plus a metadata table with labels or numeric targets
- common sequence: `gleskit_run_supervised_expression_model`
- use `gleskit_evaluate_cluster_agreement` instead of `gleskit_run_expression_consensus_clustering` when the question is specifically about held-out test agreement rather than cohort-wide bootstrap stability
- if the question asks for the held-out agreement count/rate on the test split, call `gleskit_evaluate_cluster_agreement` directly and answer from `agreement.count_test_only` / `agreement.rate_test_only`
- if the question asks how many observations are consistently grouped across the full matrix, use `gleskit_run_expression_consensus_clustering` and answer from `consistency_summary` or `stability`

### Enrichment mode

- source: DEG table, significant gene list, or ranked signature
- common sequence: `gleskit_enrich_gene_set`, `gleskit_ranked_gene_set_gsea`, or `gleskit_gene_set_disease_context`

### Literature follow-up mode

- source: biological result, ranked genes, or enrichment output
- common sequence: `gleskit_search_papers` or other provenance-focused follow-up

## Pipeline Contract

- `source` describes the starting data object.
- `steps` is an ordered list of stage objects, each with a `tool` name and a `params` object.
- Stage outputs are captured as artifacts and can be consumed by later stages in the same pipeline.
- If a later stage needs to reference multiple earlier DEG stages explicitly, give those earlier stages an `id` and point the later stage at them with `from_steps`.
- Build the entire stage list before calling `gleskit_run_pipeline`; do not use a second `gleskit_run_pipeline` call to continue the same turn.
- The backend accepts a one-key compatibility shorthand for stage objects, but the explicit `tool` / `params` form is the preferred contract.
- When `source.kind` is omitted, the backend can infer it from the supplied starting fields such as genes, ranked scores, query text, matrix paths, or counts paths, and in some cases from the stage list when the pipeline shape is already clear.
- This reference is the only public routing guide for chained workflows.
- Do not route new conversations directly to the old matrix mode file.
- If a pipeline stage fails validation, correct the GLESKit arguments and retry through GLESKit; do not continue the workflow with shell, Python, or R substitutions.

## Example Sequences

- `gleskit_inspect_tabular_data -> gleskit_prepare_rnaseq_inputs -> gleskit_analyze_deseq2_counts`
- `gleskit_run_log_transform -> gleskit_run_expression_pca`
- `gleskit_run_log_transform -> gleskit_run_expression_hierarchical_clustering`
- `gleskit_run_log_transform -> gleskit_run_expression_statistics`
- `gleskit_analyze_single_cell_rnaseq (pseudobulk) -> gleskit_analyze_deseq2_counts -> gleskit_enrich_gene_set`
- `gleskit_analyze_deseq2_counts (id=a) -> gleskit_analyze_deseq2_counts (id=b) -> gleskit_compare_differential_expression_results`
- `gleskit_gene_summary -> gleskit_interaction_network -> gleskit_pathway_context -> gleskit_gene_disease_associations -> gleskit_search_papers`
- `/pipeline /data artifact_... upload_... -> gleskit_run_pipeline`
- `gleskit_analyze_deseq2_counts -> gleskit_enrich_gene_set`
- `gleskit_analyze_deseq2_counts -> gleskit_ranked_gene_set_gsea`
- `gleskit_enrich_gene_set -> gleskit_search_papers`
