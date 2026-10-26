# Tool Call Contract

Use GLESKit MCP tools as the primary analysis engine.

`analyze_differential_result` and `integrate_gene_level_results` are available only for a future episode that explicitly provides compatible precomputed cross-modal result tables; they do not change this fixed single-question evaluation workflow.

Mandatory contract:

- in OpenCode, use the exposed GLESKit tool names with the `gleskit_` prefix, for example `gleskit_inspect_tabular_data`
- inspect ambiguous workbook or tabular inputs with `gleskit_inspect_tabular_data` before choosing a downstream route
- when `gleskit_inspect_tabular_data` returns `candidate_group_levels`, `candidate_sample_preview`, `matrix_sample_preview`, or bounded `matrix_sample_columns`, treat those as the authoritative visible hints instead of trying to open the raw file
- if counts and metadata need explicit alignment, filtering, or count-like transformation, use `gleskit_prepare_rnaseq_inputs`
- prefer file-path arguments over inline pasted data
- prefer workbook sheet selection arguments over manual workbook inspection
- if `gleskit_prepare_rnaseq_inputs` produced a manifest, pass `prepared_manifest_path` into `gleskit_analyze_deseq2_counts`
- if a tool returns `derived_inputs`, treat those file paths as the default follow-up route
- if a clustering tool returns `derived_inputs.cluster_labels_path`, pass that path into `gleskit_cluster_marker_analysis`
- if `gleskit_cluster_marker_analysis` already returns the needed cluster/pathway answer in `question_ready`, finalize from that block instead of reopening marker-set files
- do not manually page through large previews when the tool already returned a compact answer block or follow-up path
- do not pass partial inline sample metadata for count-matrix analyses when a metadata file path is available
- when a raw-count matrix has no metadata table, pass one exact `sample_group_map` object covering every returned `matrix_sample_columns` value to `gleskit_prepare_rnaseq_inputs` or `gleskit_analyze_deseq2_counts`; do not construct `sample_metadata_text` manually
- never combine `sample_group_map` with a metadata file, a prepared manifest, or inline metadata text
- do not pass a handoff manifest into a tool that expects a direct file path unless the tool explicitly accepts the manifest
- use exact visible condition labels verbatim
- if a contrast fails, inspect the visible levels returned by the tool and retry with those exact strings
- if a result-table question asks about one gene, use the result-table or focus-gene pathway rather than reopening the whole file
- if a workbook contains one DEG sheet per cell type and the question asks which cell type has the most or fewest significant genes, use `gleskit_analyze_single_cell_rnaseq` with `analysis=cell_type_deg_counts` instead of looping sheet-by-sheet
- if a comparison or overlap question is already answered by a comparison tool output, finalize immediately
- if a tool returns the same non-retryable schema or alignment error twice, stop calling it and abstain; do not cycle through guessed headers or labels

Forbidden fallbacks:

- no workspace handshake
- no repo inspection
- no full-file manual reading
- no subagent delegation for set math or DEG overlap when GLESKit already has a comparison tool
- no paper-search fallback for ordinary numeric or DEG questions

## Exact Labels, Filters, And Provider Identity

- Treat `candidate_group_levels` from `gleskit_inspect_tabular_data` as authoritative visible metadata. Preserve exact spelling, case, punctuation, and underscores in every downstream group or condition argument.
- If the question says `DMSO`, use the exact metadata level `DMSO`; never substitute `Untreated`, `vehicle`, or another plausible control.
- In `gleskit_analyze_deseq2_counts`, `significance_mode="p_value"` is for raw p-values, `significance_mode="padj"` is for adjusted p-values, `base_mean_threshold` is a post-result filter, and `log2_fold_change_threshold=0.0` means no absolute-LFC filter.
- Pass a positive LFC threshold only when the question states it. Do not inherit or invent `1.0`.
- Named gseapy/Enrichr libraries require the exact `library` and `backend="enrichr"` (including organism-specific names such as `KEGG_2019_Mouse`); generic `database="GO_BP"` and `database="REAC"` are different local routes.
- Use `backend="clusterprofiler"` only when the requested method must be local clusterProfiler. That explicit request never substitutes g:Profiler; a structured failure means the requested method could not be completed and must not be replaced silently.
- When a question names a specific pathway, GO term, or identifier, pass the exact name or identifier in `focus_terms` and use the returned `focused_terms` rows even when the term is outside the top preview.
- Treat an empty `focused_terms` result as evidence that the requested term was not found in that computed result, not as permission to inspect hidden tool-output files.
- If an enrichment response reports `focus_status="not_found"` under the intended database, library, and background, report that result and finalize. Do not rerun the same evidence with altered focus labels, thresholds, or simplification settings.
- Confirm the returned `library`, `backend`, and top-term or focused-term fields before finalizing an enrichment answer.

## Input Resolution And Safety

- Prefer the inspector's `header_row`, `row_identifier_column`, `candidate_sample_column`, and candidate group values.
- Preserve exact labels such as `DMSO`, `Code`, `Tissue`, `GeneID`, and `protein_coding`; these are data values, not synonyms to translate.
- If inspection reports `candidate_sample_group_map`, verify it and pass it only for the corresponding matrix columns.
- Use `background_genes_path` only with a generated tested-gene handoff or another path returned by GLESKit. Never construct a path into audit files, private logs, or unrelated project files.
- Direct shell, Python, repository inspection, arbitrary file reads, and full-table paging remain forbidden. Tool outputs are intentionally compact.

## Matrix Estimands

For `gleskit_run_expression_statistics` with `analysis="matrix_group_test"`, choose one explicit observation unit:

- `flattened_values`: pooled raw feature-by-sample entries.
- `featurewise`: a test for each feature across selected groups.
- `pairwise_log2_fold_change`: group-mean log2 ratios for every selected group pair, followed by the requested test; with four groups this yields six pairwise distributions.

Use `analysis="matrix_log2_fold_change_distribution"` for all-pairwise distribution shape questions. Do not replace a
matrix statistic with DESeq2 merely because the input is an RNA-seq matrix.
