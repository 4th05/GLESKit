script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_ranked_gsea.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_ranked_gsea.R")
input_path <- io$input_path
output_path <- io$output_path

request <- gleskit_read_request(input_path)

species <- gleskit_string_value(request$species, "human")
species_orgdb_pkg <- gleskit_species_orgdb_package(species)
species_kegg_org <- gleskit_species_kegg_organism(species)
species_reactome_org <- gleskit_species_reactome_organism(species)
is_human_species <- gleskit_is_human_species(species)
is_mouse_species <- gleskit_is_mouse_species(species)
is_pseudomonas_species <- gleskit_is_pseudomonas_species(species)

score_rows <- request$gene_scores
if (is.null(score_rows)) {
  score_rows <- list()
}
limit <- max(1L, min(gleskit_int_value(request$limit, 15L), 30L))
sources <- toupper(trimws(as.character(unlist(request$sources, use.names = FALSE))))
sources <- sources[nzchar(sources)]
if (length(sources) == 0) {
  sources <- if (is_pseudomonas_species) c("KEGG") else c("GO:BP", "REAC")
}
if (length(setdiff(sources, c("GO:BP", "REAC", "KEGG"))) > 0) {
  stop("Ranked GSEA supports sources 'GO:BP', 'REAC', and 'KEGG'.")
}
if (is_pseudomonas_species && any(sources %in% c("GO:BP", "REAC"))) {
  stop("Pseudomonas ranked GSEA currently supports KEGG only.")
}

required_packages <- c("clusterProfiler")
if ("REAC" %in% sources) {
  required_packages <- c(required_packages, "ReactomePA")
}
if ((is_human_species || is_mouse_species) && any(sources %in% c("GO:BP", "REAC", "KEGG"))) {
  required_packages <- c(required_packages, species_orgdb_pkg)
}
gleskit_require_packages(required_packages[nzchar(required_packages)], "ranked GSEA")

orgdb <- NULL
if (nzchar(species_orgdb_pkg)) {
  orgdb <- getExportedValue(species_orgdb_pkg, species_orgdb_pkg)
}

score_map <- list()
for (row in score_rows) {
  gene <- gleskit_string_value(row$gene, "")
  if (is_human_species) {
    gene <- toupper(gene)
  }
  score <- suppressWarnings(as.numeric(row$score[[1]]))
  if (!nzchar(gene) || is.na(score)) {
    next
  }
  current <- score_map[[gene]]
  if (is.null(current) || abs(score) > abs(current)) {
    score_map[[gene]] <- score
  }
}

if (length(score_map) < 10) {
  output <- list(
    provider = "yulab_clusterprofiler",
    summary = paste0("Retrieved 0 ranked GSEA term(s) for ", length(score_map), " ranked gene(s)."),
    analysis_mode = "gsea",
    sources = as.list(sources),
    backend = "clusterProfiler",
    species = species,
    input_gene_count = length(score_map),
    query_size = length(score_map),
    background_size = length(score_map),
    mapped_gene_count = 0L,
    term_count = 0L,
    terms = list(),
    positive_terms = list(),
    negative_terms = list()
  )
  gleskit_write_output(output, output_path)
  quit(save = "no", status = 0)
}

symbol_scores <- unlist(score_map, use.names = TRUE)
symbol_scores <- sort(symbol_scores, decreasing = TRUE)

resolve_matched_genes <- function(gene_ids, mapping) {
  gene_ids <- unique(as.character(gene_ids[nzchar(gene_ids)]))
  if (length(gene_ids) == 0) {
    return(character(0))
  }
  if (is.null(mapping) || length(mapping) == 0) {
    return(gene_ids)
  }
  matched <- unique(unname(mapping[gene_ids]))
  matched <- matched[!is.na(matched) & nzchar(matched)]
  if (length(matched) == 0) {
    return(gene_ids)
  }
  matched
}

