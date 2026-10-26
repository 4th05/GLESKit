# Tool Call Contract

Use this file whenever you are about to call a GLESKit tool, translate user phrasing into GLESKit arguments, recover from a GLESKit tool error, or decide whether to retry versus stop.

This file is the canonical contract for:

- what to pass
- what not to pass
- when to use direct tools versus `run_pipeline`
- how to handle uploads, artifact ids, and local paths
- what to do after validation errors, timeouts, or backend failures

## Non-Negotiable Rules

- Never invent a tool name.
- Never invent an argument name.
- Use only arguments that exist in the current GLESKit tool schema.
- In OpenCode, call GLESKit MCP tools with the exposed `gleskit_` prefix, for example `gleskit_inspect_tabular_data`.
- If a canonical GLESKit analysis tool exists for the requested analysis, do not re-implement that analysis in shell, Python, or R after a GLESKit tool failure.
- Shell may be used only for lightweight inspection or debugging of inputs, not as a substitute execution path for a canonical GLESKit analysis result.
- If a GLESKit tool fails, either:
  - correct the GLESKit call and retry, or
  - stop and explain the GLESKit limitation clearly.

## Selector Contract

Path-aware GLESKit analysis tools may receive:

- an absolute local file path
- a linked local file path
- a GLESKit upload selector such as `upload_...`
- a GLESKit artifact selector such as `artifact_...` when that artifact exposes a reusable file path

Prefer this order:

1. if the user gave an absolute path, pass it directly
2. if the user gave `/data upload_...` or `/data artifact_...`, pass that selector directly to the path-aware GLESKit tool
3. if you inspected an upload and already have `technical.path`, you may pass that path directly

Do not manually reconstruct workspace storage paths in chat.

If a path-aware tool accepts a selector directly, prefer the selector over copying the internal path into the prompt.

## Inspection-First Contract

When a workbook or tabular input is ambiguous:

1. call `gleskit_inspect_tabular_data` first
2. use the returned `sheet_roles`, `candidate_sample_column`, `candidate_group_columns`, `candidate_group_levels`, `candidate_sample_preview`, `matrix_sample_preview`, `candidate_gene_id_columns`, `matrix_orientation`, `candidate_analysis_families`, and `ambiguities`
3. choose the downstream route from those facts rather than from filename or question wording alone

If the inspection result shows mixed matrix and metadata sheets or unresolved alignment issues, use `gleskit_prepare_rnaseq_inputs` before DESeq2.

If `gleskit_prepare_rnaseq_inputs` returns a prepared manifest, pass `prepared_manifest_path` into `gleskit_analyze_deseq2_counts` instead of rebuilding the preprocessing choices in chat.

If a clustering tool returns `derived_inputs.cluster_labels_path`, pass that path into `gleskit_cluster_marker_analysis` instead of reopening a bulky clustering preview to recover assignments.

If `gleskit_cluster_marker_analysis` already returns the needed cluster/pathway answer in `question_ready`, finalize from that block instead of reopening marker-set files.

## Direct Tool Vs Pipeline

Use a direct tool when the user wants exactly one analysis step.

Use `run_pipeline` when:

- the user explicitly says `/pipeline`
- one GLESKit stage should feed another
- the task requires preprocessing plus clustering / PCA / statistics
- the task requires multiple DE or enrichment stages

In a `/pipeline` turn:

- make one `run_pipeline` call only
- do not switch to direct analysis tools mid-turn
- do not continue the same workflow with a second pipeline call

## Matrix Argument Contract

For gene-level matrices:

- `row_id_column` is the identifier column that names the matrix rows, usually `gene`
- `analysis_axis` is the observation axis for the analysis step, not a generic orientation note

Canonical mapping:

- if samples are in the columns of a gene x sample matrix, pass `analysis_axis="columns"`
- if samples are in the rows of a sample x gene matrix, pass `analysis_axis="rows"`
- if genes are in the columns, that implies samples are in the rows, so pass `analysis_axis="rows"`
- if genes are in the rows, that implies samples are in the columns, so pass `analysis_axis="columns"`

Never pass free-text values such as:

- `samples`
- `genes`
- `sample_columns`
- `genes_are_rows`
- prose sentences in the argument slot

Use exact GLESKit argument values:

- `analysis_axis="rows"` or `analysis_axis="columns"`

## Clustering Contract

Use `run_expression_consensus_clustering` for:

- bootstrap consensus clustering
- cohort-wide stability across all observations
- questions that ask how many samples are consistently grouped across the full matrix
- questions whose answer should come from the consensus stability distribution rather than a held-out test split

Use `evaluate_cluster_agreement` when the question asks for:

- held-out test-set agreement after train/test splitting
- logistic regression label transfer/prediction onto the test split
- held-out agreement count or rate for the test split specifically
- explicit test-only agreement rather than cohort-wide bootstrap stability

Do not default to `evaluate_cluster_agreement` just because the wording mentions:

- `70/30`
- `train/test`
- `logistic regression`

if the requested answer is still a cohort-wide count across all observations.

Answer extraction rules:

- for `run_expression_consensus_clustering`, answer cohort-wide consistency questions from `consistency_summary` or `stability`
- for `evaluate_cluster_agreement`, answer held-out questions from `agreement.count_test_only` or `agreement.rate_test_only`
- do not present held-out test agreement as if it were an all-sample cohort-wide consistency count

For clustering tools:

- `cluster_count` is the requested number of clusters
- `iterations` is the bootstrap consensus iteration count
- `metric` is the clustering distance, not an evaluation score
- `linkage` is the hierarchical clustering linkage method

Preferred clustering metric for gene-expression matrices:

- `metric="correlation"`

Allowed clustering metric intents:

- `correlation`
- `pearson` meaning correlation distance
- `spearman`
- `euclidean`
- `manhattan`

Do not put downstream agreement words in `metric`, including:

- `accuracy`
- `agreement`
- `classification_accuracy`

Those are outputs or interpretations, not clustering distances.

## Supervised Matrix Contract

Use `run_supervised_expression_model` for:

- classification
- regression
- split-aware label prediction
- group-aware or stratified evaluation

Use exact task-family arguments:

- `task="classification"` or `task="regression"`
- `split_strategy="train_test"`, `split_strategy="stratified_train_test"`, or `split_strategy="group_train_test"`

Do not invent alternate values such as:

- `binary_classification`
- `holdout`
- `kfold`
- `accuracy_split`

unless the tool schema actually exposes them.

## RNA-seq / Counts Contract

Use `analyze_deseq2_counts` when the user has raw counts and sample metadata.

Use `analyze_rnaseq_result` when the user already has a DEG/result table.

For `analyze_deseq2_counts`:

- `counts_path` may be a path or `upload_...`
- `sample_metadata_path` may be a path or `upload_...`
- keep `sample_column`, `condition_column`, `condition_a`, and `condition_b` explicit when the user or metadata make them clear

For `analyze_rnaseq_result`:

- `result_path` may be a path or `upload_...`
- preserve the comparison direction exactly

## Single-Cell Contract

Use `analyze_single_cell_rnaseq` with one task at a time:

- `analysis="summary"`
- `analysis="cell_type_deg_counts"`
- `analysis="gene_cell_type_lookup"`
- `analysis="pseudobulk"`

Do not mix multiple single-cell tasks into one direct call.

If the workflow is pseudobulk followed by DESeq2, use `run_pipeline`.

## Failure Handling Contract

### Validation Or Enum Error

Examples:

- invalid `analysis_axis`
- unsupported `metric`
- unsupported `split_strategy`
- missing required argument

What to do:

1. inspect the exact error
2. correct the GLESKit arguments to the canonical values
3. retry the GLESKit tool
4. only after a corrected retry fails should you explain the limitation

