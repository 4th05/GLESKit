# RNA-seq And Statistics Routing

Use this routing for omics and statistics evaluation questions.

## Raw Counts + Sample Metadata

When the question provides raw counts plus metadata:

- inspect ambiguous workbooks or tabular inputs first with `gleskit_inspect_tabular_data`
- if the inputs need explicit alignment, sample filtering, or count-like transformation, use `gleskit_prepare_rnaseq_inputs`
- for one workbook containing both counts and metadata, copy the inspector's `recommended_arguments` into
  `prepare_rnaseq_inputs`: use its count/metadata sheet names, header rows, gene column, and matched
  `sample_key_column`; do not assume a visible `Sample ID` column is the key that matches matrix headers
- choose `condition_column` and any `sample_filter_column` from the returned candidate columns using the exact
  cohort and biological variable named in the question; do not let the tool substitute a contrast or silently
  include a different cohort
- when the inspector reports `count_transform="round_int"`, pass it explicitly before DESeq2; this records the
  normalized-value pseudo-count conversion instead of relying on an implicit coercion
- start with `gleskit_analyze_deseq2_counts`
- use file-backed metadata whenever available
- if `gleskit_prepare_rnaseq_inputs` produced a manifest, pass it with `prepared_manifest_path`
- if the metadata has more than two condition levels, run explicit pairwise contrasts with exact visible level strings
- if shrinkage is requested, keep the requested contrast explicit
- for several contrasts over the same cohort, prepare once and reuse the same manifest for every DESeq2 call

## Raw Counts Without A Metadata Table

- inspect the matrix and use its bounded complete `matrix_sample_columns` list as the only source of sample names
- infer biological group labels only when the question makes that mapping explicit, then pass one exact `sample_group_map` object to `gleskit_analyze_deseq2_counts` (or `gleskit_prepare_rnaseq_inputs` when a reusable manifest is needed)
- the map must cover every matrix sample and must not include extra names; GLESKit materializes and validates the metadata internally
- do not hand-write `sample_metadata_text` and do not omit a sample merely because it is outside `matrix_sample_preview`

## Result-Table Questions

When the question provides a DEG/result table or a DESeq2 handoff:

- start with `gleskit_analyze_rnaseq_result`
- use `focus_genes` when the question targets a specific gene
- use the returned compact summary or focus rows to answer directly

## Single-Cell DEG Workbook Questions

When a workbook contains one DEG sheet per cell type and the question asks which cell type has the most or fewest significant genes:

- use `gleskit_analyze_single_cell_rnaseq`
- set `analysis="cell_type_deg_counts"`
- pass the workbook as `deg_table_path`
- omit `cell_type_column` for sheet-per-cell-type workbooks so the tool uses sheet names as the labels
- if the first gene-id column is blank or `Unnamed: 0`, omit `gene_column` and let the tool use the first column automatically
- finalize from `question_ready.top_cell_type` or the first row of `cell_type_counts`
- do not loop sheet-by-sheet with `gleskit_analyze_rnaseq_result` unless the question asks about one specific gene or one specific sheet metric

## Overlap / Shared / Unique DEG Questions

When the question asks for overlap, shared genes, union counts, or exclusive counts:

- use `gleskit_compare_differential_expression_results`
- prefer DESeq2 handoff paths or handoff manifests from prior steps
- do not manually compute the set arithmetic outside the tool

When a question explicitly requests a binomial exact test after a DESeq2 workflow:

- obtain the required success and trial counts from compact DESeq2 or comparison-tool results first
- call `gleskit_run_expression_statistics` with `analysis="exact_binomial_test"` and the question's stated null
  probability or threshold interpretation
- do not invent a numerator, denominator, or null probability that the question does not define

## Enrichment Questions

When the question asks for pathway or term enrichment:

- use `gleskit_enrich_gene_set` or `gleskit_ranked_gene_set_gsea`
- pass `genes_path` or `gene_scores_path` from `derived_inputs` whenever available
- when the question explicitly names a gseapy/Enrichr library, preserve its exact identity with `backend="enrichr"` (for example `library="GO_Biological_Process_2021"`, `library="Reactome_2022"`, or `library="KEGG_2019_Mouse"`); custom tested-gene backgrounds are supported through the generated `background_genes_path`
- use `database="REAC"` for generic Reactome questions and `database="GO_BP"` for generic GO biological-process questions; these select GLESKit's local clusterProfiler/ReactomePA backend and are not interchangeable with Enrichr libraries
- call `gleskit_enrich_gene_set` directly on the saved gene-list handoff; do not route these questions through a broad RNA-seq orchestration call
- use `all_significant_genes_path` unless the question explicitly asks for upregulated or downregulated genes
- when the question names a specific pathway, GO term, or identifier, pass the exact target through `focus_terms` and report the matching `focused_terms` row; do not search OpenCode cache files for omitted rows
- answer from the returned top terms or focused terms instead of reopening enrichment tables

## DESeq2 Filter Semantics

- use `significance_mode="p_value"` and `p_value_threshold` only when the question names a raw p-value cutoff
- use `significance_mode="padj"` and `padj_threshold` when the question names adjusted p-values or padj
- use `base_mean_threshold` for a stated baseMean cutoff; do not substitute the count prefilter `min_count`
- pass an explicit `log2_fold_change_threshold` only when the question states an LFC cutoff; otherwise use `0.0` for an enrichment gene handoff rather than inventing a cutoff

When metadata exposes several plausible controls, the question's named level is a hard constraint. For example, a
question naming DMSO must use `condition_b="DMSO"` even if `Untreated` is also available. Never infer a control from
the word "control" alone. If a tool call fails because a guessed label is invalid, inspect the returned levels and
retry with an exact visible value; if the guessed label is valid but contradicts the question, correct the call before
continuing.

## Clustering + Marker Questions

When the question asks about clusters, cluster-specific genes, or enrichment of cluster markers:

- run a clustering tool first
- if the clustering response returns `derived_inputs.cluster_labels_path`, pass that path into `gleskit_cluster_marker_analysis`
- when enrichment is requested, prefer `enrichment_database=REAC` for Reactome-specific questions
- `gleskit_cluster_marker_analysis` can already summarize per-cluster pathway counts in `question_ready.cluster_enrichment_summary`
- if `gleskit_cluster_marker_analysis` already returns `question_ready.top_pathway_for_max_cluster` or `question_ready.top_reactome_pathway_for_max_cluster`, finalize from that block

## Matrix / Workbook Statistics Questions

When the input is a matrix or workbook and the question is about skewness, Wilson intervals, fold-change summaries, or per-feature group tests:

- inspect ambiguous workbooks first with `gleskit_inspect_tabular_data`
- if you apply `gleskit_run_log_transform`, reuse the returned `derived_inputs.matrix_path` or the saved `artifact_...` selector instead of reopening the raw matrix
- for PCA questions, answer directly from `question_ready.pc1_explained_variance_percent` when present
- use `gleskit_run_expression_statistics` only if inspection shows the question is truly a direct matrix-statistics problem
- route pairwise log2-fold-change ANOVA questions to `analysis="matrix_group_test"` with `test="anova"` and `observation_unit="pairwise_log2_fold_change"`; this tests the six pairwise distributions for four selected groups
- route other multi-group ANOVA questions to `analysis="matrix_group_test"` with an explicit `observation_unit`; use `flattened_values` only when pooling every matrix entry is explicit, otherwise use `featurewise`
- route all-pairwise log2 fold-change distribution questions to `analysis="matrix_log2_fold_change_distribution"`, not DESeq2
- if inspection reports a blank first identifier column or `column_1`, omit `row_id_column` and let the tool resolve the first column
- if matrix data and sample metadata are separate, keep them separate and pass the correct file or sheet role
- use `gleskit_prepare_rnaseq_inputs` plus `gleskit_analyze_deseq2_counts` when inspection shows the real task is a DEG/contrast workflow
- do not force those questions through DESeq2 unless the question is actually about differential expression or a DEG-derived proportion

## Continuous And qPCR Matrices