build_gsea_term <- function(row, source_name, core_gene_ids, core_matched_genes, query_size, backend_name, species_name) {
  core_gene_ids <- unique(as.character(core_gene_ids[nzchar(core_gene_ids)]))
  core_matched_genes <- unique(as.character(core_matched_genes[nzchar(core_matched_genes)]))
  query_size <- suppressWarnings(as.integer(query_size))
  term_size <- if ("setSize" %in% names(row)) suppressWarnings(as.integer(row[["setSize"]][[1]])) else NA_integer_
  if (is.na(term_size) || term_size <= 0) {
    term_size <- length(core_gene_ids)
  }
  background_size <- query_size
  intersection_size <- length(core_matched_genes)
  if (is.na(query_size) || query_size <= 0) {
    query_size <- max(length(core_gene_ids), intersection_size)
  }
  precision <- if (is.na(query_size) || query_size <= 0) NA_real_ else intersection_size / query_size
  recall <- if (is.na(term_size) || term_size <= 0) NA_real_ else intersection_size / term_size
  list(
    term_id = gleskit_string_value(row[["ID"]]),
    term_name = gleskit_string_value(row[["Description"]]),
    source = source_name,
    p_value = suppressWarnings(as.numeric(row[["pvalue"]][[1]])),
    adjusted_p_value = suppressWarnings(as.numeric(row[["p.adjust"]][[1]])),
    q_value = if ("qvalues" %in% names(row)) suppressWarnings(as.numeric(row[["qvalues"]][[1]])) else NULL,
    enrichment_score = suppressWarnings(as.numeric(row[["enrichmentScore"]][[1]])),
    normalized_enrichment_score = suppressWarnings(as.numeric(row[["NES"]][[1]])),
    query_size = query_size,
    background_size = background_size,
    term_size = term_size,
    intersection_size = intersection_size,
    odds_ratio = gleskit_enrichment_odds_ratio(query_size, term_size, intersection_size, background_size),
    precision = precision,
    recall = recall,
    gene_ratio = paste0(intersection_size, "/", max(1L, query_size)),
    background_ratio = paste0(term_size, "/", max(1L, background_size)),
    core_gene_ids = as.list(core_gene_ids),
    core_matched_genes = as.list(core_matched_genes),
    backend = backend_name,
    species = species_name,
    direction = ifelse(suppressWarnings(as.numeric(row[["NES"]][[1]])) >= 0, "positive", "negative")
  )
}

build_go_rows <- function(result_df) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    core_genes <- gleskit_split_ids(row[["core_enrichment"]][[1]])
    rows[[length(rows) + 1L]] <- build_gsea_term(
      row,
      source_name = "GO:BP",
      core_gene_ids = core_genes,
      core_matched_genes = core_genes,
      query_size = length(symbol_scores),
      backend_name = "clusterProfiler",
      species_name = species
    )
  }
  rows
}

build_reactome_rows <- function(result_df, entrez_to_symbol) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    core_gene_ids <- gleskit_split_ids(row[["core_enrichment"]][[1]])
    core_symbols <- resolve_matched_genes(core_gene_ids, entrez_to_symbol)
    rows[[length(rows) + 1L]] <- build_gsea_term(
      row,
      source_name = "REAC",
      core_gene_ids = core_gene_ids,
      core_matched_genes = core_symbols,
      query_size = length(symbol_scores),
      backend_name = "clusterProfiler",
      species_name = species
    )
  }
  rows
}

build_kegg_rows <- function(result_df, gene_mapping) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    core_gene_ids <- gleskit_split_ids(row[["core_enrichment"]][[1]])
    core_matched <- resolve_matched_genes(core_gene_ids, gene_mapping)
    rows[[length(rows) + 1L]] <- build_gsea_term(
      row,
      source_name = "KEGG",
      core_gene_ids = core_gene_ids,
      core_matched_genes = core_matched,
      query_size = length(symbol_scores),
      backend_name = "clusterProfiler",
      species_name = species
    )
  }
  rows
}

rows <- list()
mapped_gene_count <- 0L

if ("GO:BP" %in% sources) {
  go_result <- tryCatch(
    clusterProfiler::gseGO(
      geneList = symbol_scores,
      OrgDb = orgdb,
      keyType = "SYMBOL",
      ont = "BP",
      pvalueCutoff = 1,
      pAdjustMethod = "BH",
      verbose = FALSE,
      eps = 1e-300
    ),
    error = function(err) NULL
  )
  go_df <- if (is.null(go_result)) data.frame() else as.data.frame(go_result)
  mapped_gene_count <- max(mapped_gene_count, length(names(symbol_scores)))
  rows <- c(rows, build_go_rows(go_df))
}

if ("REAC" %in% sources) {
  symbol_to_entrez <- clusterProfiler::bitr(
    names(symbol_scores),
    fromType = "SYMBOL",
    toType = "ENTREZID",
    OrgDb = orgdb
  )
  if (!is.null(symbol_to_entrez) && nrow(symbol_to_entrez) > 0) {
    symbol_to_entrez <- unique(symbol_to_entrez[, c("SYMBOL", "ENTREZID")])
    entrez_to_symbol <- setNames(as.character(symbol_to_entrez$SYMBOL), as.character(symbol_to_entrez$ENTREZID))
    mapped_gene_count <- max(mapped_gene_count, nrow(symbol_to_entrez))
    entrez_scores <- symbol_scores[symbol_to_entrez$SYMBOL]
    names(entrez_scores) <- symbol_to_entrez$ENTREZID
    entrez_scores <- sort(entrez_scores, decreasing = TRUE)
    reactome_result <- tryCatch(
      ReactomePA::gsePathway(
        geneList = entrez_scores,
        organism = species_reactome_org,
        pvalueCutoff = 1,
        pAdjustMethod = "BH",
        verbose = FALSE,
        eps = 1e-300
      ),
      error = function(err) NULL
    )
    reactome_df <- if (is.null(reactome_result)) data.frame() else as.data.frame(reactome_result)
    reactome_rows <- build_reactome_rows(reactome_df, entrez_to_symbol)
    rows <- c(rows, reactome_rows)
  }
}

