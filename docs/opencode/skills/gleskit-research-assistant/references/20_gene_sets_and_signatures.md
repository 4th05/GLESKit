# Gene Sets And Signatures

Use this file for:

- gene lists
- marker panels
- protein sets
- biomarker candidates
- pathway-like signatures
- mechanism questions tied to a set of genes

## Mandatory Trigger

If the turn includes gene-set, marker-panel, or signature interpretation, this reference must be read before final response.

Cross-trigger rule:

- if the same turn also asks for papers, citations, or literature support, you must also read `references/40_literature_and_provenance.md` before using `search_papers`
- for multi-gene literature retrieval without explicit per-gene request, apply the grouped OR query strategy from `references/40_literature_and_provenance.md` instead of one-gene-at-a-time loops

## Core Rule

Treat a pasted or provided set as biologically unlabeled until GLESKit evidence supports a label.

If you have an initial guess, state it only as an unverified guess.

## Preferred Workflow

1. do not call `resolve_gene` once per gene for an ordinary set analysis
2. use `resolve_gene_set` only if the user actually wants a batch normalization audit
3. otherwise use:
   - `investigate_gene_set` for the broad first-pass integrated workflow
   - `enrich_gene_set` for direct pathway enrichment
   - `gene_set_disease_context` when the user wants disease themes from one human gene set
   - `compare_gene_programs` when the user provides multiple named programs or gene sets and wants pathway/program comparison
   - `search_papers`
4. summarize the likely interpretation only after GLESKit returns evidence
5. if multiple plausible interpretations remain, say so
6. if you used a set-level tool such as `investigate_gene_set`, `enrich_gene_set`, or `gene_set_disease_context`, you must expose the set-level sources visibly in the final answer
7. if only set-level provenance is available, that is still sufficient to require visible attribution; do not omit references just because they are not per-gene
8. when only set-level tools were used, include a visible `Sources:` line or `Sources` section that names the returned provider, source collection, citation, artifact link, figure link, or query context when available
9. do not present per-gene bullets as tool-backed unless a per-gene tool was actually used for those gene-level claims
10. if you choose to include unsourced gene-level background context, label it clearly as general background and keep tool-backed set-level conclusions separate

For set-driven reports:

- prefer source names like `Reactome`, `GO Biological Process`, `Europe PMC`, `Open Targets`, `GTEx`, or `Expression Atlas`
- if a GLESKit figure or artifact link exists, expose it directly in the report body
- keep tool names secondary unless they help reproducibility or audit

## Disease Themes For A Whole Gene Set

When the user asks which diseases, phenotypes, or disease programs fit a single gene set:

1. prefer `gene_set_disease_context`
2. treat the result as set-level disease enrichment, not as proof of causality
3. when useful, connect it back to:
   - `investigate_gene_set`
   - `gene_disease_associations` for a few lead genes
4. be explicit that GLESKit still does not have a dedicated multi-set disease-comparison tool

If the user wants disease comparison across several sets:

1. say GLESKit does not yet have a direct multi-set disease-comparison tool
2. if helpful, run separate `gene_set_disease_context` analyses per set and compare them cautiously in prose
3. do not describe that workaround as a native comparison tool

## Ranked Signatures And Multiple Programs

When the user provides:

- a ranked list of genes with scores
- several named programs or modules

prefer:

- `ranked_gene_set_gsea` for one ranked signature
- `compare_gene_programs` for several named gene sets

Important boundary:

- `compare_gene_programs` compares pathway/program enrichment
- it does not directly compare disease themes

## Mechanistic Interpretation

When the user wants mechanism:

1. separate observations from hypotheses
2. state the best-supported processes first
3. then discuss candidate mechanisms
4. explicitly note what would strengthen or falsify the leading hypothesis

## Biomarker-Oriented Interpretation

When the user asks if a panel is a plausible biomarker:

focus on:

- coherence of the panel
- biological specificity
- confounders
- disease relevance
- translational plausibility
- validation needs

## Target Prioritization

When ranking candidate genes or proteins:

focus on:

- evidence strength
- disease relevance
- pathway context
- interaction/network position when available
- therapeutic plausibility and risk

## Good Response Pattern

- "This set may relate to immune activation, but I should verify that with GLESKit first."
- use GLESKit
- "GLESKit enrichment supports an interferon and inflammatory response interpretation, though some overlap with general stress signaling remains possible."
- "Sources: investigate_gene_set; pathway sources GO:BP, REAC; Europe PMC literature query/results; GLESKit evidence bundle."
