# RNA-seq And Omics

Use this file for:

- DESeq2 result tables
- uploaded raw counts and count matrices
- gene-level expression matrices that need PCA, clustering, or correlation
- DEG files
- local paths to omics results
- transcriptomics interpretation
- single-cell RNA objects or DEG workbooks
- RNA/gene statistics and QC questions
- supervised matrix questions over gene-level features
- broader multi-omics reasoning when one modality is already available

Before any path-aware or tool-backed omics analysis call, also read `references/12_tool_call_contract.md`.

## DEG / RNA-seq Workflow

1. preserve comparison semantics exactly as the user described them
2. preserve directionality
   - clarify what positive `log2FoldChange` means if needed
3. preserve formula and covariate context when given
4. inspect ambiguous workbooks or tabular inputs first with `gleskit_inspect_tabular_data`
5. if the user has uploaded raw counts or a count matrix, pass the upload path to `gleskit_analyze_deseq2_counts`
6. if counts and metadata need explicit alignment, filtering, or count-like transformation first, use `gleskit_prepare_rnaseq_inputs`
7. if `gleskit_prepare_rnaseq_inputs` produced a manifest, pass it into `gleskit_analyze_deseq2_counts` with `prepared_manifest_path`
8. if a local DESeq2 result path exists, pass it to `gleskit_analyze_rnaseq_result`
9. if only a small table snippet exists, use inline table text
10. interpret upregulated and downregulated programs separately when useful
11. connect results to:
   - pathway enrichment
   - whole-signature disease enrichment when available
   - disease themes
   - literature support
12. if the user has a DEG workbook or wants shared/unique DEG reasoning across multiple contrasts, use `gleskit_compare_differential_expression_results`
13. if the user has a single-cell `.h5ad` object or cell-type DEG workbook, use `gleskit_analyze_single_cell_rnaseq`
14. if the user has a gene-level expression matrix and wants PCA, clustering, correlation, supervised modeling, held-out cluster agreement, or any chained preprocessing/statistics workflow, route to `references/30_pipeline_mode.md`
15. for chained workflows, prefer `gleskit_run_pipeline`; for a single preprocessing step, prefer `gleskit_run_log_transform`; for a single matrix statistic step, use `gleskit_run_expression_statistics` or the matching direct `gleskit_run_expression_*` tool
16. when using `gleskit_run_log_transform`, prefer an explicit preset (`custom`, `log2`, `log2p1`, `log10`, `log10p1`, `ln`, `lnp1`) instead of inventing a new synonym
17. for transformed matrices, reuse the returned `derived_inputs.matrix_path` or the saved `artifact_...` selector in downstream matrix tools instead of manually revisiting the raw matrix
17. when using `gleskit_run_expression_pca`, prefer the orientation where samples are the observation axis and genes/features are the feature axis; if the selected observation axis is larger than the feature axis, switch axes instead of forcing it unless the user explicitly wants that orientation
18. when the user asks for classification, regression, split-aware label prediction, or model comparison on one matrix plus metadata, use `gleskit_run_supervised_expression_model`
19. when the user asks for held-out test agreement after train/test splitting, use `gleskit_evaluate_cluster_agreement`
20. when the user asks for cohort-wide consistency or stability across all samples in the matrix, use `gleskit_run_expression_consensus_clustering` and answer from its stability outputs rather than from held-out agreement
21. for matrix clustering on gene-expression data, `metric` usually means clustering distance and should normally be `correlation`; do not substitute agreement words such as `accuracy`
22. if a clustering tool returns `derived_inputs.cluster_labels_path`, pass that path into `gleskit_cluster_marker_analysis` instead of reopening a bulky clustering preview to recover assignments
23. for Reactome-specific cluster-marker questions, prefer `enrichment_database="REAC"`
24. if `gleskit_cluster_marker_analysis` already returns a `question_ready` block with the needed cluster/pathway summary, finalize from that block
25. pass path-aware inputs as the original absolute path, `upload_...`, or reusable `artifact_...` selector; do not reconstruct workspace storage paths manually
26. if the user explicitly prefixes the turn with `/pipeline`, treat it as a hard instruction to plan and execute sequentially rather than asking for confirmation

### Exact Contrast And Filter Contract

Before a count-based contrast, inspect metadata when the condition labels are not already explicit. Copy the exact
visible level strings into `condition_a` and `condition_b`; semantic substitutions are not safe because a dataset may
contain both `DMSO` and `Untreated`. If a question names `DMSO`, use `condition_b="DMSO"` when that exact level is
present. For a question with no absolute LFC cutoff, pass `log2_fold_change_threshold=0.0` explicitly. A stated
`p`, adjusted `p`, `baseMean`, or LFC filter must be mapped to its matching DESeq2 parameter and not replaced by a
different filter.

For matrix statistics, make the observation unit explicit. Use `pairwise_log2_fold_change` only for questions that
ask for ANOVA over pairwise log2-fold-change distributions; use `matrix_log2_fold_change_distribution` for shape or
skewness labels; use `featurewise` for per-feature tests; and use `flattened_values` only when pooling all matrix
entries is the stated estimand.

For named gseapy/Enrichr libraries, pass the exact `library` with `backend="enrichr"` and verify the returned
provider identity. Do not describe `database="GO_BP"` or `database="REAC"` results as gseapy/Enrichr.

