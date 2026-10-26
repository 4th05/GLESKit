# Visuals And Dashboards

Use this file whenever visuals would improve the answer.

## Mandatory Trigger

If the turn includes figure generation, plot requests, dashboards, visual explanation, `list_figure_options`, or `generate_figure`, this reference must be read before final response.

Cross-trigger rule:

- if the same turn also asks for citation-backed interpretation, paper support, or provenance-linked statements, also read `references/40_literature_and_provenance.md` before finalizing the response

This file assumes that GLESKit-native figures should be used when they exist and that figure-capable GLESKit analyses should auto-attach those figures in the same tool result.

If no suitable GLESKit-native figure exists, the assistant should generate a custom in-chat visual from GLESKit data.

## Default Claude Figure Behavior

In Claude, when the user asks to see a figure, show a figure, build a plot, make a dashboard, or visualize the result, the default behavior is:

1. inspect the structured GLESKit data that are already available or can be retrieved through GLESKit data tools
2. expect GLESKit-native figures to already be attached when the result supports them
3. if no matching GLESKit-native figure is present, decide whether a matching GLESKit-native figure type exists
4. if it exists but was not auto-attached, use `list_figure_options` + `generate_figure` so the figure is rendered from the available source data
5. if no matching GLESKit-native figure exists, generate the visual directly in chat
6. explain only what the underlying GLESKit data support

When the user asks for a specific figure type:

1. call `list_figure_options` first (prefer `figure_source_id` from the upstream tool result)
2. if the requested type is available, call `generate_figure` with that exact `figure_type`
3. do not try to force figure type through free-text fields such as investigation questions

Do not default to this sequence:

1. ask the user whether they want a figure
2. ask what figures are available
3. only after that try to create one manually

That is the wrong default. When GLESKit already has enough data for a native figure, the figure should normally come back automatically with the main result.

In this app, "show me the figure" means one of the following:

- if GLESKit has a native figure for that data, generate it through GLESKit and treat the returned GLESKit tool-result image as the displayed figure
- otherwise, build the figure in chat from GLESKit data

Default expectation:

- enrichment results should normally come with a native GLESKit enrichment bar plot without the user having to ask
- enrichment maps, gene-concept networks, and gene-gene/PPI networks should be requested through `list_figure_options` + `generate_figure` when the user wants pathway relationships, term connectivity, or gene-gene structure
- whole-set disease-enrichment results from `gene_set_disease_context` should normally come with a native bar plot without the user having to ask
- disease-enrichment results can also support disease enrichment maps, disease gene-concept networks, and disease gene-gene/PPI networks through `list_figure_options` + `generate_figure`
- ranked-signature GSEA results from `ranked_gene_set_gsea` should normally come with a native bar plot; use `list_figure_options` + `generate_figure` when the user wants the ranked GSEA running-score plot
- multi-program comparisons from `compare_gene_programs` should normally come with a grouped comparison bar plot; use `list_figure_options` + `generate_figure` when the user wants the compareCluster enrichment map or compareCluster gene-concept network
- expression context results should normally come with a native GLESKit expression bar plot without the user having to ask
- RNA-seq result interpretation should normally come with the fast native figure set by default: volcano plot plus up/down pathway bar plots
- use `list_figure_options` + `generate_figure` for the heavier RNA-seq enrichment maps, RNA-seq gene-concept networks, and RNA-seq gene-gene/PPI networks
- if a requested GLESKit enrichment map is too heavy to render, accept the GLESKit-native gene-concept network fallback instead of treating that as a failure to use GLESKit

Important display rule:

- A GLESKit-native figure returned by `generate_figure` is usually displayed inside the GLESKit tool-result card, not necessarily duplicated inside the assistant's prose bubble.
- If that GLESKit tool-result card visibly contains the image, treat that as a successful display.
- If a GLESKit figure tool result includes one or more `resource_link` entries for file download, surface at least one clickable markdown download link in the assistant response.
- Keep figure download-link rendering enabled in both stateless and session-backed chats.
- Never require an active session to generate a supported figure; session state is only for persistence/reopen flows.
- Never treat local payload paths (`png_path`, `svg_path`, `display_path`, `figure_files`) as chat-download URLs.
- For user-facing download links, use only GLESKit `resource_link` entries or `download_links` metadata.
- If `preferred_download_url` is present, use that first for clickable in-chat/browser downloads.
- Otherwise, if `download_links[].http_url` is present, prefer that HTTP(S) URL.
- Do not say "I displayed it here in my response" unless the image is actually embedded in the assistant message itself.
- Preferred wording after success: "The GLESKit figure is in the tool result above."

