script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_ppi_figure.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_ppi_figure.R")
input_path <- io$input_path
output_path <- io$output_path

gleskit_require_packages(c("clusterProfiler", "ggtangle", "ggplot2"), "PPI figure")
request <- gleskit_read_request(input_path)

normalize_source_token <- function(value) {
  token <- toupper(trimws(as.character(value)))
  if (identical(token, "GO_BP")) {
    return("GO:BP")
  }
  token
}

genes <- unique(trimws(as.character(unlist(request$genes, use.names = FALSE))))
genes <- genes[nzchar(genes)]
saved_terms_raw <- request$saved_terms
fold_changes_raw <- request$fold_changes
top_n <- max(1L, min(gleskit_int_value(request$top_n, 8L), 20L))
add_nodes <- max(0L, min(gleskit_int_value(request$add_nodes, 0L), 15L))
prefer_source <- normalize_source_token(gleskit_string_value(request$prefer_source, ""))
title <- gleskit_string_value(request$title, "Gene-Gene PPI Network")
png_path <- gleskit_string_value(request$output_png_path, "")
svg_path <- gleskit_string_value(request$output_svg_path, "")

if (!nzchar(png_path)) {
  stop("PPI figure requires output_png_path.")
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
    source_value <- toupper(gleskit_string_value(row$source, ""))
    term_id <- gleskit_string_value(row$term_id, "")
    term_name <- gleskit_string_value(row$term_name, "")
    matched_genes <- unique(trimws(as.character(unlist(row$matched_genes, use.names = FALSE))))
    matched_genes <- matched_genes[nzchar(matched_genes)]
    if (!nzchar(source_value) || !nzchar(term_id) || !nzchar(term_name) || length(matched_genes) == 0) {
      next
    }
    normalized[[length(normalized) + 1L]] <- list(
      term_id = term_id,
      term_name = term_name,
      source = source_value,
      p_value = suppressWarnings(as.numeric(gleskit_string_value(row$p_value, "NA"))),
      adjusted_p_value = suppressWarnings(as.numeric(gleskit_string_value(row$adjusted_p_value, "NA"))),
      matched_genes = matched_genes
    )
  }
  normalized
}

normalize_fold_changes <- function(raw_rows) {
  if (is.null(raw_rows) || length(raw_rows) == 0) {
    return(data.frame())
  }
  rows <- list()
  for (row in raw_rows) {
    if (is.null(row) || length(row) == 0) {
      next
    }
    gene <- toupper(gleskit_string_value(row$gene, ""))
    score <- suppressWarnings(as.numeric(gleskit_string_value(row$score, "NA")))
    if (!nzchar(gene) || is.na(score)) {
      next
    }
    rows[[length(rows) + 1L]] <- list(name = gene, logFC = score)
  }
  if (length(rows) == 0) {
    return(data.frame())
  }
  unique(do.call(rbind.data.frame, c(rows, stringsAsFactors = FALSE)))
}

saved_terms <- normalize_saved_terms(saved_terms_raw)
fold_change_df <- normalize_fold_changes(fold_changes_raw)

select_genes <- function(input_genes, saved_rows, limit, preferred_source) {
  selected_rows <- saved_rows
  if (length(selected_rows) > 0 && nzchar(preferred_source)) {
    preferred_rows <- Filter(function(row) identical(row$source, preferred_source), selected_rows)
    if (length(preferred_rows) > 0) {
      selected_rows <- preferred_rows
    }
  }
  if (length(selected_rows) > 0) {
    order_frame <- data.frame(
      idx = seq_along(selected_rows),
      p_adjust = vapply(selected_rows, function(row) ifelse(is.na(row$adjusted_p_value), 1, row$adjusted_p_value), numeric(1)),
      p_value = vapply(selected_rows, function(row) ifelse(is.na(row$p_value), 1, row$p_value), numeric(1)),
      stringsAsFactors = FALSE
    )
    selected_rows <- selected_rows[order(order_frame$p_adjust, order_frame$p_value, na.last = TRUE)]
    selected_rows <- head(selected_rows, limit)
    ranked_genes <- c()
    for (idx in seq_along(selected_rows)) {
      for (gene in unique(selected_rows[[idx]]$matched_genes)) {
        if (!(gene %in% ranked_genes)) {
          ranked_genes <- c(ranked_genes, gene)
        }
      }
    }
    selected_genes <- ranked_genes
    return(list(
      genes = unique(c(selected_genes, input_genes[input_genes %in% selected_genes])),
      term_count = length(selected_rows),
      source = if (length(selected_rows) > 0) selected_rows[[1]]$source else preferred_source
    ))
  }
  list(genes = unique(input_genes), term_count = 0L, source = preferred_source)
}

selection <- select_genes(genes, saved_terms, top_n, prefer_source)
selected_genes <- unique(selection$genes)
selected_genes <- selected_genes[nzchar(selected_genes)]
if (length(selected_genes) < 2) {
  stop("PPI figure requires at least two genes after term selection.")
}

ppi_graph <- NULL
ppi_graph <- tryCatch(
  clusterProfiler::getPPI(selected_genes, taxID = "9606", add_nodes = add_nodes),
  error = function(err) NULL
)
if (is.null(ppi_graph)) {
  stop("clusterProfiler::getPPI could not build a PPI graph for the selected genes.")
}

base_plot <- ggplot2::ggplot(ppi_graph)
plot_obj <- NULL
if (nrow(fold_change_df) > 0 && exists("%<+%")) {
  plot_obj <- base_plot %<+% fold_change_df +
    ggtangle::geom_edge(alpha = 0.35, colour = "#94a3b8") +
    ggtangle::geom_point(ggplot2::aes(color = logFC), size = 6) +
    ggplot2::scale_color_gradient2(low = "#327eba", mid = "#ffffff", high = "#e06663", midpoint = 0) +
    ggplot2::theme_void() +
    ggplot2::ggtitle(title)
} else {
  plot_obj <- base_plot +
    ggtangle::geom_edge(alpha = 0.35, colour = "#94a3b8") +
    ggtangle::geom_point(size = 6, color = "#e06663") +
    ggplot2::theme_void() +
    ggplot2::ggtitle(title)
}

render_status <- gleskit_write_plot_files(plot_obj, png_path, svg_path, width = 10.5, height = 7.2, dpi = 150)
gleskit_write_output(
  list(
    provider = "yulab_clusterprofiler",
    render_mode = "ppi_network",
    rendered_source = selection$source,
    rendered_gene_count = length(selected_genes),
    selected_term_count = selection$term_count,
    caption = paste0(
      "clusterProfiler::getPPI gene-gene interaction network for ",
      length(selected_genes),
      " genes",
      if (selection$term_count > 0) paste0(" drawn from the top ", selection$term_count, " enriched terms.") else "."
    ),
    png_path = if (isTRUE(render_status$png_created)) png_path else "",
    svg_path = if (isTRUE(render_status$svg_created)) svg_path else ""
  ),
  output_path
)
