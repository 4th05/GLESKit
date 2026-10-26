script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_enrichment_figure.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_enrichment_figure.R")
input_path <- io$input_path
output_path <- io$output_path

gleskit_require_packages(c("clusterProfiler", "ReactomePA", "org.Hs.eg.db", "enrichplot", "ggplot2"), "enrichment figure")
request <- gleskit_read_request(input_path)

genes <- unique(trimws(as.character(unlist(request$genes, use.names = FALSE))))
genes <- genes[nzchar(genes)]
saved_terms_raw <- request$saved_terms
fold_changes_raw <- request$fold_changes
normalize_source_token <- function(value) {
  token <- toupper(trimws(as.character(value)))
  if (identical(token, "GO_BP")) {
    return("GO:BP")
  }
  token
}

sources <- vapply(unlist(request$sources, use.names = FALSE), normalize_source_token, character(1))
sources <- sources[nzchar(sources)]
if (length(sources) == 0) {
  sources <- c("REAC", "GO:BP")
}
plot_type <- tolower(gleskit_string_value(request$plot_type, "emapplot"))
if (!(plot_type %in% c("emapplot", "cnetplot"))) {
  stop(paste0("Unsupported enrichment plot_type: ", plot_type))
}
prefer_source <- normalize_source_token(gleskit_string_value(request$prefer_source, "REAC"))
top_n <- max(2L, min(gleskit_int_value(request$top_n, 12L), 20L))
title <- gleskit_string_value(request$title, "Enrichment Plot")
png_path <- gleskit_string_value(request$output_png_path, "")
svg_path <- gleskit_string_value(request$output_svg_path, "")

if (length(genes) < 2) {
  stop("Enrichment figure requires at least two genes.")
}
if (!nzchar(png_path)) {
  stop("Enrichment figure requires output_png_path.")
}

subset_result <- function(result_obj, limit) {
  if (is.null(result_obj)) {
    return(NULL)
  }
  result_df <- as.data.frame(result_obj)
  if (nrow(result_df) == 0) {
    return(NULL)
  }
  p_adjust <- if ("p.adjust" %in% names(result_df)) result_df[["p.adjust"]] else rep(1, nrow(result_df))
  p_value <- if ("pvalue" %in% names(result_df)) result_df[["pvalue"]] else rep(1, nrow(result_df))
  ordered <- result_df[order(p_adjust, p_value, na.last = TRUE), , drop = FALSE]
  ordered <- head(ordered, limit)
  ids <- as.character(ordered[["ID"]])
  result_obj@result <- result_obj@result[result_obj@result$ID %in% ids, , drop = FALSE]
  result_obj@result <- result_obj@result[match(ids, result_obj@result$ID), , drop = FALSE]
  slot_names <- methods::slotNames(result_obj)
  if ("geneSets" %in% slot_names) {
    keep_gene_sets <- intersect(ids, names(result_obj@geneSets))
    result_obj@geneSets <- result_obj@geneSets[keep_gene_sets]
  }
  if ("termsim" %in% slot_names) {
    result_obj@termsim <- matrix(numeric(0), nrow = 0, ncol = 0)
  }
  result_obj
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
      q_value = suppressWarnings(as.numeric(gleskit_string_value(row$q_value, "NA"))),
      gene_ratio = gleskit_string_value(row$gene_ratio, ""),
      background_ratio = gleskit_string_value(row$background_ratio, ""),
      intersection_size = suppressWarnings(as.integer(gleskit_string_value(row$intersection_size, as.character(length(matched_genes))))),
      matched_genes = matched_genes
    )
  }
  normalized
}

normalize_fold_changes <- function(raw_rows) {
  if (is.null(raw_rows) || length(raw_rows) == 0) {
    return(setNames(numeric(0), character(0)))
  }
  values <- c()
  for (row in raw_rows) {
    if (is.null(row) || length(row) == 0) {
      next
    }
    gene <- toupper(gleskit_string_value(row$gene, ""))
    score <- suppressWarnings(as.numeric(gleskit_string_value(row$score, "NA")))
    if (!nzchar(gene) || is.na(score)) {
      next
    }
    current <- values[[gene]]
    if (is.null(current) || abs(score) > abs(current)) {
      values[[gene]] <- score
    }
  }
  if (length(values) == 0) {
    return(setNames(numeric(0), character(0)))
  }
  stats::setNames(as.numeric(unlist(values, use.names = FALSE)), names(values))
}

