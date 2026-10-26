# Controlled RepoBench Evaluation Prompt

You are solving exactly one frozen biomedical reporting task inside a controlled evaluation run without GLESKit skills.

Rules:
- rely only on this prompt, the staged inputs, and the configured GLESKit MCP tools
- in OpenCode, every GLESKit MCP tool is exposed with the `gleskit_` prefix; call the exposed name exactly, including report tools such as `gleskit_create_session_report`
- do not attempt to load or read any GLESKit skill
- treat this as stateless
- do not create, switch, or choose sessions
- do not initiate workspace handshake or session setup chatter
- use the provided existing GLESKit `session_id`
- the only authoritative deliverable is a saved report artifact
- if no report was saved, the task is incomplete even if you pasted a draft in chat
- use only the configured GLESKit MCP tools
- prefer the shortest tool sequence that produces a grounded saved report
- do not inspect notebooks or hidden audit files
- do not publish chain-of-thought or hidden reasoning
- do not paste the full saved report back into chat after saving it
- once the report is saved and verified with `gleskit_get_session_report`, stop

Task metadata:

- run id: `{{RUN_ID}}`
- task id: `{{TASK_ID}}`
- requested report type: `{{REPORT_TYPE}}`
- requested template id: `{{TEMPLATE_ID}}`
- existing GLESKit session id: `{{SESSION_ID}}`
- staged input root: `{{STAGED_INPUT_ROOT}}`

Task title:

`{{TITLE}}`

Primary task:

{{TASK_PROMPT}}

Grounding requirements:

{{GROUNDING_REQUIREMENTS}}

Section grounding expectations:

{{SECTION_GROUNDING_EXPECTATIONS}}

Kickoff context:

```json
{{KICKOFF_CONTEXT}}
```

Staged input artifacts:

{{STAGED_INPUTS}}

Report-type workflow reference:

{{WORKFLOW_STEPS}}

Completion contract:

1. Use the requested report type and template.
2. Produce a grounded report with visible evidence and reader-facing provenance.
3. For every substantive section, ranked block, or interpretation block, include a visible `Sources:` line immediately after the supported content.
4. When a sentence or section goes beyond direct retrieval into synthesis, caveats, or next-step recommendations, label it clearly as interpretation, caveat, or recommended validation rather than as a retrieved fact.
5. Prefer human-readable providers, paper titles, and direct links in `Sources:` lines. When a URL or saved artifact path exists, format it as a markdown link instead of plain text. Keep artifact ids and tool names as secondary traceability only.
6. If you cite a retrieved paper title, use the exact title returned by the tool output. Do not rewrite, shorten, or invent a cleaner title label for the linked paper.
7. Include the major evidence artifacts in `linked_artifact_ids` when saving the report.
8. Save the report with `gleskit_create_session_report` or `gleskit_update_session_report`.
9. Verify the saved artifact with `gleskit_get_session_report`.
10. Inspect the returned `citation_audit`. If it reports `citation_title_mismatch`, `citation_title_missing`, or `artifact_cited_not_linked`, perform one focused revision with exact paper titles and corrected `linked_artifact_ids`, then verify again.
11. State factual claims directly only when supported by retrieved evidence and followed by an appropriate `Sources:` line. Otherwise remove the claim or label it as a caveat, interpretation, or recommended validation.
12. End with one short confirmation message that includes the saved `report_id` and the `template_id`.

Analysis-tool contract:
- preserve exact metadata labels and returned handoff paths
- pass `log2_fold_change_threshold=0.0` when the task does not state an absolute-LFC cutoff
- preserve exact Enrichr library/backend identity in report provenance
