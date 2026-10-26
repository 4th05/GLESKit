script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_deseq2.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_deseq2.R")
input_path <- io$input_path
output_path <- io$output_path

gleskit_require_packages(c("DESeq2", "jsonlite"), "DESeq2 analysis")
request <- gleskit_read_request(input_path)

counts_path <- gleskit_string_value(request$counts_path, "")
if (!nzchar(counts_path) || !file.exists(counts_path)) {
  stop(paste0("DESeq2 counts file not found: ", counts_path))
}

sample_metadata_path <- gleskit_string_value(request$sample_metadata_path, "")
sample_metadata_text <- gleskit_string_value(request$sample_metadata_text, "")
gene_column <- gleskit_string_value(request$gene_column, "")
sample_column <- gleskit_string_value(request$sample_column, "sample")
condition_column <- gleskit_string_value(request$condition_column, "condition")
condition_a <- gleskit_string_value(request$condition_a, "")
condition_b <- gleskit_string_value(request$condition_b, "")
design_formula <- gleskit_string_value(request$design_formula, "")
label <- gleskit_string_value(request$label, "")
comparison <- gleskit_string_value(request$comparison, "")
min_count <- max(0L, gleskit_int_value(request$min_count, 10L))
top_n <- max(1L, min(gleskit_int_value(request$top_n, 25L), 100L))
padj_threshold <- suppressWarnings(as.numeric(request$padj_threshold[[1]]))
if (is.na(padj_threshold)) {
  padj_threshold <- 0.05
}
p_value_threshold <- suppressWarnings(as.numeric(request$p_value_threshold[[1]]))
if (is.na(p_value_threshold)) {
  p_value_threshold <- 0.05
}
base_mean_threshold <- suppressWarnings(as.numeric(request$base_mean_threshold[[1]]))
if (is.na(base_mean_threshold)) {
  base_mean_threshold <- 0
}
if (base_mean_threshold < 0) {
  stop("DESeq2 base_mean_threshold must be non-negative.")
}
significance_mode <- tolower(gleskit_string_value(request$significance_mode, "padj"))
if (!(significance_mode %in% c("padj", "p_value"))) {
  stop("DESeq2 significance_mode must be 'padj' or 'p_value'.")
}
log2_fold_change_threshold <- suppressWarnings(as.numeric(request$log2_fold_change_threshold[[1]]))
if (is.na(log2_fold_change_threshold)) {
  log2_fold_change_threshold <- 0
}
lfc_shrinkage <- tolower(gleskit_string_value(request$lfc_shrinkage, "none"))
if (!(lfc_shrinkage %in% c("none", "apeglm"))) {
  stop("DESeq2 lfc_shrinkage must be 'none' or 'apeglm'.")
}
if (identical(lfc_shrinkage, "apeglm")) {
  gleskit_require_packages(c("apeglm"), "DESeq2 LFC shrinkage")
}

read_table <- function(path) {
  ext <- tolower(tools::file_ext(path))
  if (ext %in% c("csv")) {
    utils::read.csv(path, check.names = FALSE, stringsAsFactors = FALSE)
  } else {
    utils::read.delim(path, check.names = FALSE, stringsAsFactors = FALSE)
  }
}

read_table_text <- function(text_value) {
  con <- textConnection(text_value)
  on.exit(close(con), add = TRUE)
  lines <- strsplit(text_value, "\n", fixed = TRUE)[[1]]
  nonempty_lines <- lines[nzchar(trimws(lines))]
  if (!length(nonempty_lines)) {
    stop("Inline sample metadata text is empty.")
  }
  first_line <- nonempty_lines[[1]]
  delimiter <- if (grepl("\t", first_line, fixed = TRUE)) {
    "\t"
  } else if (grepl(",", first_line, fixed = TRUE)) {
    ","
  } else {
    ""
  }
  utils::read.table(
    con,
    header = TRUE,
    sep = delimiter,
    check.names = FALSE,
    stringsAsFactors = FALSE
  )
}

canonical_token <- function(value) {
  token <- trimws(as.character(value))
  if (!nzchar(token)) {
    return("")
  }
  gsub("[^[:alnum:]]", "", tolower(token))
}