When the input comes from uploads or cohorts, inspect the structured summary first:

- use the reported `sample_count`, `gene_count`, and `validation_status` to sanity-check the data
- use `technical.path` for the downstream tool call
- use the metadata profile to confirm the likely sample column, group column, and design formula before DESeq2

Canonical RNA-seq route when the source is messy or ambiguous:

1. `gleskit_inspect_tabular_data`
2. `gleskit_prepare_rnaseq_inputs` when alignment/filtering/transformation is needed
3. `gleskit_analyze_deseq2_counts` or `gleskit_run_expression_statistics`
4. interpret and finalize from the compact answer-bearing fields rather than reopening files

When you explain upload-backed omics data or analysis results, use markdown sections:

- `Overview`
- `Data summary`
- `Metadata / design`
- `Analysis result`
- `Technical details`

Keep file paths and ids out of the main narrative unless the user asked for technical details.

When the user needs help choosing a DESeq2 contrast from metadata:

- present a short ranked shortlist of likely contrast variables
- explain the biological meaning of each option in plain language
- separate candidate contrasts from covariates and QC-only fields
- do not dump the full metadata column inventory into the answer body
- if the user is unsure, ask them to choose from the shortlist instead of listing everything
- if a provider schema or cohort guide exists, use it to rank the shortlist before answering

Current behavior to remember:

- `gleskit_analyze_rnaseq_result` can now return:
  - local pathway enrichment for the upregulated and downregulated signatures
  - ranked GSEA for the whole signature when the local backend is available
  - whole-signature disease enrichment when the local disease backend is available
  - lead-gene disease follow-up and literature support

- `gleskit_analyze_deseq2_counts` can now:
  - run DESeq2 directly from an uploaded counts matrix
  - accept `.xlsx` counts or metadata files by sheet name
  - apply `lfc_shrinkage="apeglm"` when the question requires shrinkage-aware effect sizes
  - save DESeq2 results and QC outputs as session artifacts
  - return an enrichment-ready ranked signature for downstream analysis

- `gleskit_enrich_gene_set` can now:
  - accept explicit database choices such as `GO_BP`, `REAC`, `KEGG`, or `WP`
  - accept benchmark-style aliases such as `GO_BIOLOGICAL_PROCESS_2021`, `REACTOME_2022`, `KEGG_2019_MOUSE`, and `WIKIPATHWAYS_2019_MOUSE`
  - use `background_genes` when the method/database supports a universe
  - simplify GO results with `simplify_go_similarity`

If the user already has a ranked gene-score list but not a full DEG table:

- prefer `gleskit_ranked_gene_set_gsea`
- do not force the RNA-seq workflow when the ranked-signature tool is the better fit

## Important Boundary

GLESKit currently interprets a differential-expression results table.

It does not automatically verify whether:

- the DESeq2 formula was statistically appropriate
- normalization was correct
- batch handling was sufficient
- the contrast specification was valid

So if the user says the table came from their own DESeq2 workflow, interpret the biology but do not pretend GLESKit validated the upstream statistics.

## Large Files

If the table is large:

- prefer `result_path`
- do not ask the user to paste the whole thing into chat

If the input is a large raw counts matrix:

- prefer `gleskit_analyze_deseq2_counts` on the upload path
- keep the raw counts as a session upload instead of pasting them into chat

If the input is a large gene-level expression matrix and the user wants matrix statistics:

- prefer `gleskit_run_pipeline` for chained workflows
- use `gleskit_run_log_transform` or the matching `gleskit_run_expression_*` tool for a single step
- if the user asks for a log transform, know that `custom` means "use the provided base and offset", while the other preset names are fixed contracts
- keep the matrix as a session upload instead of pasting it into chat
- if a GLESKit matrix/omics tool fails, correct the GLESKit call or report the GLESKit limitation; do not replace the analysis with shell, Python, or R

For continuous/qPCR matrices:

- use `gleskit_run_expression_statistics`, not DESeq2;
- use `matrix_featurewise_group_test` for per-feature p-values and BH/BY/Bonferroni counts;
- use `value_transform="log2"` only when explicitly requested;
- use `sample_group_map` for verified prefix-number sample labels when no metadata table exists;
- stop after repeated non-retryable alignment errors rather than guessing headers or labels.

For composite DESeq2 questions involving multiple strata:

- use `gleskit_analyze_stratified_deseq2`;
- pass exact target/reference strata from inspection;
- do not encode a set of metadata levels as a new literal level.
- use `analysis="focus_gene_lfc"` for a requested gene, `analysis="significant_gene_count"` for an adjusted-DE count,
  and `analysis="annotation_group_lfc_test"` for an annotation-class LFC comparison; reserve the default mode for a
  target-versus-reference distribution test.

## Multi-Omics Framing

When the user is doing a broader multi-omics study:

1. keep each modality explicit
2. identify the shared entities:
   - genes
   - pathways
   - diseases
   - targets
3. integrate at the gene/pathway layer first
4. keep cross-modal claims cautious unless multiple modalities clearly converge

## Omics Interpretation Language

Good:

- "This DE result supports an upregulated interferon program and a weaker cell-cycle suppression signal."
- "The RNA-seq result is consistent with this pathway story, but it does not by itself prove mechanism."

Avoid:

- "This table proves the causal mechanism."
