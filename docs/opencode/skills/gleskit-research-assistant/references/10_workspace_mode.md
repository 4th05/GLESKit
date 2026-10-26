# Workspace Mode

Use this file when the user is working with saved GLESKit state.

For user-provided input files, also consult [11_uploads_mode.md](11_uploads_mode.md).

This file is for persistence and saved-state actions only.

Do not use it to decide which analysis tool to call once the user has moved on to RNA, gene, matrix, single-cell, enrichment, or literature work.

## Workspace Structure

GLESKit workspace is organized around a saved `Session`.

A session is the container for one study or work thread. It can hold:

- `Artifacts`: generated outputs from GLESKit or the assistant, such as analysis results, figures, or exported deliverables
- `Uploads`: user-provided input files for the session, either copied into GLESKit storage or linked by path
- `Notes`: lightweight markdown for hypotheses, reminders, and short interpretations
- `Reports`: structured authored outputs for study summaries and deliverables

Session metadata also stores the study title, description, and related saved-state fields.

For input data, the workspace tools should expose both:

- a user-facing summary block with sample count, gene count, data type, and validation state
- a technical block with the file path and provenance needed for downstream tool calls

When you answer about workspace inputs or cohorts, format the response in markdown:

- `Overview`
- `Data summary`
- `Metadata / validation`
- `Next steps`
- `Technical details`

Keep `technical.path`, ids, and provenance out of the main narrative unless the user asks for them.

If the user is asking which metadata column should be used as a contrast or group variable:

- give a short ranked shortlist of the most plausible options
- explain the likely biological meaning of each option in one sentence
- separate true contrast candidates from covariates and QC-only columns
- avoid listing every detected metadata column unless the user explicitly asks for the full inventory
- if the workspace has a provider schema or column guide, use it to rank the shortlist

How to think about the classes:

- `Session` = the study container
- `Artifacts` = outputs created during analysis
- `Uploads` = inputs supplied by the user
- `Notes` = short working memory
- `Reports` = polished authored results

## Core Principle

Saved GLESKit workspace state is user-controlled.

That means Claude must not:

- silently create sessions
- silently switch the active session
- silently delete anything
- imply persistence unless GLESKit confirms workspace availability

## When To Use Workspace Tools

Use workspace tools only for explicit saved-work tasks such as:

- listing sessions
- opening session summaries
- reading notes or reports
- inspecting artifacts
- creating a new session
- renaming a session
- rewriting a session description
- creating a note
- saving a report
- creating a new session from existing artifacts
- deleting sessions, artifacts, or uploads

Do not route pure analysis requests here just because the data happen to live in a session.

## Opening Handshake

At the start of every new chat, before substantive work:

1. check workspace availability and whether a session is already active
2. tell the user that state plainly
3. explicitly ask whether to proceed:
   - with the active session
   - statelessly
   - by connecting or creating a different session when appropriate

Examples:

- "GLESKit workspace is available and a session is currently active. Do you want to continue with that session or work statelessly instead?"
- "GLESKit workspace is available but no session is active. I can still list or create sessions; do you want me to do that, or do you want to work statelessly?"
- "GLESKit workspace is unavailable right now. Do you want to proceed statelessly?"

Do not skip this opening handshake in a new chat, even if the user's first message is already a substantive research request.

Exception: if the user explicitly requests a global workspace action that does not require an active session, such as `list_sessions`, `create_session`, or opening a named session by id, do that action immediately when workspace is available instead of blocking on the handshake.

## Workspace Execution Boundary

When a canonical GLESKit workspace tool exists for the requested action:

- use that canonical GLESKit MCP tool
- do not substitute shell commands, direct CLI invocations, filesystem inspection, or manual reads of workspace files
- if the canonical workspace tool is unavailable in the current chat, say that plainly instead of working around it

## Allowed Workspace Actions

When explicitly requested and workspace is available:

- `list_sessions`
- `get_session_summary`
- `list_session_notes`
- `get_session_note`
- `list_session_reports`
- `get_session_report`
- `list_session_artifacts`
- `get_artifact`
- `list_session_uploads`
- `get_upload`
- `delete_session_uploads`
- `create_session`
- `update_session`
- `create_session_from_artifacts`
- note tools
- report tools
- figure tools
- deletion tools

## Deletion Workflow

Always use a preview-first deletion pattern:

1. list or preview the targets
2. summarize what will be affected
3. ask for confirmation when appropriate
4. only then delete

Use that especially for:

- deleting sessions
- deleting multiple artifacts
- deleting notes or reports

## Session Metadata Updates

If the user asks to improve, rename, or rewrite the current session title or description based on what was learned in the conversation:

- use `update_session`
- preserve the study meaning instead of making the name vague
- keep the revised title concise and recognizable
- keep the revised description specific about the dataset, comparison, mechanism, or study angle when that information is available
- do not rename a session silently; do it only when the user asks

## Notes Versus Reports

`Notes` are lightweight markdown documents for:

- interpretations
- hypotheses
- reminders
- short stored rationale

`Reports` are structured authored outputs for:

- study summaries
- mechanistic reports
- biomarker reports
- audience-specific deliverables

Do not blur those together.

## Uploads

Uploads are user-provided session inputs, not agent-created outputs.

Use uploads when the user wants to supply a file for later path-based analysis, especially when copying a large local file would be wasteful.

Important mapping:

- if the user asks about "uploads", "uploaded files", or "current uploads", use `list_session_uploads`
- do not substitute `list_session_artifacts` for uploads
- uploads are user-provided input files, not agent-generated outputs

Key rules:

- use `list_session_uploads` and `get_upload` to inspect saved inputs
- prefer `summary_block` / `inspection` first, then use `technical.path` for analysis calls
- use `delete_session_uploads` only after explicit user confirmation
- treat copied uploads and linked-path uploads as session-scoped inputs
- if the agent itself creates a file, save it as an artifact instead of an upload
- do not silently create uploads from MCP; uploads originate in the workspace UI

## Session-Aware Language

Good examples:

- "GLESKit workspace is available, but no session is currently active."
- "This result can be saved into the connected session if you want."
- "I can list your saved sessions and help you choose the right one."

Avoid:

- implying results were saved when they were not
- silently changing which session is in focus
- treating uploads as if they were agent-generated artifacts
