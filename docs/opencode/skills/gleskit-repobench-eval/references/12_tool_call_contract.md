# RepoBench Tool Call Contract

`analyze_differential_result` and `integrate_gene_level_results` are reserved for an episode that explicitly provides compatible precomputed cross-modal result tables; they do not change this persona's required report-save and provenance workflow.

General rules:

- use exact tool names that are actually available
- in OpenCode, call GLESKit MCP tools with the exposed `gleskit_` prefix, including report tools
- prefer the shortest valid tool path
- use the provided existing `session_id`
- never call `gleskit_create_session` during RepoBench evaluation

Report workflow tools:

- `gleskit_list_report_templates`
- `gleskit_get_report_template`
- `gleskit_create_session_report`
- `gleskit_update_session_report`
- `gleskit_get_session_report`

Contract:

1. If you need to confirm the expected structure, call `gleskit_get_report_template` with the requested `template_id`.
2. When the report draft is ready, save it with `gleskit_create_session_report` using the provided `session_id`.
3. If you need one revision, use `gleskit_update_session_report`.
4. Always verify the final saved artifact with `gleskit_get_session_report`.
5. Inspect `citation_audit` in the verification payload. If it reports title mismatches, missing exact paper titles, or cited-but-unlinked artifacts, fix them in one focused revision and verify again.
6. Do not treat a chat draft as equivalent to a saved report.

## Analysis Tool Semantics

When a RepoBench task uses GLESKit analysis tools before report writing, apply the same canonical contracts:

- preserve exact metadata group and condition values from `gleskit_inspect_tabular_data`; never replace a named level such as `DMSO` with `Untreated`;
- pass `prepared_manifest_path` and other `derived_inputs` paths exactly as returned by upstream tools;
- use `log2_fold_change_threshold=0.0` when no absolute LFC cutoff is stated, and preserve raw-p versus adjusted-p semantics;
- choose matrix `observation_unit` explicitly (`flattened_values`, `featurewise`, or `pairwise_log2_fold_change`);
- use `backend="enrichr"` with exact named gseapy libraries, and use `GO_BP`/`REAC` only for generic local enrichment;
- report the actual provider and analysis route in the saved report's visible provenance.
- for continuous/qPCR matrices, use matrix statistics with an explicit `value_transform` and verified
  `sample_group_map`; never route those values through DESeq2.
- use `gleskit_analyze_stratified_deseq2` for composite strata such as a selected set of cell types versus its
  complement, and pass the strata as separate exact lists. Select its explicit focus-gene, adjusted-count, or
  annotation-group endpoint when the requested output is not a cross-stratum LFC distribution.
- use `analysis="threshold_count"` for direct counts in result columns such as dispersion.
- after two identical non-retryable tool errors, stop guessing and record the compact diagnostic in the report.