build_saved_result <- function(saved_terms, source_name, limit) {
  source_terms <- Filter(function(row) identical(row$source, source_name), saved_terms)
  if (length(source_terms) == 0) {
    return(NULL)
  }
  order_frame <- data.frame(
    idx = seq_along(source_terms),
    p_adjust = vapply(source_terms, function(row) ifelse(is.na(row$adjusted_p_value), 1, row$adjusted_p_value), numeric(1)),
    p_value = vapply(source_terms, function(row) ifelse(is.na(row$p_value), 1, row$p_value), numeric(1))
  )
  source_terms <- source_terms[order(order_frame$p_adjust, order_frame$p_value, na.last = TRUE)]
  source_terms <- head(source_terms, limit)

  term_ids <- vapply(source_terms, `[[`, character(1), "term_id")
  term_names <- vapply(source_terms, `[[`, character(1), "term_name")
  term_genes <- lapply(source_terms, `[[`, "matched_genes")
  term2gene <- unique(data.frame(
    term = rep(term_ids, lengths(term_genes)),
    gene = unlist(term_genes, use.names = FALSE),
    stringsAsFactors = FALSE
  ))
  if (nrow(term2gene) == 0) {
    return(NULL)
  }
  term2name <- unique(data.frame(
    term = term_ids,
    name = term_names,
    stringsAsFactors = FALSE
  ))
  working_genes <- unique(term2gene$gene)
  result_obj <- tryCatch(
    suppressMessages(
      clusterProfiler::enricher(
        gene = working_genes,
        TERM2GENE = term2gene,
        TERM2NAME = term2name,
        pvalueCutoff = 1,
        pAdjustMethod = "BH",
        qvalueCutoff = 1,
        minGSSize = 1
      )
    ),
    error = function(err) NULL
  )
  if (is.null(result_obj)) {
    return(NULL)
  }

  result_df <- data.frame(
    ID = term_ids,
    Description = term_names,
    GeneRatio = vapply(
      source_terms,
      function(row) {
        if (nzchar(row$gene_ratio)) {
          return(row$gene_ratio)
        }
        paste0(max(1L, row$intersection_size), "/", max(1L, length(working_genes)))
      },
      character(1)
    ),
    BgRatio = vapply(
      source_terms,
      function(row) {
        if (nzchar(row$background_ratio)) {
          return(row$background_ratio)
        }
        paste0(max(1L, row$intersection_size), "/", max(1L, length(unique(term2gene$gene))))
      },
      character(1)
    ),
    pvalue = vapply(source_terms, function(row) ifelse(is.na(row$p_value), 1, row$p_value), numeric(1)),
    p.adjust = vapply(source_terms, function(row) ifelse(is.na(row$adjusted_p_value), 1, row$adjusted_p_value), numeric(1)),
    qvalue = vapply(source_terms, function(row) ifelse(is.na(row$q_value), 1, row$q_value), numeric(1)),
    geneID = vapply(source_terms, function(row) paste(row$matched_genes, collapse = "/"), character(1)),
    Count = vapply(source_terms, function(row) max(1L, row$intersection_size), integer(1)),
    stringsAsFactors = FALSE
  )
  rownames(result_df) <- result_df$ID
  result_obj@result <- result_df
  result_obj@gene <- working_genes
  if ("geneSets" %in% methods::slotNames(result_obj)) {
    rebuilt_gene_sets <- split(term2gene$gene, term2gene$term)
    rebuilt_gene_sets <- rebuilt_gene_sets[term_ids]
    result_obj@geneSets <- rebuilt_gene_sets
  }
  subset_result(result_obj, limit)
}

build_go_result <- function(input_genes, limit) {
  result <- tryCatch(
    clusterProfiler::enrichGO(
      gene = input_genes,
      OrgDb = org.Hs.eg.db::org.Hs.eg.db,
      keyType = "SYMBOL",
      ont = "BP",
      pvalueCutoff = 1,
      pAdjustMethod = "BH",
      readable = TRUE
    ),
    error = function(err) NULL
  )
  subset_result(result, limit)
}

