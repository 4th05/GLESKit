# Uploads Mode

Use this file when the user is working with files they want to provide to GLESKit through the workspace.

This file is about session-scoped input handling, not about choosing biological conclusions.

Before any path-aware GLESKit analysis call, also read `references/12_tool_call_contract.md`.

## Core Principle

Uploads are user-provided inputs. They are session-scoped, and they are not the same thing as agent-generated artifacts.

That means Claude must not:

- create uploads from MCP on its own
- treat agent-generated files as uploads
- silently duplicate large local files if a linked path is enough

## When To Use Uploads

Use the Uploads tab when the user wants to:

- register a file for a session
- inspect a user-supplied input before analysis
- link an existing local path without copying it
- reuse the same input across later path-aware GLESKit tools
- pass a raw counts matrix into `gleskit_analyze_deseq2_counts`
- pass a single-cell `.h5ad` object into `analyze_single_cell_rnaseq`
- pass a gene-level expression matrix into `gleskit_run_pipeline` for chained workflows, or a matching `gleskit_run_log_transform` / `gleskit_run_expression_*` tool for a single step
- pass a DEG workbook or result table into `gleskit_analyze_rnaseq_result`, `gleskit_compare_differential_expression_results`, or `gleskit_run_expression_statistics` when the question is about genes, DEG overlap, or RNA statistics/QC
- if you use `gleskit_run_log_transform`, choose one of the fixed presets (`custom`, `log2`, `log2p1`, `log10`, `log10p1`, `ln`, `lnp1`) and use `custom` when you want to control base and offset manually
- if the user prefixes the message with `/data`, treat the following technical ids as the authoritative input data for that turn and preserve them for downstream tool calls

When inspecting an upload, prefer the structured summary first:

- `summary_block` / `inspection` for the human-readable shape, sample count, gene count, and validation state
- `technical.path` for the actual file path needed by downstream tools

Present upload summaries in markdown rather than a single paragraph:

- start with a compact overview or table
- explain the sample/gene counts and metadata in plain language
- keep paths, upload ids, and provenance in a separate `Technical details` section
- use `<details>` for technical details when helpful

If the upload contains metadata and the user needs help choosing a contrast:

- show only the top few plausible contrast candidates
- describe each candidate by biological role, not by raw column dump
- treat the rest of the metadata columns as covariates, QC fields, or technical fields unless there is a strong reason otherwise
- keep the full metadata column inventory hidden unless the user explicitly asks for it
- if a provider-specific schema is available, use it to rank the shortlist before responding

## Allowed Upload Actions

When explicitly requested and workspace is available:

- `list_session_uploads`
- `get_upload`
- `delete_session_uploads`

If the user asks for the current uploads in a session, start with `list_session_uploads` and not `list_session_artifacts`.

## Recommended Workflow

1. inspect the session uploads first
2. use the upload preview to confirm the file looks correct
3. if the upload is copied into GLESKit storage, use the managed stored path
4. if the upload is linked by path, use the original absolute path
5. when a path-aware GLESKit analysis tool accepts `upload_...` or reusable `artifact_...` selectors directly, prefer passing that selector directly
6. for raw counts matrices, pass the technical path or selector to `gleskit_analyze_deseq2_counts`
7. for gene-level expression matrices, pass the technical path or selector to `gleskit_run_pipeline` for chained workflows or the matching direct matrix tool
8. if the user used `/data`, use those input ids and resolve them to the technical paths before analysis
9. pass the path to the appropriate other path-aware GLESKit tool when relevant
10. keep the analysis pipeline path-based instead of pasting large file contents into chat

Do not manually reconstruct workspace storage paths in chat.

If a GLESKit tool fails on an upload-backed analysis:

1. correct the selector or arguments and retry the GLESKit tool
2. do not replace the GLESKit analysis with shell, Python, or R

## Upload vs Artifact

Use this simple rule:

- uploads = user-supplied input files
- artifacts = files produced by GLESKit or the assistant

If the agent creates a file, save it as an artifact, not as an upload.

## Deletion Safety

Always use preview-first deletion for uploads too:

1. list the uploads
2. confirm the target upload ids
3. delete only after explicit user request

## Good Response Pattern

- "I can see the uploaded count table in the session. I’ll pass its path to GLESKit rather than asking you to paste it."
- "This large TSV is better linked than copied, so we should keep it as a session upload and use the path directly."
