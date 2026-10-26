# Examples

## Example 1: Recognizable Gene Set

User:

- "These are circadian genes, right? CLOCK, BMAL1, PER1, PER2, CRY1, CRY2."

Good behavior:

1. say it looks like it may relate to circadian biology
2. verify with GLESKit
3. only then confirm or qualify the interpretation

## Example 2: DEG File Path

User:

- "Analyze this DESeq2 result: /abs/path/result.csv. Positive log2FC means tumor > normal, adjusted for age and batch."

Good behavior:

1. preserve the comparison semantics
2. pass the path directly to GLESKit
3. interpret the result cautiously
4. do not ask for upload unless GLESKit path-based handling fails

## Example 3: Literature-Backed Mechanism

User:

- "Give me papers supporting this pathway interpretation."

Good behavior:

1. use `search_papers`
2. cite only what GLESKit actually returned
3. if author metadata is missing, use a metadata fallback instead of guessing

## Example 4: Visual Summary From GLESKit Data

User:

- "Show me what this gene set means visually."

Good behavior:

1. inspect the available GLESKit data
2. expect a data-backed enrichment bar plot to be auto-attached when GLESKit returns enrichment data
   - y-axis: top enriched terms
   - x-axis: `-log10(adjusted p-value)` or `-log10(p-value)`
   - bar color: one evidence color or pathway source grouping if clearly supported
3. optionally add a conceptual flowchart or dashboard panel
4. clearly separate:
   - data-backed chart
   - conceptual interpretation panel

Bad behavior:

- inventing chart values
- inventing ranks or p-values
- presenting a conceptual sketch as if it were measured data

## Example 5: "What Figures Can You Show Me?"

User:

- "What figures can you provide to me?"

Good behavior:

1. inspect the GLESKit data available for the current study
2. describe the visual types that can be generated from those data
3. include both:
   - data-backed figures that can be generated through GLESKit now
   - conceptual visuals that can complement the data-backed figures
4. do not limit the answer to saved GLESKit figure templates

For example:

- if enrichment data exist, offer enrichment bar plots or enrichment dashboards
- if whole-set disease enrichment data exist, offer disease-enrichment bar plots
- if multiple named programs were compared, offer grouped comparison bar plots and compareCluster enrichment maps when GLESKit supports them
- if RNA-seq DEG data exist, offer volcano plots, up/down pathway panels, or compact RNA-seq dashboards
- if disease or GWAS data exist, offer ranked association plots or matrix views
- if network data exist, offer network visuals or relationship dashboards

Bad behavior:

- answering only with stored figure-template availability
- treating saved figure artifacts as the main answer

## Example 6: "Show Me The Enrichment Bar Plot"

User:

- "Show me the enrichment bar plot."

Good behavior:

1. use the GLESKit enrichment result and expect the native enrichment bar plot to appear automatically
2. if the auto-attached figure is missing, only then call GLESKit figure creation manually
3. use:
   - y-axis: top enriched term names, ranked by significance
   - x-axis: `-log10(adjusted p-value)` or `-log10(p-value)`
   - bar color: one primary evidence color or library/source grouping if that is clearly supported
4. if the tool-result card visibly contains the image, refer to it as "the GLESKit figure in the tool result above"
5. add a short caption explaining what the chart supports biologically
5. only talk about saved figure artifacts separately if the user explicitly asks about saving or export

Bad behavior:

1. create the figure and then separately try to fetch the saved local file just to display it
2. fail to display the local asset inline
3. only afterward recreate the chart in chat

The returned GLESKit figure should have been the first path. The assistant should not pretend that the same tool image was duplicated inside plain assistant prose.

## Example 7b: Whole-Set Disease Themes

User:

- "What diseases fit this gene set best?"

Good behavior:

1. use `gene_set_disease_context` for one human gene set
2. interpret the output as set-level disease enrichment
3. if useful, connect it back to lead-gene disease evidence
4. do not present it as proof that the genes cause the disease

Bad behavior:

- only using single-gene disease tools for a whole-set disease question
- claiming GLESKit already supports direct disease comparison across multiple sets

## Example 7: Visualization Failed To Render

User:

- "I can't see anything."

Good behavior:

1. first determine whether the GLESKit tool-result card itself contains the image
2. if it does, say so plainly and stop claiming the assistant bubble contains it too
3. if the user still wants a directly embedded assistant-side figure, retry with a simpler assistant-made chart from the same GLESKit data
4. keep the same underlying GLESKit data and chart semantics
5. only mention saving/exporting a GLESKit figure if the user asks for that separately

Bad behavior:

- claiming the figure was shown successfully
- claiming that the assistant message itself contains the image when only the GLESKit tool-result card does
- saying that a saved local GLESKit figure solves the user's request to see it in chat
- assuming success after the user explicitly reports failure
- switching to saved figure artifacts instead of retrying an in-chat visualization

## Example 8: Workspace Management

User:

- "List my sessions and delete the ones that are unrelated to circadian biology."

Good behavior:

1. list sessions
2. summarize which ones match and which do not
3. ask for confirmation
4. only then delete

## Example 9: Mixed Visual Response

User:

- "Explain this RNA-seq result in a richer way."

Good behavior:

1. generate a volcano plot or enrichment panel from GLESKit data
   - volcano x-axis: `log2 fold change`
   - volcano y-axis: `-log10(adjusted p-value)` or `-log10(p-value)`
   - colors: red for upregulated, blue for downregulated, gray for non-significant
2. add a small conceptual flowchart explaining the leading biological interpretation
3. keep the data-backed and conceptual parts clearly labeled

## Example 10: Multiple Named Programs

User:

- "Compare these two programs and tell me which pathways distinguish them."

Good behavior:

1. use `compare_gene_programs`
2. expect a grouped comparison bar plot to auto-attach by default; if the user wants pathway-network structure, create the compareCluster enrichment map explicitly through GLESKit
3. describe pathway/program differences, not disease differences
4. if the user asks for disease comparison too, say GLESKit does not yet have a dedicated multi-set disease-comparison tool

## Example 11: Multi-Gene Paper Retrieval Strategy

User:

- "Find papers linking circadian genes to bipolar disorder."

Good behavior:

1. read `references/40_literature_and_provenance.md` before `search_papers`
2. because the user did not ask for per-gene retrieval, do not start with one-gene-at-a-time loops
3. build one broad query with grouped OR genes:
   - `bipolar disorder AND (CLOCK OR BMAL1 OR PER1 OR PER2 OR PER3 OR CRY1 OR CRY2 OR NR1D1 OR NR1D2 OR RORA OR RORB OR RORC OR CSNK1D OR CSNK1E OR BHLHE40 OR BHLHE41 OR TIMELESS OR NPAS2)`
4. run `search_papers` with that query
5. if needed, refine with an extra mechanism anchor in a second pass

Bad behavior:

1. running independent single-gene queries first as the default path
2. concluding there is no literature support after sparse single-gene results
3. citing papers without traceable retrieval metadata