read_counts_matrix <- function(path) {
  ext <- tolower(tools::file_ext(path))
  readers <- list()
  if (ext %in% c("csv")) {
    readers <- list(
      function() utils::read.csv(path, check.names = FALSE, stringsAsFactors = FALSE),
      function() utils::read.table(path, header = TRUE, sep = ",", check.names = FALSE, stringsAsFactors = FALSE),
      function() utils::read.table(path, header = TRUE, sep = "", check.names = FALSE, stringsAsFactors = FALSE)
    )
  } else if (ext %in% c("tsv", "tab")) {
    readers <- list(
      function() utils::read.delim(path, check.names = FALSE, stringsAsFactors = FALSE),
      function() utils::read.table(path, header = TRUE, sep = "", check.names = FALSE, stringsAsFactors = FALSE),
      function() utils::read.csv(path, check.names = FALSE, stringsAsFactors = FALSE)
    )
  } else {
    readers <- list(
      function() utils::read.table(path, header = TRUE, sep = "", check.names = FALSE, stringsAsFactors = FALSE),
      function() utils::read.delim(path, check.names = FALSE, stringsAsFactors = FALSE),
      function() utils::read.csv(path, check.names = FALSE, stringsAsFactors = FALSE)
    )
  }
  last_error <- NULL
  for (reader in readers) {
    candidate <- tryCatch(reader(), error = function(e) e)
    if (inherits(candidate, "error")) {
      last_error <- conditionMessage(candidate)
      next
    }
    if (is.data.frame(candidate) && ncol(candidate) >= 2 && nrow(candidate) >= 1) {
      return(candidate)
    }
    if (is.data.frame(candidate) && ncol(candidate) == 1) {
      last_error <- "Counts table parsed into a single column."
    }
  }
  stop(paste0("DESeq2 could not parse counts table with a supported delimiter. Last error: ", last_error))
}

read_sample_metadata <- function(path, text_value) {
  if (nzchar(path)) {
    if (!file.exists(path)) {
      stop(paste0("Sample metadata file not found: ", path))
    }
    return(read_table(path))
  }
  if (nzchar(text_value)) {
    return(read_table_text(text_value))
  }
  stop("DESeq2 requires sample metadata via sample_metadata_path or sample_metadata_text.")
}

resolve_sample_column <- function(metadata_df, requested_sample_column, count_sample_ids) {
  requested <- gleskit_string_value(requested_sample_column, "")
  candidate_columns <- names(metadata_df)
  if (!length(candidate_columns)) {
    stop("DESeq2 sample metadata table does not contain any columns.")
  }

  score_column <- function(column_name) {
    values <- trimws(as.character(metadata_df[[column_name]]))
    values <- values[nzchar(values)]
    if (!length(values)) {
      return(0L)
    }
    metadata_tokens <- unique(vapply(values, canonical_token, character(1)))
    count_tokens <- unique(vapply(count_sample_ids, canonical_token, character(1)))
    sum(metadata_tokens %in% count_tokens)
  }

  scores <- vapply(candidate_columns, score_column, integer(1))
  best_index <- which.max(scores)
  best_score <- if (length(best_index) > 0) scores[[best_index]] else 0L
  requested_score <- if (nzchar(requested) && requested %in% candidate_columns) scores[[requested]] else NA_integer_

  if (nzchar(requested) && requested %in% candidate_columns && requested_score >= 2L) {
    return(list(sample_column = requested, inferred = FALSE, requested_score = requested_score, best_score = best_score, canonical_matching = TRUE))
  }
  if (best_score >= 2L) {
    return(
      list(
        sample_column = candidate_columns[[best_index]],
        inferred = !identical(candidate_columns[[best_index]], requested),
        requested_score = requested_score,
        best_score = best_score,
        canonical_matching = TRUE
      )
    )
  }

  if (nzchar(requested) && requested %in% candidate_columns) {
    return(list(sample_column = requested, inferred = FALSE, requested_score = requested_score, best_score = best_score, canonical_matching = FALSE))
  }

  preferred_names <- c("sample", "sample_id", "sampleid", "barcode", "sample_barcode", "submitter_id", "id")
  for (preferred in preferred_names) {
    matches <- candidate_columns[tolower(candidate_columns) == preferred]
    if (length(matches) > 0) {
      return(list(sample_column = matches[[1]], inferred = TRUE, requested_score = requested_score, best_score = best_score, canonical_matching = FALSE))
    }
  }

  return(list(sample_column = candidate_columns[[1]], inferred = TRUE, requested_score = requested_score, best_score = best_score, canonical_matching = FALSE))
}