### Selector Or Path Error

Examples:

- `upload_...` not found
- artifact id ambiguous across sessions
- selector resolved to an artifact without a reusable file path

What to do:

1. if the selector came from `/data`, preserve it
2. inspect with `get_upload` or `get_artifact` only when needed
3. retry the GLESKit call with the corrected selector or path
4. if the artifact has no reusable path, say so and keep chained work inside `run_pipeline`

### Timeout Or Backend Runtime Failure

Examples:

- MCP timeout
- a clustering job is too large for synchronous execution
- backend process crash

What to do:

1. do not re-implement the analysis outside GLESKit
2. do not produce an approximate biological answer from shell code
3. explain that the GLESKit backend did not complete the requested computation
4. if the failure is likely scale-related, say that clearly
5. only suggest a lower-cost GLESKit retry if that still answers the user's intended question and the user agrees

### Unknown Internal Error

What to do:

1. do not guess a result
2. do not claim the analysis succeeded
3. report the failed GLESKit tool and the unresolved uncertainty

## Forbidden Fallbacks

After a GLESKit tool failure, do not:

- run a local approximation in shell and present it as the GLESKit answer
- silently switch from `evaluate_cluster_agreement` to a homemade clustering script
- substitute an R or Python notebook computation for a GLESKit tool result
- present shell inspection output as if it were the authoritative GLESKit analysis

## Good Patterns

- "Samples are in the columns" -> `analysis_axis="columns"`
- held-out agreement question -> call `evaluate_cluster_agreement`
- `/pipeline /data upload_abc ...` -> pass `upload_abc` directly into the pipeline source
- invalid enum error -> normalize to canonical GLESKit values and retry once
- timeout on large synchronous clustering -> report GLESKit runtime limitation; do not approximate in shell

## Exact Metadata And Filter Semantics

`gleskit_inspect_tabular_data` is the source of truth for visible metadata values. Treat
`candidate_group_levels`, `candidate_sample_preview`, and `matrix_sample_preview` as exact strings. Preserve case,
punctuation, and underscores in `condition_a`, `condition_b`, `metadata_group_value`, and
`metadata_group_values`; do not map a named control such as `DMSO` to a semantically similar level such as
`Untreated`.

For `gleskit_analyze_deseq2_counts` and `gleskit_analyze_rnaseq_result`:

- `significance_mode="p_value"` goes with a stated raw `p` cutoff; `significance_mode="padj"` goes with adjusted p-values.
- `base_mean_threshold` is a post-result `baseMean` filter; it is not the raw-count `min_count` filter.
- `log2_fold_change_threshold=0.0` means no absolute-LFC filter. Pass a positive threshold only when the question states one.
- Use explicit pairwise contrasts when metadata has more than two levels.
- Use returned `derived_inputs` paths for downstream comparison or enrichment instead of reopening result tables.

## Matrix Observation Units And Enrichment Providers

For `gleskit_run_expression_statistics` with `analysis="matrix_group_test"`, choose `observation_unit` from the
question rather than accepting a silent default:

- `flattened_values` pools every numeric feature-by-sample entry.
- `featurewise` tests each feature's group observations and returns per-feature results.
- `pairwise_log2_fold_change` computes group-mean log2 ratios for every selected group pair, using `pseudocount > 0`, and then tests those pairwise distributions.

Use `analysis="matrix_log2_fold_change_distribution"` for all-pairwise distribution shape or skewness questions,
not ANOVA. For `gleskit_enrich_gene_set`, `database="GO_BP"` and `database="REAC"` select the generic local
backends. Named gseapy/Enrichr libraries require the exact `library` plus `backend="enrichr"`; that explicit route
does not silently become a local clusterProfiler result. Confirm `library`, `backend`, and `top_term`/`top_terms`
in the returned payload before describing the result.