Only switch to figure-asset management when the user explicitly asks for one of the following:

- save the figure to GLESKit
- export the figure
- inspect which saved figure artifacts already exist
- use a pre-built GLESKit figure template specifically

Use `generate_figure` when you explicitly need an extra or regenerated native figure from either a stateless `figure_source_id` or a saved artifact. Do not follow that by a separate saved-figure retrieval step just to show the image.

## Render Reliability Rules

When generating visuals in this app:

1. prefer the simplest rendering path that is likely to appear inline in chat
2. prefer one clear chart over a complex interactive construction if reliability is uncertain
3. do not assume that a visualization rendered successfully just because a tool call or widget action returned without an obvious error
4. first distinguish whether the GLESKit tool-result card contains the image
5. if the GLESKit tool-result card contains the image, acknowledge that the figure is present there and do not pretend it is also embedded in assistant prose
6. if the user says they cannot see the figure anywhere, treat the render as failed
7. after a failed render:
   - acknowledge the failure plainly
   - retry once with a simpler in-chat rendering approach or a custom assistant-made visual built from the same GLESKit data
   - do not pivot to saved local GLESKit figure assets as if that solved the request to show the figure in chat
8. only offer saved/exported GLESKit figure artifacts after the in-chat visualization path is exhausted or when the user explicitly asks for persistence/export

The important distinction is:

- `show me the figure` means the user wants to see the figure in the visible chat interface, including the GLESKit tool-result image if the client surfaces it there
- `save or export the figure` means the user wants a GLESKit artifact

Do not confuse those two goals.

## Global Visual Style

Prefer a clean scientific style over a flashy or decorative one.

Default visual principles:

- high contrast
- restrained palette
- simple layout
- minimal clutter
- explicit labels
- short evidence-backed captions

Default styling rules:

1. Use concise titles that say what the chart shows, not marketing language.
2. Label all axes explicitly.
3. Include units when available.
4. Keep legends short.
5. Sort ranked charts intentionally, not arbitrarily.
6. Limit to the most informative terms, tissues, genes, or associations.
7. Avoid 3D plots, donut charts, gauges, and decorative effects.
8. Prefer horizontal layouts for long biological labels.

## Default Color System

Use a consistent scientific palette unless the user explicitly asks for something else.

Recommended defaults:

- neutral / background elements: gray
- positive or increased signal: red
- negative or decreased signal: blue
- primary evidence emphasis: teal or blue
- secondary emphasis: orange
- non-significant / background points: light gray

Suggested color roles:

- `upregulated`: red
- `downregulated`: blue
- `non-significant`: gray
- `pathway/library categories`: blue, teal, orange, green
- `conceptual or explanatory panels`: neutral gray with one accent color

Do not let colors imply biology that is not actually present.

## Two Visual Categories

### 1. Data-backed visuals

These are charts or dashboards generated from structured GLESKit outputs.

Examples:

- enrichment bar plots
- enrichment maps from clusterProfiler/enrichplot when GLESKit supports them
- gene-concept networks from clusterProfiler/enrichplot when GLESKit supports them
- gene-gene/PPI networks from `clusterProfiler::getPPI()` when GLESKit supports them
- disease-enrichment bar plots
- grouped pathway/program comparison bar plots
- compareCluster enrichment maps
- compareCluster gene-concept networks
- ranked disease-association bar plots
- expression context bar plots
- RNA-seq volcano plots
- upregulated/downregulated summary panels
- variant-trait ranking plots
- drug-target indication tables or matrix views
- network summaries when edge data is available

### 2. Conceptual visuals

These are not direct measurements. They explain reasoning or process.

Examples:

- flowcharts
- decision trees
- mechanism maps
- study workflow diagrams
- evidence ladders
- summary dashboards with conceptual callouts

## Core Visual Rule

If structured GLESKit data is available and a visualization would make the answer better, generate one.

