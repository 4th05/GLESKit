# Cross-Modal Differential Results

Use this reference for requests that combine precomputed differential transcriptomics and proteomics results, including DEG/DEP biomarker or pathway questions.

1. Inspect each table first when its structure or identifiers are ambiguous. For a workbook containing both filtered and full candidates, call `preflight_differential_result`; it reads paths server-side and keeps source rows out of the model response.
2. Call `analyze_differential_result` separately for each modality. Require the exact positive and reference conditions, the significance basis, and whether each table is complete or filtered. For a linear protein ratio, provide `effect_definition` with declared biological numerator/denominator; never infer its direction from `HCA/HCC`-style shorthand. For a large complete table, defer automatic enrichment and/or ranked GSEA with `include_enrichment=false` and `include_ranked_gsea=false`; use the returned reusable handoffs for focused follow-up instead of timing out the canonical analysis.
3. Run directional ORA only with a valid complete tested/quantified background. Run ranked GSEA only with a complete ranked universe. If a compatible full protein result is unavailable, retain protein candidates but explicitly omit protein pathway inference.
4. Use `integrate_gene_level_results` only on canonical analysis outputs or their manifests. Pass separately produced direction-labelled enrichment evidence in `pathway_results` for pathway convergence. Do not manually join tables in prose or shell code.
5. Treat RNA-only, protein-only, concordant, discordant, mapping-ambiguous, and direction-not-comparable results as different evidence states. A zero shared-gene count is a finding, not an integration failure.
6. Do not claim a biomarker score, diagnostic classifier, clinical specificity, causality, or validation from differential tables alone. Use `build_biomarker_candidate_dossiers` only for explicitly selected genes; it preserves evidence and caveats without assigning a fixed score. State what sample-level and independent-cohort validation would be needed.
7. Use gene/protein context, pathway/network, disease, and literature tools only after integration for focused follow-up on explicitly selected evidence rows. Comparator-mismatched literature is context-only.

The canonical integration is gene-level. It does not process raw mass-spectrometry data, infer contrast direction from a comparison label, or silently select among ambiguous protein-to-gene mappings.
