# Literature And Provenance

Use this file when the user wants:

- papers
- citation-backed interpretation
- evidence support for claims
- literature-backed reporting

## Mandatory Trigger

If the turn includes any of the following, this reference must be read before final response:

- `search_papers`
- a request to find papers
- citation-backed biological claims
- provenance checks for paper-level evidence

## Provenance Rule

In biomedical and literature-grounded work, use strict provenance mode by default.

Every paper mention must be traceable to:

- a GLESKit tool result actually loaded in the chat
- a saved GLESKit note, report, or artifact actually loaded in the chat
- an explicit web retrieval actually performed in the current chat

When available, treat `gleskit_provenance` metadata as the primary grounding source and reuse:

- `source_tool`
- `providers` / `sources`
- `citations` and `artifact_ids`

Visible attribution is mandatory in the final answer body.

For every section, paragraph, or bullet that relies on retrieved literature or provenance-backed evidence:

- include a short visible citation inline or immediately adjacent to the claim whenever possible, not only in a final bibliography block
- include a visible `Sources:` line immediately after it
- prefer human-readable provider or source names first
- include exact GLESKit tool names only when they add useful secondary traceability
- include citation identifiers, providers, source collections, artifact links, figure links, or query strings when useful
- do not rely on the UI tool trace as a substitute

## Reader-Friendly Provenance Rule

In saved reports and polished answers, provenance should be understandable to a scientist reading the document directly.

Prefer this order in visible `Sources:` lines:

1. provider or source collection names
2. clickable paper titles or DOI/PubMed links
3. artifact or figure links when available
4. query strings or internal ids only as secondary traceability

Avoid user-facing provenance lines that are dominated by opaque internal ids or tool names when better source labels exist.

## Never Invent

Do not invent:

- author names
- author order
- paper titles
- DOI, PMID, or PMCID
- author-year citations

## Citation Rule

Use `(Surname et al., Year)` only when trustworthy author metadata was actually returned, and place it beside the sentence or bullet that depends on that paper.

If author metadata is missing:

- cite by title, journal, DOI, PMID, or PMCID
- do not guess authors

## Link Rendering Rule

When citing retrieved papers, make citations clickable whenever possible.

Preferred link order:

1. direct paper URL from retrieval metadata (for example `papers[].url`)
2. if URL missing and DOI exists, `https://doi.org/<DOI>`
3. if URL and DOI missing but PMID exists, `https://pubmed.ncbi.nlm.nih.gov/<PMID>/`

Do not leave DOI/PMID as bare text when a clickable link can be generated.
Do not leave citations as bare identifiers when a link can be constructed.

For saved reports, prefer a human-friendly citation surface such as:

- paper title linked to the direct URL
- or paper title linked to DOI/PubMed when a direct URL is missing
- optionally followed by journal, year, DOI, PMID, or PMCID

Do not rely on raw ids alone when a readable title or clickable link can be shown.

## Query Construction Strategy

Build paper queries from the user's biological intent, not from a naive one-gene-at-a-time loop.

### Default behavior for multi-gene requests

When the user provides multiple genes and does not explicitly ask for per-gene retrieval:

1. use one context anchor (disease, mechanism, phenotype, drug, or pathway)
2. add one grouped OR clause with representative genes
3. run `search_papers` with that combined query first

Template:

`<context terms> AND (GENE1 OR GENE2 OR ... OR GENEn)`

Example:

`bipolar disorder AND (CLOCK OR BMAL1 OR PER1 OR PER2 OR PER3 OR CRY1 OR CRY2 OR NR1D1 OR NR1D2 OR RORA OR RORB OR RORC OR CSNK1D OR CSNK1E OR BHLHE40 OR BHLHE41 OR TIMELESS OR NPAS2)`

Why this is default:

- it maximizes recall for sparse literature intersections
- it avoids false negatives from strict single-gene loops
- it better matches how user questions are usually framed

### When not to use grouped OR first

Use per-gene queries first only if the user explicitly asks for gene-by-gene evidence comparison.

### Gene selection for grouped OR

1. prioritize user-highlighted genes
2. include central or representative genes from the provided set
3. start with about 12-20 genes in one query when feasible
4. if the list is larger or query quality drops, split into 2-3 OR-grouped queries and merge evidence cautiously

### Iterative refinement loop

1. **Pass 1 (broad):** disease/context anchor + grouped OR gene clause
2. **Pass 2 (focused):** add mechanism or pathway anchor if Pass 1 is too broad
3. **Pass 3 (targeted):** gene-by-gene follow-up only for unresolved high-priority genes

If initial retrieval is sparse:

- relax over-specific mechanism terms
- keep the disease/context anchor and gene OR group
- try a synonym variant of the disease or phenotype term

If initial retrieval is noisy:

- add one mechanism/pathway qualifier
- add species or tissue qualifier when user context requires it

## Literature Workflow

1. answer the user's question directly first; do not begin with a tool-log recap unless the user explicitly asked for the retrieval details
2. construct query with the strategy above
3. retrieve papers with `search_papers`
4. when multiple papers or evidence items are retrieved, summarize them in a compact markdown table before the prose synthesis
5. tie retrieved papers back to the biological question
6. keep retrieved evidence separate from background knowledge
7. clearly label hypotheses that go beyond retrieved evidence
8. when useful, show the exact query string used so the user can inspect or refine it
9. every literature-backed conclusion in the final answer must have a visible `Sources:` line

Table guidance for literature answers:

- use one row per paper when a review or bibliography is requested
- include short columns such as `Paper`, `Main finding`, and `Why it matters`
- keep the prose synthesis short and direct after the table
- do not flatten multiple bullet points into one paragraph; keep each bullet on its own line

## Missing Evidence

If literature support has not yet been retrieved:

1. say so plainly
2. do not fill the gap with memory-based citations
3. suggest the next retrieval step

## If A Provenance Error Is Detected

1. say so plainly
2. withdraw unsupported citation text
3. keep only supportable claims
4. offer to re-retrieve evidence with corrected query strategy
