# GLESKit MCP Tool Contract

Use this file as the current prompt-facing and client-facing contract for the GLESKit MCP tool surface.

It is intentionally practical: what each major analysis tool is for, what figure behavior to expect, and what boundaries still exist.

## Contents

- [Path Selectors](#path-selectors)
- [Workspace, Uploads, And Reports](#workspace-uploads-and-reports)
- [Figure Generation Model](#figure-generation-model)
- [Current Direct Analysis Tools](#current-direct-analysis-tools)
- [Tabular Inspection And RNA-seq Preparation](#tabular-inspection-and-rna-seq-preparation)
- [Matrix, Statistics, Clustering, And Modeling](#matrix-statistics-clustering-and-modeling)
- [Single-cell And DEG Comparison](#single-cell-and-deg-comparison)
- [Figure Discovery And Rendering Tools](#figure-discovery-and-rendering-tools)
- [Single-Gene Evidence Tools Still Matter](#single-gene-evidence-tools-still-matter)
- [Figure Contract](#figure-contract)
- [Important Current Boundaries](#important-current-boundaries)
- [Prompt-Pack Guidance](#prompt-pack-guidance)
- [Exact Analysis Parameter Contract](#exact-analysis-parameter-contract)

## Path Selectors

Many GLESKit tools now accept the same three path-selector forms:

- absolute local file paths
- `upload_...` selectors for saved user uploads
- `artifact_...` selectors for reusable saved artifacts

Prompt packs and operators should prefer reusing these selectors directly instead of trying to read large files into model context.

## Workspace, Uploads, And Reports

The workspace surface is still optional, but the live MCP now exposes a broader saved-state layer than the older docs implied.

Important groups:

- session state:
  - `workspace_status`
  - `list_sessions`
  - `get_session_summary`
  - `create_session`
  - `update_session`
  - `archive_session`
  - `plan_delete_sessions`
  - `delete_sessions`
- notes:
  - `add_session_note`
  - `list_session_notes`
  - `get_session_note`
  - `update_session_note`
  - `delete_session_note`
  - `delete_session_notes`
- artifacts and uploads:
  - `list_session_artifacts`
  - `get_artifact`
  - `delete_session_artifacts`
  - `list_session_uploads`
  - `get_upload`
  - `delete_session_uploads`
  - `create_session_from_artifacts`
- reports:
  - `list_report_templates`
  - `get_report_template`
  - `list_session_reports`
  - `get_session_report`
  - `create_session_report`
  - `update_session_report`
  - `delete_session_report`
  - `delete_session_reports`

Boundary:

- workspace/report/upload tools should be used only when the user explicitly wants persistence or saved-state inspection
- normal benchmark solving should stay path-driven and stateless unless the task itself is report-oriented

Current report-tool behavior:

- `create_session_report`, `update_session_report`, and `get_session_report` return `linked_artifacts` so the client can inspect the saved evidence bundle without reopening raw artifact files
- when the saved report includes literature citations, those same payloads now also return a deterministic `citation_audit`
- `citation_audit` is designed as a repair loop for agents: it flags mismatched visible paper titles, paper links that omit the exact title, and cited paper artifacts that were not carried into `linked_artifact_ids`

## Figure Generation Model

GLESKit now separates figure rendering from session persistence.

- Figure rendering can be stateless using `figure_source_id`.
- Session/workspace is optional and only needed when you want to save artifacts.
- Use `list_figure_options` to discover valid figure types dynamically.
- Use `generate_figure` to render a chosen figure type from either:
  - `figure_source_id` (stateless), or
  - `session_id` + `artifact_id` (saved artifact path).

## Current Direct Analysis Tools

The live analysis surface now falls into five practical families:

- gene/protein/entity context
- gene-set and enrichment analysis
- tabular inspection and RNA-seq preparation
- matrix/statistics/clustering/modeling
- single-cell and DEG comparison

### `resolve_gene`

Use for one gene only.

Important boundary:

- do not call this repeatedly for a gene list
- for a whole list, use `resolve_gene_set` if you need a batch normalization audit
- for ordinary analysis, prefer the set-level tools because they already normalize inputs internally

### `resolve_gene_set`

Use for a batch normalization audit of one gene list.

Current behavior:

- resolves a whole input list in one call
- returns canonical symbols and identifier fields
- saves a `gene_set_resolution` artifact when a workspace session is active

Important boundary:

- this is an audit/helper tool
- for ordinary biological interpretation, prefer `investigate_gene_set`, `enrich_gene_set`, `gene_set_disease_context`, or `compare_gene_programs`

### `enrich_gene_set`

Use for pathway and biological-process enrichment on one gene set.

Current behavior:

- human gene sets prefer the local `clusterProfiler` + `ReactomePA` backend
- GLESKit falls back to `g:Profiler` when needed
- pass `library="GO_Biological_Process_2021"` or `library="Reactome_2022"` with `backend="enrichr"` when the question explicitly requires the corresponding Enrichr/gseapy library; this route does not silently fall back to clusterProfiler
- native figures:
  - enrichment bar plot auto-attached by default
  - enrichment maps, gene-concept networks, and gene-gene/PPI networks are available through `list_figure_options` + `generate_figure` from the same returned data (stateless) or saved artifact

### `gene_set_disease_context`

Use for disease-program enrichment on one human gene set.

Current behavior:

- local `DOSE` backend
- current databases: `DO`, `DGN`, `NCG`
- native figure: disease-enrichment bar plot auto-attached when terms are returned
- disease enrichment maps, disease gene-concept networks, and disease gene-gene/PPI networks are available through `list_figure_options` + `generate_figure` from the same returned data (stateless) or saved artifact

Important boundary:

- this is a whole-set disease-enrichment tool
- it is not a causal proof tool

### `ranked_gene_set_gsea`

Use for one ranked gene-score signature.

Current behavior:

- local `clusterProfiler` + `ReactomePA` backend
- current sources: `GO:BP`, `REAC`
- native figures:
  - enrichment bar plot auto-attached by default when ranked terms are returned
  - ranked GSEA running-score plot available through `list_figure_options` + `generate_figure` when the returned ranked inputs support it

### `compare_gene_programs`

Use for pathway/program comparison across multiple named gene sets.

Current behavior:

- local `clusterProfiler` + `ReactomePA` backend
- compares pathway/program enrichment patterns across sets
- native figures:
  - grouped comparison bar plot auto-attached by default when terms are returned
  - compareCluster enrichment map and compareCluster gene-concept network available through `list_figure_options` + `generate_figure` when inputs support them

Important boundary:

- this is not a dedicated disease-comparison tool

### `investigate_gene_set`

Use for the broad first-pass integrated workflow on one gene set.

Current behavior:

- gene resolution
- pathway enrichment
- whole-set disease enrichment
- lead-gene disease follow-up
- literature retrieval
- native figures:
  - enrichment bar plot auto-attached by default
  - enrichment maps, gene-concept networks, and gene-gene/PPI networks remain available through `list_figure_options` + `generate_figure` when inputs support them

### `analyze_rnaseq_result`

Use for a DESeq2-style differential-expression result table or a local result path.

Current behavior:

- pathway enrichment for upregulated and downregulated signatures
- ranked GSEA when the local backend is available
- whole-signature disease enrichment when the local backend is available
- lead-gene disease follow-up
- literature retrieval
- native figures:
  - default auto-attached set: volcano plot plus up/down pathway bar plots
  - optional figures: up/down enrichment maps, up/down gene-concept networks, and up/down gene-gene/PPI networks through `list_figure_options` + `generate_figure` when inputs support them
  - when an up/down enrichment map is too heavy to render cleanly, GLESKit may return a native gene-concept network fallback instead of failing outright

Important boundary:

- use this when the user already has a DEG/result table
- if the user has raw counts plus metadata, prefer `prepare_rnaseq_inputs` and/or `analyze_deseq2_counts`

### `analyze_differential_result`

Use for a precomputed transcriptomics or proteomics differential-result table when the contrast orientation, modality, result scope, and significance basis can be declared explicitly. It maps features to genes conservatively, preserves mapping audit records, and returns reusable canonical analysis data. It is not a raw-omics processing tool. For a large complete table, set `include_enrichment=false` and/or `include_ranked_gsea=false` to return the validated canonical result first; then use its returned directional gene-set paths plus `tested_genes_path`, or `ranked_gene_scores_path`, for the expensive follow-up tools.

`effect_definition` is required for a linear abundance ratio: declare its biological numerator and denominator, plus an optional source formula. GLESKit converts it to a log2 effect and explicitly retains or inverts the sign to match the requested contrast. It never infers condition biology from a ratio header. Responses are compact by default; the complete audit remains in the returned manifest path. Use `save_to_session_id` only for an explicitly requested persistence action.

### `preflight_differential_result`

Use before a workbook-backed differential-result analysis when sheet roles or result scope are uncertain. It scans table structures server-side, reports complete and filtered candidates without returning rows, and identifies a structural complete-table recovery candidate only when the filtered identifiers are an exact subset and a compatible significance-statistic type exists. Biological effect/contrast semantics must still be declared before use.

### `integrate_gene_level_results`

Use after two or more `analyze_differential_result` calls. It returns a modality-preserving gene evidence matrix and pathway convergence summary without a biomarker score. Directional concordance is reported only when all inputs declare the same positive/reference conditions. Pass separately computed, direction-labelled enrichment outputs in `pathway_results` for valid pathway convergence; absence of comparable enrichment is reported as unavailable, not as a negative result.

### Differential-result reliability and persistence

Use `analysis_runtime_status` after a timeout before retrying a long local R analysis. It lists registered workers and recent terminal diagnostics; `cancel_analysis_job` requests process-group cancellation for a still-running job. Use `save_analysis_manifest_to_session` to persist any generated analysis handoff without changing the UI-owned active session. `build_biomarker_candidate_dossiers` creates transparent dossiers only for explicitly selected genes and never computes a fixed biomarker score or clinical-validation label.

## Tabular Inspection And RNA-seq Preparation

### `inspect_tabular_data`

Use first when a workbook or table is ambiguous.

Current behavior:

- inspects one tabular file or workbook sheet without choosing the route for the model
- returns routing facts such as:
  - detected data type
  - candidate sample/group/gene columns
  - sheet roles
  - matrix orientation
  - candidate analysis families
  - ambiguities
  - next-step hints

Important boundary:

- this tool exposes facts, not a forced analysis plan
- it is the correct first move for ambiguous workbook questions

### `prepare_rnaseq_inputs`

Use when raw-count style inputs need explicit sanitization before DESeq2.

Current behavior:

- materializes sanitized counts and metadata TSVs
- supports:
  - separate counts + metadata files
  - same-workbook counts + metadata
  - optional sample filtering
  - explicit count transforms:
    - `none`
    - `round_int`
    - `library_scale_round`
- returns a reusable prepared manifest for downstream `analyze_deseq2_counts`

Important boundary:

- mechanical cleanup is allowed here
- scientific choices such as sample filtering and count transformation must still be explicit in the call and remain visible in provenance

### `analyze_deseq2_counts`

Use for raw counts plus metadata when the task is really a DESeq2-style contrast question.

Current behavior:

- runs DESeq2 from raw counts
- accepts either direct file inputs or `prepared_manifest_path` from `prepare_rnaseq_inputs`
- returns compact question-ready summaries such as:
  - comparison
  - tested gene counts
  - significant gene counts
  - subset/provenance details
  - direct focus-gene metrics when `focus_genes` is supplied
- returns compact derived follow-up files for:
  - significant genes
  - ranked scores
  - DE result tables

Important boundary:

- this is for count-based differential expression
- do not use it for already-computed DEG tables
- if the input is already a result table, prefer `analyze_rnaseq_result`

## Matrix, Statistics, Clustering, And Modeling

### `run_log_transform`

Use to materialize an explicit transformed matrix before downstream analysis.

Current behavior:

- applies one controlled log transform preset or custom base/offset
- returns a reusable transformed matrix path in `derived_inputs`
- does not expose the whole transformed matrix inline

### `run_expression_pca`

Use for PCA on expression matrices when the question asks for variance structure or leading components.

Current behavior:

- supports observation-axis selection
- returns compact PCA summaries
- surfaces direct question-ready values such as first-component explained variance

### `run_expression_hierarchical_clustering`

Use for ordinary hierarchical clustering on one expression matrix.

Current behavior:

- supports clustering-distance and linkage settings
- returns reusable saved cluster labels through `derived_inputs.cluster_labels_path`

### `run_expression_correlation`

Use for matrix-wide or paired-matrix correlation questions.

Current behavior:

- supports one matrix or matrix-vs-secondary matrix flows
- supports group-level summaries and top correlated outputs

### `run_expression_consensus_clustering`

Use for cohort-wide bootstrap consensus clustering.

Current behavior:

- computes bootstrap stability summaries
- is different from held-out agreement evaluation

Important boundary:

- use this for stability/consensus questions
- use `evaluate_cluster_agreement` for train/test held-out agreement questions

### `run_supervised_expression_model`

Use for classification or regression from expression data plus metadata.

Current behavior:

- supports:
  - classification
  - regression
  - train/test, stratified, and group-aware splits
- returns split-aware evaluation summaries and top features

### `evaluate_cluster_agreement`

Use for held-out train/test agreement on clustering assignments.

Current behavior:

- measures agreement after train/test splitting
- returns test-only agreement counts and rates

### `run_expression_statistics`

Use for focused statistics, not for everything that merely contains a matrix.

Current behavior:

- supports several analysis families:
  - `matrix_annotation_correlation`
  - `distribution_summary`
  - `group_test`
  - `featurewise_group_test`
  - `matrix_featurewise_group_test`
  - `matrix_group_fold_change_summary`
  - `proportion_interval`
  - `exact_binomial_test`
  - `multiple_testing_summary`
- supports workbook-driven and table-driven paths
- supports metadata and feature-annotation restriction when needed

Important boundary:

- do not use this as a generic fallback for DEG/contrast questions that really need DESeq2
- when the task is about DEG counts, contrasts, shrinkage, or DE overlap, prefer `analyze_deseq2_counts`, `analyze_rnaseq_result`, or `compare_differential_expression_results`

## Single-cell And DEG Comparison

### `analyze_single_cell_rnaseq`

Use for one single-cell RNA task at a time.

Current behavior:

- supports:
  - object summary
  - cell-type DEG counts
  - gene lookup across cell types
  - pseudobulk construction
- accepts `.h5ad` objects and DEG tables where appropriate

Important boundary:

- this is not a free-form single-cell planner
- choose one explicit single-cell task per call

### `cluster_marker_analysis`

Use to derive cluster markers from one expression matrix and optionally enrich the strongest markers.

Current behavior:

- consumes direct cluster labels or a saved `cluster_labels_path`
- can optionally run enrichment on top markers
- returns question-ready summaries for cluster-level pathway interpretation when available

### `compare_differential_expression_results`

Use to compare two or more DEG/result sets.

Current behavior:

- accepts:
  - DEG tables
  - DESeq2 handoff manifests
  - compact gene-list paths emitted by DESeq2/RNA-seq tools
- returns compact set-operation summaries such as:
  - shared counts
  - exclusive counts
  - union counts
  - direction-aware comparisons where applicable

Important boundary:

- this is for DE result comparison, not ranked GSEA
- do not pass ranked score files here

### `run_pipeline`

Use when the user explicitly wants one bounded linear multi-step GLESKit pipeline in a single call.

Current behavior:

- executes a step list over one source payload
- stores each stage as a workspace artifact
- supports compatibility shorthands, but still expects an explicit step sequence

Important boundary:

- this is a linear executor, not a replacement for ordinary step-by-step tool use
- for most benchmark questions, direct single-tool or short explicit multi-tool use is clearer

## Figure Discovery And Rendering Tools

### `list_figure_options`

Use when you need to know which figure types are available for a source.

Inputs:

- stateless path: `figure_source_id`
- saved path: `session_id` + `artifact_id`

Behavior:

- returns dynamic figure metadata from `FigureCatalog.figure_options(...)`
- no hardcoded list is required
- new figure types automatically appear when added to the catalog

### `generate_figure`

Use to render a specific figure type.

Inputs:

- required: `figure_type`
- source selector:
  - `figure_source_id` (stateless), or
  - `session_id` + `artifact_id` (saved)
- optional: `title`, `top_n`
- optional persistence: `save_to_session_id`

Behavior:

- renders figure without requiring a session
- can optionally persist into a session when requested

## Single-Gene Evidence Tools Still Matter

These are still important and are not replaced by the local set-level backends:

- `gene_disease_associations`
  Live single-gene disease evidence, currently backed by Open Targets
- `variant_trait_associations`
  Single-gene GWAS study and trait context
- `drug_target_context`
  Drug-target and indication context

## Figure Contract

GLESKit now treats figures as part of the main analysis result when the tool supports them.

Expected behavior:

- figure-capable analysis tools auto-attach native figures in the same MCP result
- figure-capable analysis tools now also return `figure_source_id` so specific figures can be generated statelessly
- the same analysis can still be saved as artifacts when a workspace session is active
- enrichment-style GLESKit figures currently prefer bar plots plus enrichplot-style relationship views when inputs support them
- GLESKit supports three main relationship views when inputs are available:
  - term-term relationship maps (`emapplot`)
  - term-gene relationship networks (`cnetplot`)
  - gene-gene/PPI networks via `clusterProfiler::getPPI()`
- heavy enrichplot relationship views may retry with fewer terms; if an enrichment map is still too heavy, GLESKit may return a native gene-concept network fallback

Current figure-capable analysis tools:

- `analyze_deseq2_counts`
- `enrich_gene_set`
- `gene_set_disease_context`
- `ranked_gene_set_gsea`
- `compare_gene_programs`
- `investigate_gene_set`
- `expression_context`
- `analyze_rnaseq_result`

## Important Current Boundaries

These are real current limitations and prompts should respect them:

- GLESKit does not yet have a dedicated direct multi-set disease-comparison tool
- `compare_gene_programs` compares pathway/program enrichment, not disease themes
- the local disease-program and ranked local backends currently assume human-focused inputs
- set-level disease enrichment should not be described as proof of causality
- `inspect_tabular_data` should expose route facts, not a forced scientific plan
- `prepare_rnaseq_inputs` is for auditable preprocessing, not for silently making scientific assumptions
- `run_expression_statistics` is not a universal fallback for every matrix-looking file
- `analyze_single_cell_rnaseq` expects one explicit single-cell subtask per call
- `run_pipeline` is a bounded linear executor, not a hidden planning agent

## Prompt-Pack Guidance

Prompt packs should currently teach the following:

- prefer path selectors (`absolute path`, `upload_...`, `artifact_...`) over reading bulk tables into context
- inspect ambiguous tabular/workbook inputs first with `inspect_tabular_data`
- use `prepare_rnaseq_inputs` before DESeq2 when counts/metadata alignment or explicit sanitization is needed
- use `analyze_deseq2_counts` for raw counts and contrasts
- use `analyze_rnaseq_result` for already-computed DEG/result tables
- use `resolve_gene` only for one gene at a time
- use `resolve_gene_set` when a batch normalization audit is actually needed
- use `gene_set_disease_context` for one-set disease-theme questions
- use `ranked_gene_set_gsea` for ranked gene-score signatures
- use `compare_gene_programs` for multiple named programs or gene sets
- use `compare_differential_expression_results` for shared/exclusive DEG questions
- use `run_expression_statistics` only for true statistics questions rather than DESeq2-style contrast questions
- use `evaluate_cluster_agreement` only for held-out agreement questions, not generic consensus questions
- explain that `investigate_gene_set` and `analyze_rnaseq_result` now include whole-set disease enrichment behavior
- for specific figure requests, call `list_figure_options` first and then `generate_figure` with exact `figure_type`
- do not try to control figure type by writing `figure_type=...` in free-text inputs like investigation questions
- when the user asks for a pathway relationship graph from enrichment, disease-enrichment, or RNA-seq results, prefer the GLESKit-native enrichplot map first and accept the GLESKit-native gene-concept network fallback if the map is too heavy
- when the user asks for a gene-gene network from enrichment, disease-enrichment, or RNA-seq results, use the GLESKit-native PPI network when the saved term-gene inputs support it
- do not promise direct disease comparison across multiple sets
- do not loop `resolve_gene` over ordinary gene-set inputs

## Exact Analysis Parameter Contract

The MCP tool descriptions and OpenCode prompt packs must preserve these semantics:

- `inspect_tabular_data` exposes exact visible metadata values in `candidate_group_levels`; callers must preserve those values in contrast and matrix-group arguments.
- `analyze_deseq2_counts` uses `significance_mode="p_value"` for raw p-values and `significance_mode="padj"` for adjusted p-values. `base_mean_threshold` filters result rows after DESeq2; `min_count` filters raw counts before DESeq2; `log2_fold_change_threshold=0.0` means no absolute-LFC filter.
- A named condition is a hard constraint. If the question says `DMSO`, `condition_a`/`condition_b` must use the exact metadata value `DMSO`, even when `Untreated` is also a valid level.
- `run_expression_statistics` requires an explicit estimand for matrix group tests: `flattened_values`, `featurewise`, or `pairwise_log2_fold_change`. The latter computes group-mean log2 ratios across every selected group pair and requires a positive pseudocount.
- `matrix_log2_fold_change_distribution` is the route for all-pairwise distribution shape or skewness questions; it is not an ANOVA.
- `enrich_gene_set` uses the local generic routes for `database="GO_BP"` and `database="REAC"`. Named gseapy/Enrichr libraries require the exact `library` and `backend="enrichr"`, and the explicit route does not silently fall back to the local backend.
- Handoff paths returned in `derived_inputs`, especially `prepared_manifest_path` and gene-list paths, are the canonical downstream inputs.