resolve_condition_level <- function(value, allowed_levels) {
  token <- trimws(as.character(value))
  if (!nzchar(token)) {
    return(list(value = token, note = NULL, ambiguous = FALSE))
  }
  if (token %in% allowed_levels) {
    return(list(value = token, note = NULL, ambiguous = FALSE))
  }
  upper_levels <- toupper(allowed_levels)
  upper_token <- toupper(token)
  if (upper_token %in% upper_levels) {
    resolved <- allowed_levels[[which(upper_levels == upper_token)[1]]]
    return(list(value = resolved, note = paste0("Normalized condition from ", token, " to ", resolved, " using case-insensitive match."), ambiguous = FALSE))
  }
  canonical_levels <- vapply(allowed_levels, canonical_token, character(1))
  canonical_input <- canonical_token(token)
  exact_canonical <- allowed_levels[canonical_levels == canonical_input]
  if (length(exact_canonical) == 1) {
    resolved <- exact_canonical[[1]]
    return(list(value = resolved, note = paste0("Normalized condition from ", token, " to ", resolved, " using canonical token match."), ambiguous = FALSE))
  }
  substring_matches <- allowed_levels[
    vapply(
      canonical_levels,
      function(level_token) {
        nzchar(level_token) && (
          grepl(canonical_input, level_token, fixed = TRUE) ||
          grepl(level_token, canonical_input, fixed = TRUE)
        )
      },
      logical(1)
    )
  ]
  if (length(substring_matches) == 1) {
    resolved <- substring_matches[[1]]
    return(list(value = resolved, note = paste0("Normalized condition from ", token, " to ", resolved, " using conservative substring match."), ambiguous = FALSE))
  }
  if (length(substring_matches) > 1) {
    lengths <- nchar(substring_matches)
    shortest <- substring_matches[lengths == min(lengths)]
    if (length(shortest) == 1) {
      resolved <- shortest[[1]]
      return(list(value = resolved, note = paste0("Normalized condition from ", token, " to ", resolved, " using shortest conservative substring match."), ambiguous = FALSE))
    }
    return(list(value = token, note = NULL, ambiguous = TRUE))
  }
  list(value = token, note = NULL, ambiguous = FALSE)
}

resolve_apeglm_coef <- function(candidate_names, condition_column, condition_a, condition_b) {
  sanitize_coef_token <- function(value) {
    gsub("[^[:alnum:]_]", "", gsub("[[:space:]-]+", "_", as.character(value)))
  }
  expected_exact <- paste0(condition_column, "_", condition_a, "_vs_", condition_b)
  if (expected_exact %in% candidate_names) {
    return(expected_exact)
  }
  expected_sanitized <- sanitize_coef_token(expected_exact)
  sanitized_names <- vapply(candidate_names, sanitize_coef_token, character(1))
  exact_sanitized <- candidate_names[sanitized_names == expected_sanitized]
  if (length(exact_sanitized) == 1) {
    return(exact_sanitized[[1]])
  }
  suffix_pattern <- paste0(sanitize_coef_token(condition_a), "_vs_", sanitize_coef_token(condition_b), "$")
  prefix_pattern <- paste0("^", sanitize_coef_token(condition_column), "_")
  relaxed <- candidate_names[
    grepl(prefix_pattern, sanitized_names) &
    grepl(suffix_pattern, sanitized_names)
  ]
  if (length(relaxed) == 1) {
    return(relaxed[[1]])
  }
  stop(
    paste0(
      "DESeq2 apeglm shrinkage could not identify a unique coefficient for the requested contrast. ",
      "Requested contrast: ", condition_a, " vs ", condition_b, ". Available coefficients: ",
      paste(candidate_names, collapse = ", ")
    )
  )
}

