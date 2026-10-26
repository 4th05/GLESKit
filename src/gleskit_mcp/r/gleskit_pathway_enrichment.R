script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_pathway_enrichment.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_pathway_enrichment.R")
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

genes <- unique(trimws(as.character(unlist(request$genes, use.names = FALSE))))
genes <- genes[nzchar(genes)]
sources <- toupper(trimws(as.character(unlist(request$sources, use.names = FALSE))))
sources <- sources[nzchar(sources)]
if (length(sources) == 0) {
  sources <- if (is_pseudomonas_species) c("KEGG") else c("GO:BP", "REAC")
}
limit <- max(1L, min(gleskit_int_value(request$limit, 15L), 30L))
background_genes <- unique(trimws(as.character(unlist(request$background_genes, use.names = FALSE))))
background_genes <- background_genes[nzchar(background_genes)]
simplify_go_similarity <- suppressWarnings(as.numeric(request$simplify_go_similarity[[1]]))
if (is.na(simplify_go_similarity)) {
  simplify_go_similarity <- 0
}
focus_terms <- unique(trimws(as.character(unlist(request$focus_terms, use.names = FALSE))))
focus_terms <- focus_terms[nzchar(focus_terms)]
focus_terms <- head(focus_terms, 10L)
focus_key <- function(value) {
  tolower(gsub("[^[:alnum:]]", "", trimws(as.character(value))))
}

if (length(setdiff(sources, c("GO:BP", "REAC", "KEGG"))) > 0) {
  stop("Pathway enrichment supports sources 'GO:BP', 'REAC', and 'KEGG'.")
}
if (is_pseudomonas_species && any(sources %in% c("GO:BP", "REAC"))) {
  stop("Pseudomonas pathway enrichment currently supports KEGG only.")
}

required_packages <- c("clusterProfiler")
if ("REAC" %in% sources) {
  required_packages <- c(required_packages, "ReactomePA")
}
if ((is_human_species || is_mouse_species) && any(sources %in% c("GO:BP", "REAC", "KEGG"))) {
  required_packages <- c(required_packages, species_orgdb_pkg)
}
gleskit_require_packages(required_packages[nzchar(required_packages)], "pathway enrichment")

orgdb <- NULL
if (nzchar(species_orgdb_pkg)) {
  orgdb <- getExportedValue(species_orgdb_pkg, species_orgdb_pkg)
}

empty_result <- list(
  provider = "yulab_clusterprofiler",
  summary = paste0("Retrieved 0 pathway enrichment term(s) for ", length(genes), " input gene(s)."),
  analysis_mode = "ora",
  sources = as.list(sources),
  backend = "clusterProfiler",
  species = species,
  input_gene_count = length(genes),
  query_size = length(genes),
  background_size = length(background_genes),
  mapped_gene_count = 0L,
  term_count = 0L,
  source_counts = list(),
  terms = list(),
  total_term_count = 0L,
  focus_terms_requested = as.list(focus_terms),
  focus_terms_found = list(),
  focused_terms = list()
)

if (length(genes) == 0) {
  gleskit_write_output(empty_result, output_path)
  quit(save = "no", status = 0)
}

query_symbol_to_entrez <- data.frame(SYMBOL = character(), ENTREZID = character(), stringsAsFactors = FALSE)
background_symbol_to_entrez <- data.frame(SYMBOL = character(), ENTREZID = character(), stringsAsFactors = FALSE)
if ((is_human_species || is_mouse_species) && nzchar(species_orgdb_pkg)) {
  query_symbol_to_entrez <- tryCatch(
    clusterProfiler::bitr(
      genes,
      fromType = "SYMBOL",
      toType = "ENTREZID",
      OrgDb = orgdb
    ),
    error = function(err) data.frame(SYMBOL = character(), ENTREZID = character(), stringsAsFactors = FALSE)
  )
  if (nrow(query_symbol_to_entrez) > 0) {
    query_symbol_to_entrez <- unique(query_symbol_to_entrez[, c("SYMBOL", "ENTREZID")])
  }
  if (length(background_genes) > 0) {
    background_symbol_to_entrez <- tryCatch(
      clusterProfiler::bitr(
        background_genes,
        fromType = "SYMBOL",
        toType = "ENTREZID",
        OrgDb = orgdb
      ),
      error = function(err) data.frame(SYMBOL = character(), ENTREZID = character(), stringsAsFactors = FALSE)
    )
    if (nrow(background_symbol_to_entrez) > 0) {
      background_symbol_to_entrez <- unique(background_symbol_to_entrez[, c("SYMBOL", "ENTREZID")])
    }
  }
}

query_entrez_ids <- if (nrow(query_symbol_to_entrez) > 0) unique(as.character(query_symbol_to_entrez$ENTREZID)) else character(0)
background_entrez_ids <- if (nrow(background_symbol_to_entrez) > 0) unique(as.character(background_symbol_to_entrez$ENTREZID)) else character(0)
entrez_to_symbol <- setNames(as.character(query_symbol_to_entrez$SYMBOL), as.character(query_symbol_to_entrez$ENTREZID))

