# RepoBench Literature And Provenance Rules

Ground the report in visible, reader-facing evidence.

Rules:

- support the main claims with visible sources
- prefer human-readable sources over opaque internal ids
- avoid unsupported causal or clinical overreach
- if evidence is limited, say so in the report instead of inventing certainty
- keep provenance useful for a reviewer who wants to inspect the saved report directly
- do not rely on the UI tool trace as a substitute for visible provenance in the saved report
- every substantive section, paragraph, ranked block, or interpretation block that depends on retrieved evidence must carry a visible `Sources:` line immediately after it

Good provenance signals:

- named resources
- direct source links when available
- concise attribution near the supported claim
- explicit caveats when the evidence is suggestive rather than definitive
- artifact or figure links when available

Visible `Sources:` rule:

1. Put `Sources:` immediately after the content it supports.
2. Prefer provider or source collection names first.
3. Then include clickable paper titles, DOI links, PubMed links, figure links, or artifact links when available.
4. If you cite a retrieved paper title, keep the exact title returned by the tool output. Do not rewrite or shorten the title while keeping the same link.
5. Keep query strings, tool names, and internal ids as secondary traceability only.

Interpretation rule:

- If a statement is a synthesis, caveat, ranking judgment, or validation recommendation that goes beyond direct retrieval, label it clearly as interpretation, caveat, or recommended validation.
- Do not present expert synthesis as though it were a directly retrieved fact.
- If support for a claim is only partial, visibly soften it instead of presenting it as settled.
- If support is weak or unsupported, remove the claim or recast it as caveat, interpretation, or recommended validation.