counts_df <- read_counts_matrix(counts_path)
if (nrow(counts_df) == 0) {
  stop("DESeq2 counts table is empty.")
}

metadata_df <- read_sample_metadata(sample_metadata_path, sample_metadata_text)
if (nrow(metadata_df) == 0) {
  stop("DESeq2 sample metadata table is empty.")
}

header <- names(counts_df)
if (!nzchar(header[[1]])) {
  names(counts_df)[[1]] <- "gene_id"
  header <- names(counts_df)
}
gene_column_name <- gene_column
if (!nzchar(gene_column_name)) {
  gene_column_name <- header[[1]]
}
if (!gene_column_name %in% header) {
  stop(paste0("Counts gene column not found: ", gene_column_name))
}
if (!condition_column %in% names(metadata_df)) {
  stop(paste0("Sample metadata missing required condition column: ", condition_column))
}

counts_df[[gene_column_name]] <- trimws(as.character(counts_df[[gene_column_name]]))
counts_df <- counts_df[nzchar(counts_df[[gene_column_name]]), , drop = FALSE]
rownames(counts_df) <- counts_df[[gene_column_name]]
counts_df[[gene_column_name]] <- NULL

count_sample_ids <- colnames(counts_df)
resolved_sample <- resolve_sample_column(metadata_df, sample_column, count_sample_ids)
sample_column_name <- resolved_sample$sample_column
analysis_notes <- character()
if (!identical(sample_column_name, sample_column)) {
  analysis_notes <- c(
    analysis_notes,
    paste0(
      "Sample column inferred as ",
      sample_column_name,
      " from overlap with count headers."
    )
  )
}
sample_column <- sample_column_name

sample_ids <- trimws(as.character(metadata_df[[sample_column]]))
condition_values <- trimws(as.character(metadata_df[[condition_column]]))
metadata_df[[sample_column]] <- sample_ids
metadata_df[[condition_column]] <- condition_values
metadata_df <- metadata_df[nzchar(sample_ids) & nzchar(condition_values), , drop = FALSE]
metadata_tokens <- vapply(metadata_df[[sample_column]], canonical_token, character(1))
count_sample_ids <- colnames(counts_df)
count_tokens <- vapply(count_sample_ids, canonical_token, character(1))

duplicate_metadata_tokens <- names(which(table(metadata_tokens[metadata_tokens != ""]) > 1))
if (length(duplicate_metadata_tokens) > 0) {
  stop("DESeq2 sample metadata contains duplicate sample identifiers after canonical normalization.")
}

matched_positions <- match(count_tokens, metadata_tokens)
matched_mask <- !is.na(matched_positions)
count_samples <- count_sample_ids[matched_mask]
sample_match_notes <- character()
if (any(matched_mask) && any(count_sample_ids[matched_mask] != metadata_df[[sample_column]][matched_positions[matched_mask]])) {
  sample_match_notes <- c(sample_match_notes, "Sample matching used canonical normalization between count headers and metadata sample ids.")
}
if (any(!matched_mask)) {
  unmatched_samples <- count_sample_ids[!matched_mask]
  sample_match_notes <- c(
    sample_match_notes,
    paste0("Unmatched count columns were ignored: ", paste(unmatched_samples, collapse = ", "), ".")
  )
}
if (nzchar(sample_metadata_text) && any(!matched_mask)) {
  stop(
    paste0(
      "Inline sample_metadata_text matched only ", sum(matched_mask), " of ", length(count_sample_ids),
      " count columns after canonical sample matching. Provide a complete sample_metadata_path instead of partial inline metadata."
    )
  )
}
if (length(count_samples) < 2) {
  stop(
    paste0(
      "DESeq2 requires at least two matched sample columns between counts and sample metadata. ",
      "Using sample_column=", sample_column, " matched ", length(count_samples), " sample(s)."
    )
  )
}