if ("KEGG" %in% sources) {
  if (is_pseudomonas_species) {
    kegg_scores <- symbol_scores
    kegg_gene_mapping <- stats::setNames(names(kegg_scores), names(kegg_scores))
    mapped_gene_count <- max(mapped_gene_count, length(kegg_scores))
    kegg_result <- tryCatch(
      clusterProfiler::gseKEGG(
        geneList = kegg_scores,
        organism = species_kegg_org,
        pvalueCutoff = 1,
        pAdjustMethod = "BH",
        verbose = FALSE,
        eps = 1e-300
      ),
      error = function(err) NULL
    )
    kegg_df <- if (is.null(kegg_result)) data.frame() else as.data.frame(kegg_result)
    rows <- c(rows, build_kegg_rows(kegg_df, kegg_gene_mapping))
  } else {
    symbol_to_entrez <- clusterProfiler::bitr(
      names(symbol_scores),
      fromType = "SYMBOL",
      toType = "ENTREZID",
      OrgDb = orgdb
    )
    if (!is.null(symbol_to_entrez) && nrow(symbol_to_entrez) > 0) {
      symbol_to_entrez <- unique(symbol_to_entrez[, c("SYMBOL", "ENTREZID")])
      entrez_to_symbol <- setNames(as.character(symbol_to_entrez$SYMBOL), as.character(symbol_to_entrez$ENTREZID))
      mapped_gene_count <- max(mapped_gene_count, nrow(symbol_to_entrez))
      entrez_scores <- symbol_scores[symbol_to_entrez$SYMBOL]
      names(entrez_scores) <- symbol_to_entrez$ENTREZID
      entrez_scores <- sort(entrez_scores, decreasing = TRUE)
      kegg_result <- tryCatch(
        clusterProfiler::gseKEGG(
          geneList = entrez_scores,
          organism = species_kegg_org,
          pvalueCutoff = 1,
          pAdjustMethod = "BH",
          verbose = FALSE,
          eps = 1e-300
        ),
        error = function(err) NULL
      )
      kegg_df <- if (is.null(kegg_result)) data.frame() else as.data.frame(kegg_result)
      rows <- c(rows, build_kegg_rows(kegg_df, entrez_to_symbol))
    }
  }
}

if (length(rows) == 0) {
  output <- list(
    provider = "yulab_clusterprofiler",
    summary = paste0("Retrieved 0 ranked GSEA term(s) for ", length(score_map), " ranked gene(s)."),
    analysis_mode = "gsea",
    sources = as.list(sources),
    backend = "clusterProfiler",
    species = species,
    input_gene_count = length(score_map),
    query_size = length(score_map),
    background_size = length(score_map),
    mapped_gene_count = mapped_gene_count,
    term_count = 0L,
    terms = list(),
    positive_terms = list(),
    negative_terms = list()
  )
  gleskit_write_output(output, output_path)
  quit(save = "no", status = 0)
}

order_frame <- data.frame(
  idx = seq_along(rows),
  p_adjust = vapply(rows, function(row) ifelse(is.null(row$adjusted_p_value) || is.na(row$adjusted_p_value), 1, row$adjusted_p_value), numeric(1)),
  abs_nes = vapply(rows, function(row) abs(ifelse(is.null(row$normalized_enrichment_score) || is.na(row$normalized_enrichment_score), 0, row$normalized_enrichment_score)), numeric(1))
)
order_idx <- order(order_frame$p_adjust, -order_frame$abs_nes, na.last = TRUE)
rows <- rows[order_idx]
rows <- head(rows, limit)

positive_terms <- Filter(function(row) identical(row$direction, "positive"), rows)
negative_terms <- Filter(function(row) identical(row$direction, "negative"), rows)

output <- list(
  provider = "yulab_clusterprofiler",
  summary = paste0("Retrieved ", length(rows), " ranked GSEA term(s) for ", length(score_map), " ranked gene(s)."),
  analysis_mode = "gsea",
  sources = as.list(sources),
  backend = "clusterProfiler",
  species = species,
  input_gene_count = length(score_map),
  query_size = length(score_map),
  background_size = length(score_map),
  mapped_gene_count = mapped_gene_count,
  term_count = length(rows),
  terms = rows,
  positive_terms = positive_terms,
  negative_terms = negative_terms
)

gleskit_write_output(output, output_path)