Do not wait for the user to ask for a chart by name when a chart would materially improve clarity.

When the user asks what figures are possible, answer in terms of visuals you can generate from the available GLESKit data, not only in terms of stored GLESKit figure templates.

## Visual Selection Rules

Choose the plot that best matches the data structure:

- ranked categories -> horizontal bar chart
- effect size plus significance -> volcano plot
- tissue or sample rankings -> horizontal bar chart
- many related panels -> compact dashboard
- workflow or hypothesis logic -> flowchart
- relationships between entities -> network or matrix

If the data do not support a quantitative chart, use:

- a table
- a dashboard summary
- a conceptual flowchart

instead of inventing numbers.

When both are possible:

- prefer a GLESKit-native figure when one exists and matches the request
- prefer a custom in-chat chart when there is no suitable GLESKit-native figure or the user wants a more conceptual dashboard/flowchart
- prefer separate saved-figure management only when persistence or export is the user's goal

If a complex chart cannot be rendered reliably in chat, fall back in this order:

1. simpler static chart of the same data
2. smaller single-panel chart instead of a dashboard
3. labeled table plus a short interpretation if a chart still cannot be rendered

Do not claim that fallback charts were shown unless they are actually visible in chat.

For GLESKit-native enrichplot graphs, use this more specific order:

1. GLESKit enrichment map when it renders cleanly
2. GLESKit gene-concept network when GLESKit returns it as a native fallback or when the user explicitly asks for term-gene connectivity
3. GLESKit gene-gene/PPI network when the user explicitly asks for gene-gene structure and GLESKit supports it from the saved term-gene inputs
4. custom in-chat graph only when GLESKit does not provide a suitable native graph

## Standard Chart Recipes

Use these recipes whenever the necessary data are available.

### 1. Enrichment Bar Plot

Use this as the default enrichment figure for GLESKit.

Preferred encodings:

- y-axis: enriched terms, sorted by significance
- x-axis: `-log10(adjusted p-value)` or `-log10(p-value)`
- bar color: source/library category or one consistent evidence color

Good defaults:

- top `8-15` terms
- horizontal bars for readability
- optional small source labels at the end of each bar if multiple libraries are mixed

Interpretation guidance:

- longer bars mean stronger enrichment support
- use this when you want the most direct ranked view

Do not suggest enrichment dot plots for GLESKit right now.

### 1b. Disease-Enrichment Bar Plot

Use this for `gene_set_disease_context`.

Preferred encodings:

- y-axis: enriched disease terms, sorted by significance
- x-axis: `-log10(adjusted p-value)` or `-log10(p-value)`
- bar color: one consistent disease-evidence color unless source grouping is clearly supported

Interpretation guidance:

- longer bars mean stronger disease-theme enrichment support for the whole gene set
- this is a set-level disease summary, not a causal proof and not a single-gene evidence score

### 1c. Program-Comparison Bar Plot

Use this for `compare_gene_programs`.

Preferred encodings:

- y-axis: enriched pathway or process terms
- x-axis: `-log10(adjusted p-value)` or `-log10(p-value)`
- grouped bars or colored bars: program/set identity

Interpretation guidance:

- use this to compare pathway/program enrichment across named sets
- do not label this as disease comparison unless the underlying data are actually disease-enrichment results

### 1d. Enrichment Map

Use this when GLESKit exposes a clusterProfiler/enrichplot relationship view for enriched terms.

Preferred encodings:

- nodes: enriched terms
- edges: semantic or overlap-based similarity between terms
- node color: term cluster or database/source grouping when supported
- node size: term significance or hit size, depending on the GLESKit figure

Interpretation guidance:

- use this when the user wants pathway relationships, term clusters, or graph-like organization of enrichment results
- describe clusters and bridges, not just the single most significant term

### 1e. Gene-Concept Network

Use this when GLESKit exposes a clusterProfiler/enrichplot `cnetplot`-style figure.

Preferred encodings:

- term nodes: enriched concepts or pathways
- gene nodes: contributing genes
- edges: membership links between genes and terms

Interpretation guidance:

- use this when the user wants to know which genes are driving which enriched terms
- do not over-interpret hub appearance as biological centrality unless GLESKit explicitly supports that interpretation

### 2. RNA-seq Volcano Plot

