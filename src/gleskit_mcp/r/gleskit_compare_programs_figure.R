script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_compare_programs_figure.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_compare_programs_figure.R")
input_path <- io$input_path
output_path <- io$output_path

gleskit_require_packages(c("DOSE", "enrichplot", "ggplot2"), "compare-programs figure")
request <- gleskit_read_request(input_path)

programs <- request$programs
if (is.null(programs)) {
  programs <- list()
}
saved_terms_raw <- request$saved_terms
database <- toupper(gleskit_string_value(request$database, "GO_BP"))
plot_type <- tolower(gleskit_string_value(request$plot_type, "emapplot"))
title <- gleskit_string_value(request$title, "Program Comparison Figure")
png_path <- gleskit_string_value(request$output_png_path, "")
svg_path <- gleskit_string_value(request$output_svg_path, "")
top_n <- max(2L, min(gleskit_int_value(request$top_n, 12L), 20L))

if (!nzchar(png_path)) {
  stop("Compare-programs figure requires output_png_path.")
}
if (!(plot_type %in% c("emapplot", "cnetplot"))) {
  stop(paste0("Unsupported compare-programs plot_type: ", plot_type))
}

gene_clusters <- list()
for (program_label in names(programs)) {
  genes <- unique(trimws(as.character(unlist(programs[[program_label]], use.names = FALSE))))
  genes <- genes[nzchar(genes)]
  if (length(genes) > 0) {
    gene_clusters[[program_label]] <- genes
  }
}
if (length(gene_clusters) < 2) {
  stop("Compare-programs figure requires at least two named non-empty gene programs.")
}

normalize_saved_terms <- function(raw_terms) {
  if (is.null(raw_terms) || length(raw_terms) == 0) {
    return(list())
  }
  normalized <- list()
  for (row in raw_terms) {
    if (is.null(row) || length(row) == 0) {
      next
    }
    program_label <- gleskit_string_value(row$program_label, "")
    source_value <- toupper(gleskit_string_value(row$source, ""))
    term_id <- gleskit_string_value(row$term_id, "")
    term_name <- gleskit_string_value(row$term_name, "")
    matched_genes <- unique(trimws(as.character(unlist(row$matched_genes, use.names = FALSE))))
    matched_genes <- matched_genes[nzchar(matched_genes)]
    if (!nzchar(program_label) || !nzchar(source_value) || !nzchar(term_id) || !nzchar(term_name) || length(matched_genes) == 0) {
      next
    }
    normalized[[length(normalized) + 1L]] <- list(
      program_label = program_label,
      term_id = term_id,
      term_name = term_name,
      source = source_value,
      p_value = suppressWarnings(as.numeric(gleskit_string_value(row$p_value, "NA"))),
      adjusted_p_value = suppressWarnings(as.numeric(gleskit_string_value(row$adjusted_p_value, "NA"))),
      q_value = suppressWarnings(as.numeric(gleskit_string_value(row$q_value, "NA"))),
      gene_ratio = gleskit_string_value(row$gene_ratio, ""),
      background_ratio = gleskit_string_value(row$background_ratio, ""),
      intersection_size = suppressWarnings(as.integer(gleskit_string_value(row$intersection_size, as.character(length(matched_genes))))),
      matched_genes = matched_genes
    )
  }
  normalized
}

saved_terms <- normalize_saved_terms(saved_terms_raw)
source_filter <- if (identical(database, "GO_BP")) "GO:BP" else if (identical(database, "REAC")) "REAC" else database
saved_terms <- Filter(function(row) identical(row$source, source_filter), saved_terms)
if (length(saved_terms) == 0) {
  stop("Compare-programs figure requires saved comparison terms matching the requested database.")
}

order_frame <- data.frame(
  idx = seq_along(saved_terms),
  p_adjust = vapply(saved_terms, function(row) ifelse(is.na(row$adjusted_p_value), 1, row$adjusted_p_value), numeric(1)),
  p_value = vapply(saved_terms, function(row) ifelse(is.na(row$p_value), 1, row$p_value), numeric(1)),
  stringsAsFactors = FALSE
)
saved_terms <- saved_terms[order(order_frame$p_adjust, order_frame$p_value, na.last = TRUE)]
saved_terms <- head(saved_terms, top_n)

