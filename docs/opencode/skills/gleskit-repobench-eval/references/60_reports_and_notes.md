# RepoBench Reports And Notes Rules

The requested report type and template are fixed by the task.

Workflow:

1. confirm or load the requested template when needed
2. gather only enough evidence to write a grounded report
3. draft the report in the requested template family
4. save it with `gleskit_create_session_report`
5. if necessary, perform one focused revision with `gleskit_update_session_report`
6. verify the final artifact with `gleskit_get_session_report`
7. inspect `citation_audit` and fix title mismatches, missing exact paper titles, or cited-but-unlinked artifacts before you stop

Do:

- keep the report aligned with the requested template category
- include useful caveats
- include visible provenance
- include a visible `Sources:` line after every substantive section, ranked block, or interpretation block
- carry the major evidence artifacts into `linked_artifact_ids` when saving the report
- state factual claims directly only when retrieved evidence supports them and an appropriate visible `Sources:` line follows; otherwise remove the claim or label it as a caveat, interpretation, or recommended validation
- stop once a good saved report exists

Do not:

- create multiple speculative report branches
- save an empty placeholder report
- paste the whole report in chat instead of saving it
- rely on the tool trace alone to prove grounding

Section-level provenance contract:

1. Every substantive section should be readable on its own and should tell the reviewer where the support came from.
2. The preferred visible pattern is:
   - section content
   - `Sources: ...`
   - optional `Interpretation:` or `Recommended validation:` line when the section contains synthesis beyond direct retrieval
3. Prefer provider names, paper titles, direct links, figure links, and artifact links before raw ids.
4. When a paper URL, figure path, or saved artifact path exists, expose it as a markdown link instead of leaving the source as plain text.
5. Avoid provenance that reads like an internal debug surface.