- Treat Ct, qPCR, and other continuous assay matrices as matrix statistics, not raw-count DESeq2 inputs.
- Use `matrix_featurewise_group_test` when the question asks for per-feature p-values or multiple-testing counts.
- Use `matrix_group_test` when the question asks for one pooled ANOVA/t/Welch statistic; set `observation_unit` explicitly.
- Pass `value_transform="log2"` only when the question explicitly requests log2 transformation; use `log2p1` only for a stated plus-one transform.
- When sample labels follow an unambiguous prefix-number pattern such as `P_1` and `C_11`, verify `candidate_sample_group_map` from inspection and pass it as `sample_group_map` when metadata is absent.
- If inspection reports missing Ct/continuous measurements, pass `missing_value_policy="omit"` for per-feature testing unless the protocol explicitly requires complete features. Report the returned tested-feature count and per-feature group counts; do not fill missing Ct values arbitrarily.
- Copy `header_row`, `analysis_axis`, `sample_group_map`, and `missing_value_policy` from the inspector's `recommended_arguments`; do not reuse a preview-derived orientation or invent a different header.
- For uncorrected significance questions, use `question_ready_counts.significant_feature_count_raw` or `significant_feature_percentage_raw`. For a question that requires every correction method, use `significant_feature_count_all_corrections`.
- Do not infer a group map from arbitrary labels; provide metadata or abstain when the mapping is ambiguous.

## RDS Differential-Expression Results

- An `.rds` input is an R differential-expression result, not a text table. Call `gleskit_analyze_rnaseq_result` directly with `result_path`; GLESKit converts it to a reusable TSV internally.
- Analyze each comparison with the requested p-value and absolute-LFC thresholds before running enrichment. For a question comparing pathway sets, pass each returned `all_significant_genes_path` directly to `gleskit_enrich_gene_set`; it contains the full filtered set, unlike compact directional follow-up lists. Select the stated database and compare returned significant term IDs. For Pseudomonas aeruginosa `PA14_...` locus tags, use `database="KEGG"`; GLESKit records the PA14 strain automatically. Do not attempt to decode RDS files yourself.

## Composite DESeq2 Contrasts

- Use `gleskit_analyze_stratified_deseq2` for a question that combines strata, such as `CD4` plus `CD8` versus the remaining cell types, with a within-stratum M-versus-F contrast.
- Pass target and reference strata as separate exact lists. Never encode a set of levels as one value such as `CD4/CD8`.
- Keep protein-coding, p-value, baseMean, absolute-LFC, and shrinkage filters in the tool arguments or returned workflow configuration.
- Finalize from the returned `question_ready` statistic and per-stratum counts.
- For a gene-specific LFC that does not name a cell-type subset, use `analysis="focus_gene_lfc"` with `focus_genes`; omit `target_strata` to run one overall contrast. If the question names a subset, pass it as `target_strata` and the tool runs one pooled contrast over that subset.
- For a padj/baseMean/absolute-LFC count in selected strata, use `analysis="significant_gene_count"`, copy the named subset into `target_strata`, and pass `padj_threshold`, `base_mean_threshold`, and `absolute_log2_fold_change_threshold`. Do not pass reference strata unless the question actually asks for a cross-group distribution comparison.
- For a Welch or ordinary t-test comparing annotation classes inside selected strata, use `analysis="annotation_group_lfc_test"`, `target_strata`, the annotation path/key/group columns, and the exact selected `feature_annotation_group_value`. This compares the selected class with all other annotated classes after one pooled M-versus-F contrast.
- The focus-gene endpoint reports annotation membership but never silently removes the requested gene solely because its annotation disagrees with wording in the question; state that membership if it matters to the interpretation.

## Direct Result-Column Counts

- Use `analysis="threshold_count"` for counts such as dispersion `< 1e-05`.
- Pass the exact `value_column`, operator, and threshold. Do not use a broad distribution summary when the question asks for a threshold count.

## Finalization Reminder

If a tool already returned:

- a scalar
- a count
- a confidence interval
- a top pathway term
- a gene-specific metric
- an overlap/exclusive/shared count

then stop and finalize.