counts_df <- counts_df[, count_samples, drop = FALSE]
metadata_df <- metadata_df[matched_positions[matched_mask], , drop = FALSE]
if (anyNA(metadata_df[[sample_column]])) {
  stop("DESeq2 sample metadata did not cover all count matrix columns.")
}

numeric_counts <- as.matrix(counts_df)
storage.mode(numeric_counts) <- "numeric"
if (anyNA(numeric_counts)) {
  stop("DESeq2 counts matrix contains non-numeric values.")
}

keep_rows <- rowSums(numeric_counts, na.rm = TRUE) >= min_count
filtered_counts <- numeric_counts[keep_rows, , drop = FALSE]
if (nrow(filtered_counts) == 0) {
  stop("DESeq2 filtering removed all rows.")
}

condition_values <- metadata_df[[condition_column]]
condition_levels <- unique(condition_values)
if (nzchar(condition_a) && nzchar(condition_b)) {
  resolved_condition_a <- resolve_condition_level(condition_a, condition_levels)
  resolved_condition_b <- resolve_condition_level(condition_b, condition_levels)
  normalized_condition_a <- resolved_condition_a$value
  normalized_condition_b <- resolved_condition_b$value
  if (resolved_condition_a$ambiguous || resolved_condition_b$ambiguous) {
    stop(
      paste0(
        "Provided DESeq2 contrast levels were ambiguous. Available levels for ",
        condition_column,
        ": ",
        paste(condition_levels, collapse = ", "),
        "."
      )
    )
  }
  if (!(normalized_condition_a %in% condition_levels) || !(normalized_condition_b %in% condition_levels)) {
    stop(
      paste0(
        "Provided DESeq2 contrast levels were not found in the sample metadata. Available levels for ",
        condition_column,
        ": ",
        paste(condition_levels, collapse = ", "),
        "."
      )
    )
  }
  if (!is.null(resolved_condition_a$note) && nzchar(resolved_condition_a$note)) {
    analysis_notes <- c(analysis_notes, resolved_condition_a$note)
  }
  if (!is.null(resolved_condition_b$note) && nzchar(resolved_condition_b$note)) {
    analysis_notes <- c(analysis_notes, resolved_condition_b$note)
  }
  condition_a <- normalized_condition_a
  condition_b <- normalized_condition_b
  contrast <- c(condition_column, condition_a, condition_b)
  contrast_label <- paste0(condition_a, "_vs_", condition_b)
} else {
  if (length(condition_levels) != 2) {
    stop(
      paste0(
        "DESeq2 requires explicit condition_a and condition_b when ",
        condition_column,
        " has ",
        length(condition_levels),
        " levels: ",
        paste(condition_levels, collapse = ", "),
        ". Run one pairwise comparison at a time."
      )
    )
  }
  contrast <- c(condition_column, condition_levels[[2]], condition_levels[[1]])
  contrast_label <- paste0(condition_levels[[2]], "_vs_", condition_levels[[1]])
}

if (nzchar(design_formula)) {
  design <- stats::as.formula(design_formula)
} else {
  design <- stats::as.formula(paste0("~ ", condition_column))
}

if (nzchar(condition_a) && nzchar(condition_b)) {
  remaining_levels <- condition_levels[!(condition_levels %in% c(condition_b, condition_a))]
  ordered_levels <- unique(c(condition_b, condition_a, remaining_levels))
  metadata_df[[condition_column]] <- factor(
    as.character(metadata_df[[condition_column]]),
    levels = ordered_levels
  )
  metadata_df[[condition_column]] <- stats::relevel(metadata_df[[condition_column]], ref = condition_b)
} else {
  metadata_df[[condition_column]] <- factor(as.character(metadata_df[[condition_column]]))
}