compare_df <- data.frame(
  Cluster = vapply(saved_terms, `[[`, character(1), "program_label"),
  ID = vapply(saved_terms, `[[`, character(1), "term_id"),
  Description = vapply(saved_terms, `[[`, character(1), "term_name"),
  GeneRatio = vapply(
    saved_terms,
    function(row) if (nzchar(row$gene_ratio)) row$gene_ratio else paste0(max(1L, row$intersection_size), "/", max(1L, length(gene_clusters[[row$program_label]]))),
    character(1)
  ),
  BgRatio = vapply(
    saved_terms,
    function(row) if (nzchar(row$background_ratio)) row$background_ratio else paste0(max(1L, row$intersection_size), "/", max(1L, length(unique(unlist(gene_clusters, use.names = FALSE))))),
    character(1)
  ),
  pvalue = vapply(saved_terms, function(row) ifelse(is.na(row$p_value), 1, row$p_value), numeric(1)),
  p.adjust = vapply(saved_terms, function(row) ifelse(is.na(row$adjusted_p_value), 1, row$adjusted_p_value), numeric(1)),
  qvalue = vapply(saved_terms, function(row) ifelse(is.na(row$q_value), 1, row$q_value), numeric(1)),
  geneID = vapply(saved_terms, function(row) paste(row$matched_genes, collapse = "/"), character(1)),
  Count = vapply(saved_terms, function(row) max(1L, row$intersection_size), integer(1)),
  stringsAsFactors = FALSE
)

identity_genes <- unique(unlist(gene_clusters, use.names = FALSE))
gene2symbol <- stats::setNames(identity_genes, identity_genes)

compare_result <- methods::new(
  "compareClusterResult",
  compareClusterResult = compare_df,
  geneClusters = gene_clusters,
  fun = if (identical(database, "GO_BP")) "enrichGO" else "enrichPathway",
  gene2Symbol = gene2symbol,
  keytype = "SYMBOL",
  readable = TRUE,
  .call = call("compareCluster"),
  termsim = matrix(numeric(0), nrow = 0, ncol = 0),
  method = "",
  dr = data.frame()
)

plot_obj <- NULL
if (identical(plot_type, "emapplot")) {
  compare_result <- enrichplot::pairwise_termsim(compare_result, method = "JC")
  plot_obj <- enrichplot::emapplot(
    compare_result,
    showCategory = min(top_n, nrow(compare_df)),
    cex.params = list(category_node = 0.75)
  ) + ggplot2::ggtitle(title)
} else {
  plot_obj <- enrichplot::cnetplot(
    compare_result,
    showCategory = min(top_n, nrow(compare_df)),
    circular = FALSE,
    color.params = list(edge = TRUE),
    node_label = "category"
  ) + ggplot2::ggtitle(title)
}

render_status <- gleskit_write_plot_files(plot_obj, png_path, svg_path, width = 10.5, height = 7.2, dpi = 150)
gleskit_write_output(
  list(
    provider = "yulab_enrichplot",
    rendered_source = source_filter,
    render_mode = plot_type,
    rendered_term_count = nrow(compare_df),
    caption = if (identical(plot_type, "emapplot")) {
      paste0(
        "ClusterProfiler/enrichplot compareCluster enrichment map across ",
        length(gene_clusters),
        " programs. Nodes are enriched terms and edges reflect similarity between themes."
      )
    } else {
      paste0(
        "ClusterProfiler/enrichplot compareCluster gene-concept network across ",
        length(gene_clusters),
        " programs."
      )
    },
    png_path = if (isTRUE(render_status$png_created)) png_path else "",
    svg_path = if (isTRUE(render_status$svg_created)) svg_path else ""
  ),
  output_path
)