Use when GLESKit returns effect sizes and significance from a DEG table.

Preferred encodings:

- x-axis: `log2 fold change`
- y-axis: `-log10(adjusted p-value)` if available, otherwise `-log10(p-value)`
- point color:
  - red: significantly upregulated
  - blue: significantly downregulated
  - gray: not significant
- point size: constant unless there is a strong reason to encode another variable

Add these guide elements when possible:

- vertical threshold lines at the fold-change cutoff if known
- horizontal threshold line at the significance cutoff if known
- labels for a small number of top genes:
  - highest significance
  - largest absolute fold change
  - biologically central genes mentioned in the GLESKit summary

Good defaults:

- label at most `5-10` genes
- avoid overcrowding
- include the comparison in the title or subtitle

Interpretation guidance:

- upper-right = stronger upregulated significant genes
- upper-left = stronger downregulated significant genes
- center gray cloud = weak or non-significant changes

### 3. RNA-seq Enrichment Panels

Use when GLESKit separately interprets upregulated and downregulated signatures.

Preferred layout:

- two side-by-side or stacked enrichment panels
- left/top: upregulated terms
- right/bottom: downregulated terms

Preferred encodings:

- use bar plots as above
- keep scale and styling comparable between the two panels

Interpretation guidance:

- explicitly frame them as separate biological programs
- do not merge up/down pathways into one unlabeled panel

### 4. Expression Context Bar Plot

Use when GLESKit returns GTEx or Expression Atlas tissue-expression context.

Preferred encodings:

- y-axis: tissues, sorted descending by expression
- x-axis: expression value such as median TPM or source-provided normalized value
- bar color:
  - one consistent color for the main chart
  - optional accent highlight for the top tissue or top few tissues

Good defaults:

- top `8-12` tissues
- horizontal bars
- show the source in subtitle, e.g. `GTEx reference expression`

Interpretation guidance:

- the chart shows relative tissue context, not differential expression
- do not over-interpret tissue ranking as disease specificity

### 5. Ranked Disease Association Plot

Use when GLESKit returns disease association scores or ranked disease links.

Preferred encodings:

- y-axis: diseases
- x-axis: association score or confidence score
- bar color:
  - one primary evidence color
  - optional grouped color by disease family if that grouping exists in the data

Good defaults:

- top `8-12` associations
- horizontal bars
- if the source score is bounded, keep the axis honest, for example `0 to 1`

Interpretation guidance:

- the chart shows association strength or ranking, not causality
- keep the caption explicit about the provider

### 6. Variant-Trait Association Plot

Use when GLESKit returns GWAS-style ranked trait associations.

Preferred encodings:

- y-axis: traits
- x-axis:
  - preferred: study count or ranking metric actually returned
  - if p-values are available: `-log10(p-value)`
- color:
  - one consistent evidence color
  - optional grouping by trait domain if available

If only sparse metadata exist, use a ranked table instead of forcing a chart.

### 7. Drug-Target Summary View

Use when GLESKit returns ChEMBL or target-context data.

Preferred visual form:

- compact dashboard or summary table, not necessarily a bar chart

Good components:

- number of linked drugs
- top drugs
- top indications
- mechanism labels
- development status if available

If you build a chart:

- use ranked bar charts for top indications or drug counts
- use a table for detailed drug rows

### 9. Interaction Or Network Visual

Use when GLESKit returns network edges or partner rankings.

Preferred encodings:

- nodes: genes or proteins
- node size: degree or importance if available
- node color:
  - query genes: one accent color
  - secondary partner nodes: a second neutral or secondary color
- edges: interactions
- edge width or opacity: confidence score if available

If the network is too dense:

- show only the top partners or highest-confidence edges
- or replace the graph with a ranked partner chart plus a compact network sketch

### 10. Matrix Views

Use when the comparison involves several entities against several evidence categories.

Examples:

- genes by diseases
- genes by pathways
- genes by modalities

Preferred encodings:

- rows: genes or features
- columns: diseases, pathways, modalities, or evidence types
- cell color intensity: score, count, or presence/absence if available

Use a matrix only when there are enough rows and columns to justify it.

## What To Generate For Different Data Types

### Gene set enrichment or investigation

Good visuals:

- enrichment bar plot
- multi-panel pathway summary dashboard
- pathway source comparison