dds <- DESeq2::DESeqDataSetFromMatrix(
  countData = round(filtered_counts),
  colData = metadata_df,
  design = design
)
dds <- DESeq2::DESeq(dds, quiet = TRUE)
gene_dispersions <- DESeq2::dispersions(dds)
if (length(gene_dispersions) == nrow(dds)) {
  dispersion_names <- names(gene_dispersions)
  if (is.null(dispersion_names) || !any(dispersion_names %in% rownames(dds))) {
    names(gene_dispersions) <- rownames(dds)
  }
}
dispersion_for_gene <- function(gene_id) {
  if (!nzchar(gene_id) || is.null(names(gene_dispersions))) {
    return(NA_real_)
  }
  index <- match(gene_id, names(gene_dispersions))
  if (is.na(index)) {
    return(NA_real_)
  }
  suppressWarnings(as.numeric(gene_dispersions[[index]]))
}
candidate_names <- DESeq2::resultsNames(dds)
coef_name <- ""
if (identical(lfc_shrinkage, "apeglm")) {
  coef_name <- resolve_apeglm_coef(candidate_names, condition_column, contrast[[2]], contrast[[3]])
  res <- DESeq2::results(dds, name = coef_name, alpha = padj_threshold)
  res <- DESeq2::lfcShrink(dds, coef = coef_name, type = "apeglm", res = res, quiet = TRUE)
} else {
  res <- DESeq2::results(dds, contrast = contrast, alpha = padj_threshold)
}
res_df <- as.data.frame(res)
res_df$gene <- rownames(res_df)
res_df <- res_df[order(res_df$padj, abs(res_df$log2FoldChange), na.last = TRUE), , drop = FALSE]
res_df <- res_df[!is.na(res_df$gene) & nzchar(res_df$gene), , drop = FALSE]

rank_score <- res_df$stat
rank_score[is.na(rank_score)] <- res_df$log2FoldChange[is.na(rank_score)]

safe_numeric_cell <- function(frame, column_name) {
  if (!nzchar(column_name) || !(column_name %in% colnames(frame))) {
    return(NA_real_)
  }
  value <- frame[[column_name]]
  if (length(value) < 1) {
    return(NA_real_)
  }
  suppressWarnings(as.numeric(value[[1]]))
}

results_rows <- lapply(seq_len(nrow(res_df)), function(i) {
  row <- res_df[i, , drop = FALSE]
  gene_id <- gleskit_string_value(row$gene, "")
  list(
    gene = gene_id,
    base_mean = safe_numeric_cell(row, "baseMean"),
    log2_fold_change = safe_numeric_cell(row, "log2FoldChange"),
    lfc_se = safe_numeric_cell(row, "lfcSE"),
    stat = safe_numeric_cell(row, "stat"),
    p_value = safe_numeric_cell(row, "pvalue"),
    padj = safe_numeric_cell(row, "padj"),
    dispersion = dispersion_for_gene(gene_id)
  )
})

top_rows <- head(results_rows, top_n)
passes_base_mean <- function(row) {
  if (base_mean_threshold <= 0) {
    return(TRUE)
  }
  !is.null(row$base_mean) && !is.na(row$base_mean) && row$base_mean >= base_mean_threshold
}

passes_significance <- function(row) {
  significance_value <- if (identical(significance_mode, "p_value")) row$p_value else row$padj
  significance_cutoff <- if (identical(significance_mode, "p_value")) p_value_threshold else padj_threshold
  !is.null(significance_value) && !is.na(significance_value) && significance_value <= significance_cutoff && passes_base_mean(row)
}

significant_rows <- Filter(passes_significance, results_rows)
up_rows <- Filter(function(row) !is.null(row$log2_fold_change) && !is.na(row$log2_fold_change) && row$log2_fold_change >= log2_fold_change_threshold && passes_significance(row), results_rows)
down_rows <- Filter(function(row) !is.null(row$log2_fold_change) && !is.na(row$log2_fold_change) && row$log2_fold_change <= -log2_fold_change_threshold && passes_significance(row), results_rows)

ranked_gene_scores <- lapply(seq_len(nrow(res_df)), function(i) {
  row <- res_df[i, , drop = FALSE]
  gene_id <- gleskit_string_value(row$gene, "")
  score <- safe_numeric_cell(row, "stat")
  score_source <- "stat"
  if (is.na(score)) {
    score <- safe_numeric_cell(row, "log2FoldChange")
    score_source <- "log2FoldChange"
  }
  list(
    gene = gene_id,
    score = score,
    score_source = score_source,
    log2_fold_change = safe_numeric_cell(row, "log2FoldChange"),
    padj = safe_numeric_cell(row, "padj"),
    p_value = safe_numeric_cell(row, "pvalue"),
    dispersion = dispersion_for_gene(gene_id)
  )
})