resolve_matched_genes <- function(gene_ids, mapping) {
  gene_ids <- unique(gene_ids[nzchar(gene_ids)])
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

build_ora_term <- function(row, source_name, gene_ids, matched_genes, query_size, background_size, species_name, backend_name) {
  gene_ids <- unique(as.character(gene_ids[nzchar(gene_ids)]))
  matched_genes <- unique(as.character(matched_genes[nzchar(matched_genes)]))
  query_size <- suppressWarnings(as.integer(query_size))
  background_size <- suppressWarnings(as.integer(background_size))
  gene_ratio <- gleskit_string_value(row[["GeneRatio"]])
  background_ratio <- gleskit_string_value(row[["BgRatio"]])
  gene_ratio_parts <- gleskit_ratio_parts(row[["GeneRatio"]])
  background_ratio_parts <- gleskit_ratio_parts(row[["BgRatio"]])
  if (is.na(query_size) || query_size <= 0) {
    query_size <- gene_ratio_parts$denominator
  }
  if (is.na(background_size) || background_size <= 0) {
    background_size <- background_ratio_parts$denominator
  }
  term_size <- background_ratio_parts$numerator
  if (is.na(term_size) || term_size <= 0) {
    term_size <- max(length(gene_ids), length(matched_genes))
  }
  if (is.na(background_size) || background_size <= 0) {
    background_size <- max(query_size, term_size)
  }
  intersection_size <- if ("Count" %in% names(row)) {
    gleskit_safe_integer(row[["Count"]], length(matched_genes))
  } else {
    length(matched_genes)
  }
  if (is.na(intersection_size) || intersection_size <= 0) {
    intersection_size <- length(matched_genes)
  }
  if (!nzchar(gene_ratio)) {
    gene_ratio <- paste0(intersection_size, "/", max(1L, query_size))
  }
  if (!nzchar(background_ratio)) {
    background_ratio <- paste0(term_size, "/", max(1L, background_size))
  }
  list(
    term_id = gleskit_string_value(row[["ID"]]),
    term_name = gleskit_string_value(row[["Description"]]),
    source = source_name,
    p_value = suppressWarnings(as.numeric(row[["pvalue"]][[1]])),
    adjusted_p_value = suppressWarnings(as.numeric(row[["p.adjust"]][[1]])),
    q_value = if ("qvalue" %in% names(row)) suppressWarnings(as.numeric(row[["qvalue"]][[1]])) else NULL,
    query_size = query_size,
    background_size = background_size,
    term_size = term_size,
    intersection_size = intersection_size,
    odds_ratio = gleskit_enrichment_odds_ratio(query_size, term_size, intersection_size, background_size),
    precision = if (is.na(query_size) || query_size <= 0) NA_real_ else intersection_size / query_size,
    recall = if (is.na(term_size) || term_size <= 0) NA_real_ else intersection_size / term_size,
    gene_ratio = gene_ratio,
    background_ratio = background_ratio,
    gene_ids = as.list(gene_ids),
    matched_genes = as.list(matched_genes),
    backend = backend_name,
    species = species_name
  )
}

build_go_rows <- function(result_df) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    gene_symbols <- gleskit_split_ids(row[["geneID"]][[1]])
    rows[[length(rows) + 1L]] <- build_ora_term(
      row,
      source_name = "GO:BP",
      gene_ids = gene_symbols,
      matched_genes = gene_symbols,
      query_size = length(genes),
      background_size = length(background_genes),
      species_name = species,
      backend_name = "clusterProfiler"
    )
  }
  rows
}

build_reactome_rows <- function(result_df) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    gene_ids <- gleskit_split_ids(row[["geneID"]][[1]])
    matched_genes <- resolve_matched_genes(gene_ids, entrez_to_symbol)
    rows[[length(rows) + 1L]] <- build_ora_term(
      row,
      source_name = "REAC",
      gene_ids = gene_ids,
      matched_genes = matched_genes,
      query_size = length(query_entrez_ids),
      background_size = length(background_entrez_ids),
      species_name = species,
      backend_name = "clusterProfiler"
    )
  }
  rows
}

build_kegg_rows <- function(result_df) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  gene_mapping <- if (is_pseudomonas_species) {
    stats::setNames(genes, genes)
  } else {
    entrez_to_symbol
  }
  query_count <- if (is_pseudomonas_species) length(genes) else length(query_entrez_ids)
  background_count <- if (is_pseudomonas_species) length(background_genes) else length(background_entrez_ids)
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    gene_ids <- gleskit_split_ids(row[["geneID"]][[1]])
    matched_genes <- resolve_matched_genes(gene_ids, gene_mapping)
    rows[[length(rows) + 1L]] <- build_ora_term(
      row,
      source_name = "KEGG",
      gene_ids = gene_ids,
      matched_genes = matched_genes,
      query_size = query_count,
      background_size = background_count,
      species_name = species,
      backend_name = "clusterProfiler"
    )
  }
  rows
}

rows <- list()