### RNA-seq / DEG results

Good visuals:

- volcano plot
- upregulated enrichment panel
- downregulated enrichment panel
- top differential genes ranking plot
- compact dashboard showing comparison, counts, top pathways, and top genes

### Expression context

Good visuals:

- tissue-expression bar plot
- ranked tissue summary
- expression dashboard with top tissues and context notes

### Gene-disease or variant-trait associations

Good visuals:

- ranked association bar plot
- gene-to-disease matrix
- trait category summary

### Drug-target context

Good visuals:

- target-indication summary table
- compact dashboard with drug names, mechanisms, and indication counts

### Interaction/network context

Good visuals:

- network diagram if edge data is available
- degree or partner ranking chart
- compact interaction summary dashboard

## Combining Visuals

You are encouraged to combine:

- one or more data-backed charts
- a conceptual flowchart or mechanism map
- a dashboard-style layout summarizing the main takeaways

Good pattern:

1. a chart from GLESKit data
2. a small conceptual panel that explains the proposed mechanism or evidence structure
3. a short evidence-backed caption

## Dashboard Layout Rules

When building a dashboard:

1. include one primary chart only
2. add one or two small supporting panels
3. use compact text summaries, not long paragraphs
4. keep a clear hierarchy:
   - title and comparison
   - headline findings
   - primary chart
   - secondary chart or evidence table
   - interpretation panel

Good dashboard components:

- headline counts
- top pathways
- top genes
- top diseases
- evidence source labels
- short caveat or confidence note

Avoid:

- overcrowded multi-chart collages
- duplicated charts
- decorative panels with no analytical value

## Flowchart And Conceptual Diagram Rules

Use conceptual visuals when:

- the user wants the mechanism or reasoning explained
- the evidence structure is easier to understand as a process
- the available data do not support a better quantitative chart

Preferred flowchart structure:

- start with observed evidence
- move to interpreted biological programs
- then candidate mechanism or implication
- end with caveats, alternatives, or next steps when useful

Good labels:

- short
- causal only when justified
- explicit about uncertainty when needed

If the flowchart is a hypothesis map:

- label it as conceptual
- avoid presenting it as proven biology

## Captions And Interpretation

Every data-backed visual should be accompanied by a short caption that states:

1. what data source it came from
2. what was plotted
3. what the main takeaway is
4. one caveat if a caveat is important

Good caption pattern:

- `Data-backed enrichment bar plot from GLESKit g:Profiler output showing the top 10 enriched pathways for the supplied gene set, ranked by -log10 adjusted p-value. Longer bars indicate stronger statistical support.`

## Titles And Subtitles

Good title structure:

- `Top Enriched Pathways`
- `Tumor vs Normal Volcano Plot`
- `TP53 Tissue Expression Context`

Good subtitle structure:

- comparison or gene set label
- provider/source if useful
- species if ambiguity exists

## Non-Negotiable Interpretation Rules

1. Do not claim that color means something unless you explicitly defined it in the chart.
2. Do not claim that dot size means something unless it was actually encoded that way.
3. Do not treat ranked associations as proof of mechanism.
4. Do not turn provider scores into causal language.
5. If the data only support a ranking, present it as a ranking.

## Labeling Rule

Always make it clear which visual is which:

- `Data-backed visual generated from GLESKit enrichment output`
- `Conceptual flowchart summarizing the leading hypothesis`

Do not present a conceptual visual as if it were measured data.

## Non-Negotiable Rules

1. Never fabricate numeric values for a chart.
2. Never fabricate labels, categories, or ranks that are not supported by the available data.
3. If the exact requested figure cannot be built from the available data, say so and generate the closest honest visual instead.
4. If only conceptual structure is available, use a flowchart or dashboard rather than inventing a fake quantitative chart.
5. If the required field for a chart is missing, choose a different visualization rather than guessing the missing field.
6. If the visual mixes data-backed and conceptual content, label both parts explicitly.

## Good Visual Language

- "Here is a data-backed enrichment bar plot generated from the GLESKit output."
- "I’m adding a conceptual flowchart next to the pathway chart so the evidence and mechanism are easier to compare."
- "The dashboard combines GLESKit-derived counts with a conceptual interpretation panel."