qc_summary <- list(
  row_count = nrow(counts_df),
  filtered_row_count = nrow(filtered_counts),
  sample_count = length(count_samples),
  condition_column = condition_column,
  sample_column = sample_column,
  sample_matching = list(
    requested_sample_column = gleskit_string_value(request$sample_column, "sample"),
    resolved_sample_column = sample_column,
    matched_sample_count = length(count_samples),
    canonical_matching = isTRUE(resolved_sample$canonical_matching),
    notes = as.list(sample_match_notes)
  ),
  dispersion_non_na_count = sum(vapply(results_rows, function(row) !is.null(row$dispersion) && !is.na(row$dispersion), logical(1))),
  dispersion_tested_count = length(results_rows),
  condition_levels = as.list(unique(as.character(metadata_df[[condition_column]]))),
  contrast_label = contrast_label,
  contrast = list(
    condition_a = condition_a,
    condition_b = condition_b,
    available_levels = as.list(condition_levels)
  ),
  reference_level = if (length(levels(metadata_df[[condition_column]])) >= 1) levels(metadata_df[[condition_column]])[[1]] else "",
  apeglm = list(
    coefficient = if (nzchar(coef_name)) coef_name else NULL,
    available_coefficients = as.list(candidate_names)
  ),
  min_count = min_count,
  padj_threshold = padj_threshold,
  p_value_threshold = p_value_threshold,
  base_mean_threshold = base_mean_threshold,
  significance_mode = significance_mode,
  log2_fold_change_threshold = log2_fold_change_threshold,
  lfc_shrinkage = lfc_shrinkage
)

output <- list(
  provider = "deseq2",
  summary = paste0(
    "Ran DESeq2 on ", nrow(filtered_counts), " filtered gene(s) across ", length(count_samples),
    " sample(s) using contrast ", contrast_label, ". ",
    if (length(analysis_notes) > 0) paste0(paste(analysis_notes, collapse = " "), " ") else "",
    length(significant_rows), " significant gene(s) were identified."
  ),
  input = list(
    counts_path = normalizePath(counts_path),
    counts_source = "file",
    sample_metadata_path = if (nzchar(sample_metadata_path)) normalizePath(sample_metadata_path) else "",
    sample_metadata_source = if (nzchar(sample_metadata_path)) "file" else "inline",
    label = label,
    comparison = comparison,
    gene_column = gene_column_name,
    sample_column = sample_column,
    condition_column = condition_column,
    condition_a = condition_a,
    condition_b = condition_b,
    design_formula = design_formula,
    lfc_shrinkage = lfc_shrinkage
  ),
  counts = qc_summary,
  summary_stats = list(
    total_gene_count = nrow(counts_df),
    filtered_gene_count = nrow(filtered_counts),
    sample_count = length(count_samples),
    significant_gene_count = length(significant_rows),
    upregulated_count = length(up_rows),
    downregulated_count = length(down_rows),
    contrast_label = contrast_label,
    padj_threshold = padj_threshold,
    p_value_threshold = p_value_threshold,
    base_mean_threshold = base_mean_threshold,
    significance_mode = significance_mode,
    log2_fold_change_threshold = log2_fold_change_threshold,
    lfc_shrinkage = lfc_shrinkage
  ),
  top_genes = list(
    upregulated = head(up_rows, top_n),
    downregulated = head(down_rows, top_n)
  ),
  significant_gene_sets = list(
    upregulated = unique(vapply(up_rows, function(row) row$gene, character(1))),
    downregulated = unique(vapply(down_rows, function(row) row$gene, character(1))),
    all_significant = unique(vapply(significant_rows, function(row) row$gene, character(1)))
  ),
  ranked_gene_scores = ranked_gene_scores,
  results = results_rows
)

gleskit_write_output(output, output_path)