if ("GO:BP" %in% sources) {
  go_result <- tryCatch(
    clusterProfiler::enrichGO(
      gene = genes,
      OrgDb = orgdb,
      keyType = "SYMBOL",
      ont = "BP",
      pvalueCutoff = 1,
      pAdjustMethod = "BH",
      universe = if (length(background_genes) > 0) background_genes else NULL,
      readable = FALSE
    ),
    error = function(err) NULL
  )
  if (!is.null(go_result) && simplify_go_similarity > 0) {
    go_result <- tryCatch(
      clusterProfiler::simplify(
        go_result,
        cutoff = simplify_go_similarity,
        by = "p.adjust",
        select_fun = min
      ),
      error = function(err) go_result
    )
  }
  raw_go_df <- if (is.null(go_result)) data.frame() else as.data.frame(go_result)
  rows <- c(rows, build_go_rows(raw_go_df))
}

if ("REAC" %in% sources && length(query_entrez_ids) > 0) {
  reactome_result <- tryCatch(
    ReactomePA::enrichPathway(
      gene = query_entrez_ids,
      organism = species_reactome_org,
      pvalueCutoff = 1,
      pAdjustMethod = "BH",
      universe = if (length(background_entrez_ids) > 0) background_entrez_ids else NULL,
      readable = FALSE
    ),
    error = function(err) NULL
  )
  reactome_df <- if (is.null(reactome_result)) data.frame() else as.data.frame(reactome_result)
  reactome_rows <- build_reactome_rows(reactome_df)
  rows <- c(rows, reactome_rows)
}

if ("KEGG" %in% sources) {
  kegg_gene_ids <- if (is_pseudomonas_species) genes else query_entrez_ids
  kegg_background_ids <- if (is_pseudomonas_species) background_genes else background_entrez_ids
  if (length(kegg_gene_ids) > 0) {
    kegg_result <- tryCatch(
      clusterProfiler::enrichKEGG(
        gene = kegg_gene_ids,
        organism = species_kegg_org,
        pvalueCutoff = 1,
        pAdjustMethod = "BH",
        universe = if (length(kegg_background_ids) > 0) kegg_background_ids else NULL
      ),
      error = function(err) NULL
    )
    kegg_df <- if (is.null(kegg_result)) data.frame() else as.data.frame(kegg_result)
    kegg_rows <- build_kegg_rows(kegg_df)
    rows <- c(rows, kegg_rows)
  }
}

if (length(rows) == 0) {
  empty_result$mapped_gene_count <- if (length(query_entrez_ids) > 0) length(query_entrez_ids) else length(genes)
  empty_result$query_size <- length(genes)
  empty_result$background_size <- if (length(background_entrez_ids) > 0) length(background_entrez_ids) else length(background_genes)
  gleskit_write_output(empty_result, output_path)
  quit(save = "no", status = 0)
}

order_frame <- data.frame(
  idx = seq_along(rows),
  p_adjust = vapply(rows, function(row) ifelse(is.null(row$adjusted_p_value) || is.na(row$adjusted_p_value), 1, row$adjusted_p_value), numeric(1)),
  p_value = vapply(rows, function(row) ifelse(is.null(row$p_value) || is.na(row$p_value), 1, row$p_value), numeric(1))
)
order_idx <- order(order_frame$p_adjust, order_frame$p_value, na.last = TRUE)
all_rows <- rows[order_idx]
focused_terms <- list()
focus_terms_found <- character()
if (length(focus_terms) > 0) {
  for (query in focus_terms) {
    query_key <- focus_key(query)
    matches <- all_rows[vapply(
      all_rows,
      function(row) query_key %in% focus_key(c(row$term_id, row$term_name)),
      logical(1)
    )]
    if (length(matches) > 0) {
      focus_terms_found <- c(focus_terms_found, query)
      focused_terms <- c(focused_terms, matches)
    }
  }
}
rows <- head(all_rows, limit)

source_counts <- table(vapply(rows, function(row) row$source, character(1)))
background_sizes <- vapply(rows, function(row) ifelse(is.null(row$background_size) || is.na(row$background_size), 0L, as.integer(row$background_size)), integer(1))
mapped_gene_count <- if (length(query_entrez_ids) > 0) length(query_entrez_ids) else length(genes)
output_background_size <- max(c(background_sizes, length(background_genes), length(background_entrez_ids)), na.rm = TRUE)
if (is.infinite(output_background_size) || is.na(output_background_size)) {
  output_background_size <- 0L
}

output <- list(
  provider = "yulab_clusterprofiler",
  summary = paste0("Retrieved ", length(rows), " pathway enrichment term(s) for ", length(genes), " input gene(s)."),
  analysis_mode = "ora",
  sources = as.list(sources),
  backend = "clusterProfiler",
  species = species,
  input_gene_count = length(genes),
  query_size = length(genes),
  background_size = output_background_size,
  mapped_gene_count = mapped_gene_count,
  term_count = length(rows),
  total_term_count = length(all_rows),
  source_counts = as.list(as.integer(source_counts)),
  terms = rows,
  focus_terms_requested = as.list(focus_terms),
  focus_terms_found = as.list(unique(focus_terms_found)),
  focused_terms = head(focused_terms, 20L)
)
names(output$source_counts) <- names(source_counts)

gleskit_write_output(output, output_path)
