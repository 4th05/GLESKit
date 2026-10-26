# Matrix Pipeline Appendix

This appendix supports `references/30_pipeline_mode.md`.

Also read `references/12_tool_call_contract.md` before calling a matrix tool or retrying after a matrix-tool error.

## Core Rules

- `run_log_transform` is elementwise and does not change matrix orientation.
- Use `run_log_transform` only when the user explicitly wants a transformed matrix.
- Use `run_pipeline` when the user wants preprocessing plus one or more downstream matrix steps.
- Use the matching direct `run_expression_*` tool when the user wants a single matrix statistic only.
- Pass `analysis_axis="columns"` when samples are in the matrix columns, and `analysis_axis="rows"` when samples are in the matrix rows.
- Pass the original absolute path, `upload_...`, or reusable `artifact_...` selector directly when the tool supports it.
- If a GLESKit matrix tool fails, correct the GLESKit call or report the GLESKit limitation; do not replace it with shell, Python, or R.

## Orientation And Step Order

- Keep row and column identities explicit throughout the pipeline.
- If the source table is transposed relative to the desired step input, correct that before or during matrix loading.
- For PCA, clustering, and correlation, make sure the chosen `analysis_axis` matches the entities you want to compare.
- For PCA, if the selected observation axis is larger than the feature axis, treat that as a likely transpose/orientation issue and either switch `analysis_axis` or set `force=true` only when the orientation is intentional.
- Preserve sample and gene labels in summaries and provenance so later steps can reuse the same matrix cleanly.

## Practical Workflow

1. load the matrix from the upload path or local path
2. apply `run_log_transform` if the user asked for a log-transformed matrix
3. run PCA, clustering, or correlation on the transformed matrix if needed
4. save each step as a workspace artifact

## Boundary

- This appendix is for gene-level matrix workflows only.
- It is not a replacement for DESeq2, enrichment, or literature follow-up guidance.
