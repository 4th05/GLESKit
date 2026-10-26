script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_compare_programs.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_compare_programs.R")
input_path <- io$input_path
output_path <- io$output_path

request <- gleskit_read_request(input_path)

species <- gleskit_string_value(request$species, "human")
species_orgdb_pkg <- gleskit_species_orgdb_package(species)
species_reactome_org <- gleskit_species_reactome_organism(species)
is_human_species <- gleskit_is_human_species(species)
is_mouse_species <- gleskit_is_mouse_species(species)
if (!(is_human_species || is_mouse_species)) {
  stop("Gene-program comparison currently supports human and mouse studies only.")
}

programs <- request$programs
if (is.null(programs)) {
  programs <- list()
}
database <- toupper(gleskit_string_value(request$database, "GO_BP"))
limit <- max(1L, min(gleskit_int_value(request$limit, 10L), 20L))

required_packages <- c("clusterProfiler")
if (database == "REAC") {
  required_packages <- c(required_packages, "ReactomePA")
}
required_packages <- c(required_packages, species_orgdb_pkg)
gleskit_require_packages(required_packages[nzchar(required_packages)], "gene-program comparison")

orgdb <- getExportedValue(species_orgdb_pkg, species_orgdb_pkg)

program_terms <- list()
comparison_rows <- list()

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

build_program_term <- function(row, program_label, source_name, gene_ids, matched_genes, query_size, species_name) {
  gene_ids <- unique(as.character(gene_ids[nzchar(gene_ids)]))
  matched_genes <- unique(as.character(matched_genes[nzchar(matched_genes)]))
  query_size <- suppressWarnings(as.integer(query_size))
  gene_ratio <- gleskit_string_value(row[["GeneRatio"]])
  background_ratio <- gleskit_string_value(row[["BgRatio"]])
  gene_ratio_parts <- gleskit_ratio_parts(row[["GeneRatio"]])
  background_ratio_parts <- gleskit_ratio_parts(row[["BgRatio"]])
  term_size <- background_ratio_parts$numerator
  if (is.na(term_size) || term_size <= 0) {
    term_size <- max(length(gene_ids), length(matched_genes))
  }
  background_size <- background_ratio_parts$denominator
  if (is.na(background_size) || background_size <= 0) {
    background_size <- max(query_size, term_size)
  }
  if (is.na(query_size) || query_size <= 0) {
    query_size <- gene_ratio_parts$denominator
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
    program_label = program_label,
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
    backend = "clusterProfiler",
    species = species_name
  )
}

build_go_rows <- function(result_df, program_label) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    gene_symbols <- gleskit_split_ids(row[["geneID"]][[1]])
    rows[[length(rows) + 1L]] <- build_program_term(
      row,
      program_label = program_label,
      source_name = "GO:BP",
      gene_ids = gene_symbols,
      matched_genes = gene_symbols,
      query_size = length(genes),
      species_name = species
    )
  }
  rows
}

build_reactome_rows <- function(result_df, program_label, entrez_to_symbol, query_size) {
  rows <- list()
  if (nrow(result_df) == 0) {
    return(rows)
  }
  for (i in seq_len(nrow(result_df))) {
    row <- result_df[i, , drop = FALSE]
    gene_ids <- gleskit_split_ids(row[["geneID"]][[1]])
    matched_symbols <- resolve_matched_genes(gene_ids, entrez_to_symbol)
    rows[[length(rows) + 1L]] <- build_program_term(
      row,
      program_label = program_label,
      source_name = "REAC",
      gene_ids = gene_ids,
      matched_genes = matched_symbols,
      query_size = query_size,
      species_name = species
    )
  }
  rows
}

for (program_label in names(programs)) {
  genes <- unique(trimws(as.character(unlist(programs[[program_label]], use.names = FALSE))))
  genes <- genes[nzchar(genes)]
  if (length(genes) == 0) {
    next
  }

  program_rows <- list()
  if (database == "GO_BP") {
    result <- tryCatch(
      clusterProfiler::enrichGO(
        gene = genes,
        OrgDb = orgdb,
        keyType = "SYMBOL",
        ont = "BP",
        pvalueCutoff = 1,
        pAdjustMethod = "BH",
        readable = FALSE
      ),
      error = function(err) NULL
    )
    result_df <- if (is.null(result)) data.frame() else as.data.frame(result)
    if (nrow(result_df) > 0) {
      result_df <- result_df[order(result_df[["p.adjust"]], result_df[["pvalue"]], na.last = TRUE), , drop = FALSE]
      result_df <- head(result_df, limit)
      program_rows <- build_go_rows(result_df, program_label)
    }
  } else if (database == "REAC") {
    symbol_to_entrez <- clusterProfiler::bitr(
      genes,
      fromType = "SYMBOL",
      toType = "ENTREZID",
      OrgDb = orgdb
    )
    if (!is.null(symbol_to_entrez) && nrow(symbol_to_entrez) > 0) {
      symbol_to_entrez <- unique(symbol_to_entrez[, c("SYMBOL", "ENTREZID")])
      entrez_to_symbol <- setNames(as.character(symbol_to_entrez$SYMBOL), as.character(symbol_to_entrez$ENTREZID))
      reactome_result <- tryCatch(
        ReactomePA::enrichPathway(
          gene = unique(as.character(symbol_to_entrez$ENTREZID)),
          organism = species_reactome_org,
          pvalueCutoff = 1,
          pAdjustMethod = "BH",
          readable = FALSE
        ),
        error = function(err) NULL
      )
      result_df <- if (is.null(reactome_result)) data.frame() else as.data.frame(reactome_result)
      if (nrow(result_df) > 0) {
        result_df <- result_df[order(result_df[["p.adjust"]], result_df[["pvalue"]], na.last = TRUE), , drop = FALSE]
        result_df <- head(result_df, limit)
        program_rows <- build_reactome_rows(result_df, program_label, entrez_to_symbol, length(symbol_to_entrez$ENTREZID))
      }
    }
  } else {
    stop(paste0("Unsupported comparison database: ", database))
  }

  program_terms[[program_label]] <- list(
    gene_count = length(genes),
    term_count = length(program_rows),
    terms = program_rows
  )
  comparison_rows <- c(comparison_rows, program_rows)
}

output <- list(
  provider = "yulab_clusterprofiler",
  summary = paste0("Compared ", length(program_terms), " gene program(s) and retrieved ", length(comparison_rows), " enrichment row(s)."),
  analysis_mode = "ora_compare",
  database = database,
  backend = "clusterProfiler",
  species = species,
  program_count = length(program_terms),
  programs = program_terms,
  comparison_terms = comparison_rows
)

gleskit_write_output(output, output_path)