build_reactome_result <- function(input_genes, limit) {
  symbol_to_entrez <- tryCatch(
    clusterProfiler::bitr(
      input_genes,
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
  result <- tryCatch(
    ReactomePA::enrichPathway(
      gene = unique(as.character(symbol_to_entrez$ENTREZID)),
      organism = "human",
      pvalueCutoff = 1,
      pAdjustMethod = "BH",
      readable = TRUE
    ),
    error = function(err) NULL
  )
  subset_result(result, limit)
}

saved_terms <- normalize_saved_terms(saved_terms_raw)
fold_change_values <- normalize_fold_changes(fold_changes_raw)
available_saved_sources <- unique(vapply(saved_terms, `[[`, character(1), "source"))
candidate_sources <- unique(c(prefer_source, sources, available_saved_sources))
candidate_sources <- candidate_sources[nzchar(candidate_sources)]

build_result_for_source <- function(source_name, limit) {
  saved_result <- build_saved_result(saved_terms, source_name, limit)
  if (!is.null(saved_result)) {
    return(saved_result)
  }
  if (identical(source_name, "GO:BP")) {
    return(build_go_result(genes, limit))
  }
  if (identical(source_name, "REAC")) {
    return(build_reactome_result(genes, limit))
  }
  NULL
}

candidate_results <- list()
for (source_name in candidate_sources) {
  candidate_results[[source_name]] <- build_result_for_source(source_name, top_n)
}

pick_result <- function(preferred, candidates, plot_kind) {
  fallback_priority <- c("REAC", "GO:BP")
  candidate_order <- unique(c(preferred, names(candidates), fallback_priority))
  for (source_name in candidate_order) {
    current <- candidates[[source_name]]
    if (is.null(current)) {
      next
    }
    row_count <- nrow(as.data.frame(current))
    if (identical(plot_kind, "emapplot") && row_count >= 2) {
      return(list(source = source_name, object = current))
    }
    if (identical(plot_kind, "cnetplot") && row_count >= 1) {
      return(list(source = source_name, object = current))
    }
  }
  NULL
}

selected <- pick_result(prefer_source, candidate_results, plot_type)
if (is.null(selected)) {
  stop(paste0("No enrichment result contained enough terms to render ", plot_type, "."))
}

result_obj <- selected$object
term_count <- nrow(as.data.frame(result_obj))

build_candidate_limits <- function(limit, available_terms, minimum_terms) {
  candidates <- unique(c(limit, 8L, 6L, 5L, 4L, 3L, 2L, 1L))
  candidates <- pmax(as.integer(candidates), minimum_terms)
  candidates <- candidates[candidates <= available_terms]
  unique(candidates)
}

attempt_render <- function(base_result, candidate_plot_type, candidate_limit) {
  local_result <- subset_result(base_result, candidate_limit)
  local_term_count <- nrow(as.data.frame(local_result))
  if (identical(candidate_plot_type, "emapplot") && local_term_count < 2) {
    stop("Enrichment map rendering requires at least two enriched terms.")
  }
  if (identical(candidate_plot_type, "cnetplot") && local_term_count < 1) {
    stop("Gene-concept network rendering requires at least one enriched term.")
  }

  plot_title <- title
  if (identical(candidate_plot_type, "cnetplot")) {
    plot_title <- gsub("Enrichment Map", "Gene-Concept Network", plot_title, fixed = TRUE)
    plot_title <- gsub("Pathway Relationship Graph", "Gene-Concept Network", plot_title, fixed = TRUE)
    plot_title <- gsub("Map$", "Gene-Concept Network", plot_title)
  }

  if (identical(candidate_plot_type, "emapplot")) {
    local_result <- enrichplot::pairwise_termsim(local_result, method = "JC")
    plot_obj <- enrichplot::emapplot(
      local_result,
      showCategory = local_term_count,
      edge.params = list(min = 0.35),
      layout = "with_fr",
      node_label = if (local_term_count >= 6) "group" else "category",
      node_label_size = 3,
      size_category = 1.1,
      cex.params = list(category_node = 0.75)
    ) + ggplot2::ggtitle(plot_title)
  } else {
    cnet_args <- list(
      x = local_result,
      showCategory = local_term_count,
      circular = FALSE,
      color.params = list(edge = TRUE),
      node_label = "all",
      categorySizeBy = ~ -log10(p.adjust)
    )
    local_gene_sets <- tryCatch(local_result@geneSets, error = function(err) NULL)
    if (!is.null(local_gene_sets) && length(fold_change_values) > 0) {
      candidate_genes <- unique(unlist(local_gene_sets, use.names = FALSE))
      local_fc <- fold_change_values[names(fold_change_values) %in% candidate_genes]
      if (length(local_fc) > 0) {
        cnet_args$foldChange <- local_fc
        cnet_args$fc_threshold <- 1
      }
    }
    plot_obj <- do.call(enrichplot::cnetplot, cnet_args) + ggplot2::ggtitle(plot_title)
  }

  render_status <- gleskit_write_plot_files(plot_obj, png_path, svg_path, width = 10, height = 7, dpi = 150)
  list(
    render_mode = candidate_plot_type,
    rendered_term_count = local_term_count,
    rendered_top_n = candidate_limit,
    png_created = isTRUE(render_status$png_created),
    svg_created = isTRUE(render_status$svg_created)
  )
}

minimum_terms <- if (identical(plot_type, "emapplot")) 2L else 1L
candidate_limits <- build_candidate_limits(min(top_n, term_count), term_count, minimum_terms)
attempt_errors <- character(0)
render_result <- NULL

for (candidate_limit in candidate_limits) {
  render_result <- tryCatch(
    attempt_render(result_obj, plot_type, candidate_limit),
    error = function(err) {
      attempt_errors <<- c(
        attempt_errors,
        paste0(plot_type, "@", candidate_limit, ": ", conditionMessage(err))
      )
      NULL
    }
  )
  if (!is.null(render_result)) {
    break
  }
}

fallback_reason <- ""
if (is.null(render_result) && identical(plot_type, "emapplot")) {
  cnet_limits <- build_candidate_limits(min(top_n, term_count), term_count, 1L)
  for (candidate_limit in cnet_limits) {
    render_result <- tryCatch(
      attempt_render(result_obj, "cnetplot", candidate_limit),
      error = function(err) {
        attempt_errors <<- c(
          attempt_errors,
          paste0("cnetplot@", candidate_limit, ": ", conditionMessage(err))
        )
        NULL
      }
    )
    if (!is.null(render_result)) {
      fallback_reason <- "The requested enrichment map was too heavy to render cleanly, so GLESKit returned a lighter GLESKit-native gene-concept network instead."
      break
    }
  }
}

if (is.null(render_result)) {
  truncated <- paste(utils::tail(attempt_errors, n = 4), collapse = " | ")
  stop(paste0("GLESKit failed to render the requested enrichplot figure after multiple reduced-size attempts. ", truncated))
}

if (identical(render_result$render_mode, "emapplot")) {
  caption <- paste0(
    "ClusterProfiler/enrichplot enrichment map using ",
    selected$source,
    " terms. Nodes are enriched terms and edges reflect gene-overlap similarity."
  )
} else {
  caption <- paste0(
    "ClusterProfiler/enrichplot gene-concept network using ",
    selected$source,
    " terms. It links enriched terms to their contributing genes and labels shared genes to reduce clutter."
  )
}
if (nzchar(fallback_reason)) {
  caption <- paste(caption, fallback_reason)
}

gleskit_write_output(
  list(
    provider = "yulab_enrichplot",
    rendered_source = selected$source,
    requested_plot_type = plot_type,
    render_mode = render_result$render_mode,
    rendered_term_count = render_result$rendered_term_count,
    rendered_top_n = render_result$rendered_top_n,
    fallback_reason = fallback_reason,
    caption = caption,
    png_path = if (isTRUE(render_result$png_created)) png_path else "",
    svg_path = if (isTRUE(render_result$svg_created)) svg_path else ""
  ),
  output_path
)
