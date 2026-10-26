# Reports And Notes

Use this file when the user asks for:

- a report
- a saved summary
- a note
- a session document

## Mandatory Trigger

If the turn includes report or note drafting, template decisions, report revisions, or save/update operations for authored documents, this reference must be read before final response.

## Reports

If the user asks for a report:

1. do not save it as a note
2. determine whether the user explicitly chose a report type
3. if not, ask before drafting or saving
4. load available templates with `gleskit_list_report_templates`
5. load the chosen template with `gleskit_get_report_template`
6. write the report from GLESKit evidence, not memory
7. stop if essential evidence is missing
8. save with `gleskit_create_session_report`
9. keep prose readable, but ground each major conclusion with explicit source cues
10. when relevant artifact ids are available, include them in `linked_artifact_ids`

## Scientific Report Provenance Contract

Saved reports are user-facing scientific documents, not internal tool traces.

For every substantive section, ranked item, or interpretation block in a report:

1. include a visible `Sources:` line immediately after the supported content
2. prefer provider or source collection names first
3. expose clickable paper links when literature is used
4. expose artifact or figure links when they exist
5. keep opaque internal ids as secondary traceability only

If a paper URL, figure path, or saved artifact path exists, format it as a markdown link instead of plain text.

Preferred visible source order in reports:

1. provider or source collection names such as `Reactome`, `GO Biological Process`, `Europe PMC`, `Open Targets`, `GTEx`, or `Expression Atlas`
2. clickable paper titles, DOI links, or PubMed links when literature is cited
3. figure download links, artifact links, or report links when available
4. internal ids or tool names only when they help audit or reproduce the result

Avoid report provenance that looks like an internal debugging surface.

Bad pattern:

- `Sources: investigate_gene_set; artifact_cf7e45...`

Better pattern:

- `Sources: Reactome and GO Biological Process enrichment; Europe PMC literature search; [enrichment figure](...)`

## Figures In Saved Reports

If a GLESKit figure is relevant to the report:

1. embed it in the markdown report when the report surface supports embedded markdown images
2. include a short caption describing what the figure shows
3. include a visible provenance line stating which evidence stream or artifact the figure was built from
4. expose the figure download link when available

Preferred figure provenance pattern:

- `Figure source: built from GLESKit enrichment output using Reactome and GO Biological Process terms; rendered from saved figure artifact [link].`

## Literature In Saved Reports

When a report uses retrieved papers:

1. use human-readable citations
2. make paper references clickable whenever possible
3. do not rely on raw DOI, PMID, PMCID, or opaque paper ids alone when a readable title or link is available
4. keep tool names secondary to provider names and citation links

Multiple reports may coexist in the same session when they represent different:

- analytical angles
- audiences
- study questions
- report styles

Examples of valid report angles now include:

- mechanism
- biomarker
- disease context
- target prioritization
- confounder disambiguation
- coverage-first synthesis

## Directional Gene Markup

When a report visually distinguishes positive and negative differential signal,
the plain-text meaning must remain visible outside the GLESKit desktop app.

Use the portable inline-HTML forms below, with a nearby direction label that
states the comparison semantics:

- positive / increased signal: `<span style="color:#B42318">↑ GENE</span>`
- negative / decreased signal: `<span style="color:#175CD3">↓ GENE</span>`

These forms render colour in Markdown implementations that allow inline HTML.
In sanitizing renderers they remain interpretable because the arrow and gene
symbol are literal text. Do not use colour as the only direction cue, and do
not describe a signal as condition-specific unless the source declares the
contrast orientation. The legacy `[[up:GENE]]` and `[[down:GENE]]` syntax is
desktop-compatible but must not be used in new saved reports intended for
external Markdown rendering.

## Notes

Use notes for:

- short stored interpretations
- hypotheses
- follow-up ideas
- concise saved rationale
- assistant-organized session summaries

Use report tools for full reports and note tools for notes.

## Visuals In Reports

When useful, reports may include:

- data-backed charts generated from GLESKit outputs
- dashboards summarizing multiple evidence streams
- conceptual flowcharts explaining mechanism or workflow

If a report uses both:

- label data-backed visuals clearly
- label conceptual visuals clearly

## Missing Evidence Rule

If a report section cannot be filled credibly from current evidence:

1. stop before saving
2. explain what is missing
3. suggest the next GLESKit retrieval or analysis step
