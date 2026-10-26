script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_ranked_gsea_figure.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_ranked_gsea_figure.R")
input_path <- io$input_path
output_path <- io$output_path

gleskit_require_packages(c("clusterProfiler", "ReactomePA", "org.Hs.eg.db", "enrichplot", "ggplot2"), "ranked GSEA figure")
request <- gleskit_read_request(input_path)

score_rows <- request$gene_scores
if (is.null(score_rows)) {
  score_rows <- list()
}
sources <- toupper(trimws(as.character(unlist(request$sources, use.names = FALSE))))
sources <- sources[nzchar(sources)]
if (length(sources) == 0) {
  sources <- c("REAC", "GO:BP")
}
prefer_source <- toupper(gleskit_string_value(request$prefer_source, "REAC"))
title <- gleskit_string_value(request$title, "Ranked GSEA Plot")
png_path <- gleskit_string_value(request$output_png_path, "")
svg_path <- gleskit_string_value(request$output_svg_path, "")
top_n <- max(2L, min(gleskit_int_value(request$top_n, 15L), 30L))

if (!nzchar(png_path)) {
  stop("Ranked GSEA figure requires output_png_path.")
}

score_map <- list()
for (row in score_rows) {
  gene <- toupper(gleskit_string_value(row$gene, ""))
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
  stop("Ranked GSEA figure requires at least 10 ranked genes.")
}

symbol_scores <- unlist(score_map, use.names = TRUE)
symbol_scores <- sort(symbol_scores, decreasing = TRUE)

subset_result <- function(result_obj, limit) {
  if (is.null(result_obj)) {
    return(NULL)
  }
  result_df <- as.data.frame(result_obj)
  if (nrow(result_df) == 0) {
    return(NULL)
  }
  p_adjust <- if ("p.adjust" %in% names(result_df)) result_df[["p.adjust"]] else rep(1, nrow(result_df))
  abs_nes <- if ("NES" %in% names(result_df)) abs(result_df[["NES"]]) else rep(0, nrow(result_df))
  ordered <- result_df[order(p_adjust, -abs_nes, na.last = TRUE), , drop = FALSE]
  ordered <- head(ordered, limit)
  ids <- as.character(ordered[["ID"]])
  result_obj@result <- result_obj@result[result_obj@result$ID %in% ids, , drop = FALSE]
  result_obj@result <- result_obj@result[match(ids, result_obj@result$ID), , drop = FALSE]
  result_obj
}

build_go_result <- function(input_scores, limit) {
  result <- tryCatch(
    clusterProfiler::gseGO(
      geneList = input_scores,
      OrgDb = org.Hs.eg.db::org.Hs.eg.db,
      keyType = "SYMBOL",
      ont = "BP",
      pvalueCutoff = 1,
      pAdjustMethod = "BH",
      verbose = FALSE,
      eps = 1e-300
    ),
    error = function(err) NULL
  )
  subset_result(result, limit)
}

build_reactome_result <- function(input_scores, limit) {
  symbol_to_entrez <- tryCatch(
    clusterProfiler::bitr(
      names(input_scores),
      fromType = "SYMBOL",
      toType = "ENTREZID",
      OrgDb = org.Hs.eg.db::org.Hs.eg.db
    ),
    error = function(err) NULL
  )
  if (is.null(symbol_to_entrez) || nrow(symbol_to_entrez) == 0) {
    return(NULL)
  }
  symbol_to_entrez <- unique(symbol_to_entrez[, c("SYMBOL", "ENTREZID")])
  entrez_scores <- input_scores[symbol_to_entrez$SYMBOL]
  names(entrez_scores) <- symbol_to_entrez$ENTREZID
  entrez_scores <- sort(entrez_scores, decreasing = TRUE)
  result <- tryCatch(
    ReactomePA::gsePathway(
      geneList = entrez_scores,
      organism = "human",
      pvalueCutoff = 1,
      pAdjustMethod = "BH",
      verbose = FALSE,
      eps = 1e-300
    ),
    error = function(err) NULL
  )
  subset_result(result, limit)
}

go_result <- if ("GO:BP" %in% sources) build_go_result(symbol_scores, top_n) else NULL
reactome_result <- if ("REAC" %in% sources) build_reactome_result(symbol_scores, top_n) else NULL

pick_result <- function(preferred, go_obj, reactome_obj) {
  candidates <- list(
    REAC = reactome_obj,
    "GO:BP" = go_obj
  )
  candidate_order <- unique(c(preferred, "REAC", "GO:BP"))
  for (source_name in candidate_order) {
    current <- candidates[[source_name]]
    if (!is.null(current) && nrow(as.data.frame(current)) >= 1) {
      return(list(source = source_name, object = current))
    }
  }
  NULL
}

selected <- pick_result(prefer_source, go_result, reactome_result)
if (is.null(selected)) {
  stop("Ranked GSEA figure requires at least one enriched term to plot.")
}

result_obj <- selected$object
result_df <- as.data.frame(result_obj)
order_idx <- order(result_df[["p.adjust"]], -abs(result_df[["NES"]]), na.last = TRUE)
result_df <- result_df[order_idx, , drop = FALSE]
top_term_id <- as.character(result_df[["ID"]][[1]])
top_term_name <- as.character(result_df[["Description"]][[1]])

plot_obj <- enrichplot::gseaplot2(
  result_obj,
  geneSetID = top_term_id,
  title = paste0(title, ": ", top_term_name),
  subplots = 1:3,
  pvalue_table = FALSE
)

render_status <- gleskit_write_plot_files(plot_obj, png_path, svg_path, width = 10.5, height = 7.2, dpi = 150)
gleskit_write_output(
  list(
    provider = "yulab_enrichplot",
    rendered_source = selected$source,
    render_mode = "gseaplot",
    rendered_term_count = nrow(result_df),
    top_term_id = top_term_id,
    top_term_name = top_term_name,
    caption = paste0(
      "ClusterProfiler/enrichplot GSEA running-score plot for the strongest ",
      selected$source,
      " term: ",
      top_term_name,
      "."
    ),
    png_path = if (isTRUE(render_status$png_created)) png_path else "",
    svg_path = if (isTRUE(render_status$svg_created)) svg_path else ""
  ),
  output_path
)
