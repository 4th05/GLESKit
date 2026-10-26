#!/usr/bin/env Rscript

suppressPackageStartupMessages({
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 6) {
  stop("Usage: download_study.R <provider_id> <dataset_id> <tissue> <counts_out> <metadata_out> <summary_out> [sample_limit] [exclude_sample_ids_path] [target_new_samples]")
}

provider_id <- tolower(as.character(args[[1]]))
dataset_id <- as.character(args[[2]])
tissue <- as.character(args[[3]])
counts_out <- as.character(args[[4]])
metadata_out <- as.character(args[[5]])
summary_out <- as.character(args[[6]])
sample_limit_arg <- suppressWarnings(as.integer(if (length(args) >= 7) args[[7]] else NA))
if (!is.finite(sample_limit_arg) || sample_limit_arg < 2) {
  sample_limit_arg <- NA_integer_
}
exclude_sample_ids_path <- as.character(if (length(args) >= 8) args[[8]] else "")
target_new_samples_arg <- suppressWarnings(as.integer(if (length(args) >= 9) args[[9]] else NA))
if (!is.finite(target_new_samples_arg) || target_new_samples_arg < 1) {
  target_new_samples_arg <- NA_integer_
}

emit_progress <- function(stage, status, meta = list()) {
  payload <- list(stage = stage, status = status, meta = meta)
  cat(sprintf("GEARS_PROGRESS:%s\n", toJSON(payload, auto_unbox = TRUE, null = "null")))
  flush.console()
}

collapse_cell_value <- function(value) {
  if (is.null(value)) {
    return("")
  }
  if (length(value) == 0) {
    return("")
  }
  if (is.list(value)) {
    flat <- unlist(value, recursive = TRUE, use.names = FALSE)
    if (length(flat) == 0) {
      return("")
    }
    return(paste(as.character(flat), collapse = "|"))
  }
  if (is.atomic(value)) {
    return(paste(as.character(value), collapse = "|"))
  }
  as.character(value)
}

normalize_df_for_csv <- function(df) {
  if (is.null(df) || !is.data.frame(df)) {
    return(data.frame())
  }
  out <- as.data.frame(df, stringsAsFactors = FALSE, check.names = FALSE)
  for (name in colnames(out)) {
    col <- out[[name]]
    if (is.list(col)) {
      out[[name]] <- vapply(col, collapse_cell_value, character(1), USE.NAMES = FALSE)
    } else if (is.factor(col) || inherits(col, "POSIXt") || inherits(col, "Date")) {
      out[[name]] <- as.character(col)
    }
  }
  rownames(out) <- NULL
  out
}

write_output <- function(counts_df, metadata_df, summary = list(), meta = list()) {
  dir.create(dirname(counts_out), recursive = TRUE, showWarnings = FALSE)
  dir.create(dirname(metadata_out), recursive = TRUE, showWarnings = FALSE)
  dir.create(dirname(summary_out), recursive = TRUE, showWarnings = FALSE)

  counts_df <- normalize_df_for_csv(counts_df)
  metadata_df <- normalize_df_for_csv(metadata_df)

  write.csv(counts_df, counts_out, row.names = FALSE)
  write.csv(metadata_df, metadata_out, row.names = FALSE)
  payload <- list(summary = summary, meta = meta)
  write(toJSON(payload, auto_unbox = TRUE, pretty = TRUE), file = summary_out)
}

as_hgnc_like <- function(values) {
  values <- as.character(values)
  values <- gsub("^.*\\|", "", values)
  values <- gsub("\\..*$", "", values)
  values[values == ""] <- NA
  values
}

canonical_sample_token <- function(value) {
  token <- tolower(trimws(as.character(value)))
  gsub("[^a-z0-9]+", "", token)
}

load_excluded_sample_ids <- function(path_value) {
  path_token <- trimws(as.character(path_value))
  if (!nzchar(path_token) || !file.exists(path_token)) {
    return(character())
  }
  lines <- readLines(path_token, warn = FALSE, encoding = "UTF-8")
  lines <- trimws(as.character(lines))
  lines <- lines[nzchar(lines)]
  unique(lines)
}

filter_sample_ids <- function(sample_ids, excluded_ids = character(), target_new_samples = NA_integer_, sample_limit = NA_integer_) {
  sample_ids <- as.character(sample_ids)
  excluded_count <- 0L

  if (length(excluded_ids) > 0) {
    excluded_canonical <- canonical_sample_token(excluded_ids)
    excluded_canonical <- unique(excluded_canonical[nzchar(excluded_canonical)])
    if (length(excluded_canonical) > 0) {
      keep_mask <- !(canonical_sample_token(sample_ids) %in% excluded_canonical)
      excluded_count <- as.integer(sum(!keep_mask))
      sample_ids <- sample_ids[keep_mask]
    }
  }

  target_limit <- NA_integer_
  if (is.finite(target_new_samples) && target_new_samples >= 1) {
    target_limit <- as.integer(target_new_samples)
  } else if (is.finite(sample_limit) && sample_limit >= 2) {
    target_limit <- as.integer(sample_limit)
  }
  if (is.finite(target_limit) && length(sample_ids) > target_limit) {
    sample_ids <- sample_ids[seq_len(target_limit)]
  }

  list(
    sample_ids = sample_ids,
    excluded_count = as.integer(excluded_count),
    target_limit = if (is.finite(target_limit)) as.integer(target_limit) else NA_integer_
  )
}

extract_tcga_result_sample_ids <- function(result_df) {
  if (is.null(result_df) || !is.data.frame(result_df) || nrow(result_df) == 0) {
    return(character())
  }
  preferred_col <- pick_column(
    result_df,
    c(
      "sample.submitter_id",
      "cases.0.samples.0.submitter_id",
      "submitter_id",
      "sample_id",
      "barcode",
      "sample",
      "cases.submitter_id",
      "cases.0.submitter_id"
    )
  )
  if (nzchar(preferred_col)) {
    col <- result_df[[preferred_col]]
    if (is.list(col)) {
      return(vapply(col, collapse_cell_value, character(1), USE.NAMES = FALSE))
    }
    return(as.character(col))
  }
  list_cols <- which(vapply(result_df, is.list, logical(1)))
  if (length(list_cols) > 0) {
    col <- result_df[[list_cols[[1]]]]
    return(vapply(col, collapse_cell_value, character(1), USE.NAMES = FALSE))
  }
  rep("", nrow(result_df))
}

extract_tcga_result_file_ids <- function(result_df) {
  if (is.null(result_df) || !is.data.frame(result_df) || nrow(result_df) == 0) {
    return(character())
  }
  preferred_col <- pick_column(
    result_df,
    c("id", "file_id", "file.id", "fileid", "cases.0.samples.0.portions.0.analytes.0.aliquots.0.aliquot_id")
  )
  if (nzchar(preferred_col)) {
    col <- result_df[[preferred_col]]
    if (is.list(col)) {
      return(vapply(col, collapse_cell_value, character(1), USE.NAMES = FALSE))
    }
    return(as.character(col))
  }
  rep("", nrow(result_df))
}

extract_tcga_result_case_ids <- function(result_df) {
  if (is.null(result_df) || !is.data.frame(result_df) || nrow(result_df) == 0) {
    return(character())
  }
  preferred_col <- pick_column(
    result_df,
    c(
      "cases.0.submitter_id",
      "cases.submitter_id",
      "case.submitter_id",
      "submitter_id",
      "case_id",
      "case"
    )
  )
  if (nzchar(preferred_col)) {
    col <- result_df[[preferred_col]]
    if (is.list(col)) {
      return(vapply(col, collapse_cell_value, character(1), USE.NAMES = FALSE))
    }
    return(as.character(col))
  }
  rep("", nrow(result_df))
}

extract_tcga_result_project_ids <- function(result_df) {
  if (is.null(result_df) || !is.data.frame(result_df) || nrow(result_df) == 0) {
    return(character())
  }
  preferred_col <- pick_column(
    result_df,
    c(
      "cases.0.project.project_id",
      "cases.project.project_id",
      "project.project_id",
      "project_id",
      "project"
    )
  )
  if (nzchar(preferred_col)) {
    col <- result_df[[preferred_col]]
    if (is.list(col)) {
      return(vapply(col, collapse_cell_value, character(1), USE.NAMES = FALSE))
    }
    return(as.character(col))
  }
  rep("", nrow(result_df))
}

filter_tcga_query_rows <- function(result_df, excluded_ids = character(), target_new_samples = NA_integer_, sample_limit = NA_integer_) {
  if (is.null(result_df) || !is.data.frame(result_df) || nrow(result_df) == 0) {
    out <- result_df
    attr(out, "excluded_count") <- 0L
    attr(out, "target_limit") <- NA_integer_
    return(out)
  }
  sample_ids <- extract_tcga_result_sample_ids(result_df)
  filtered <- filter_sample_ids(
    sample_ids,
    excluded_ids = excluded_ids,
    target_new_samples = target_new_samples,
    sample_limit = sample_limit
  )
  keep_samples <- as.character(filtered$sample_ids)
  if (length(keep_samples) == 0) {
    out <- result_df[FALSE, , drop = FALSE]
    attr(out, "excluded_count") <- as.integer(filtered$excluded_count)
    attr(out, "target_limit") <- filtered$target_limit
    return(out)
  }
  keep_tokens <- unique(canonical_sample_token(keep_samples))
  row_tokens <- canonical_sample_token(sample_ids)
  keep_mask <- row_tokens %in% keep_tokens
  out <- result_df[keep_mask, , drop = FALSE]
  if (nrow(out) > length(keep_samples)) {
    out <- out[seq_len(length(keep_samples)), , drop = FALSE]
  }
  attr(out, "excluded_count") <- as.integer(filtered$excluded_count)
  attr(out, "target_limit") <- filtered$target_limit
  out
}

canonical_token_set <- function(values) {
  tokens <- canonical_sample_token(values)
  unique(tokens[nzchar(tokens)])
}

collect_tcga_cached_file_inventory <- function(project_id) {
  inventory <- list(
    tokens = character(),
    scanned_sample_dirs = 0L,
    valid_sample_dirs = 0L,
    broken_sample_dirs = 0L,
    valid_files = 0L
  )
  base_dir <- file.path(
    "GDCdata",
    as.character(project_id),
    "Transcriptome_Profiling",
    "Gene_Expression_Quantification"
  )
  if (!dir.exists(base_dir)) {
    return(inventory)
  }
  is_valid_counts_file <- function(path) {
    info <- file.info(path)
    if (is.null(info) || nrow(info) == 0) {
      return(FALSE)
    }
    size <- suppressWarnings(as.numeric(info$size[[1]]))
    if (!is.finite(size) || size < 1024) {
      return(FALSE)
    }
    con <- NULL
    lines <- character()
    ok <- FALSE
    try({
      if (grepl("\\.gz$", tolower(path))) {
        con <- gzfile(path, open = "rt")
      } else {
        con <- file(path, open = "rt")
      }
      lines <- readLines(con, n = 4, warn = FALSE)
      close(con)
      con <- NULL
      if (length(lines) >= 1) {
        scope <- tolower(paste(as.character(lines), collapse = "\n"))
        ok <- grepl("gene", scope, fixed = TRUE) && grepl("unstranded", scope, fixed = TRUE)
      }
    }, silent = TRUE)
    if (!is.null(con)) {
      try(close(con), silent = TRUE)
    }
    isTRUE(ok)
  }

  dir_paths <- list.dirs(base_dir, recursive = FALSE, full.names = TRUE)
  if (length(dir_paths) == 0) {
    return(inventory)
  }
  scanned_dirs <- 0L
  valid_sample_dirs <- 0L
  broken_sample_dirs <- 0L
  valid_files_total <- 0L
  dir_ids <- character()
  file_ids <- character()
  for (dir_path in dir_paths) {
    scanned_dirs <- scanned_dirs + 1L
    payload_files <- list.files(
      dir_path,
      recursive = TRUE,
      full.names = TRUE,
      include.dirs = FALSE,
      pattern = "star_gene_counts\\.(tsv|txt)(\\.gz)?$"
    )
    if (length(payload_files) == 0) {
      broken_sample_dirs <- broken_sample_dirs + 1L
      next
    }
    valid_payload_files <- payload_files[vapply(payload_files, is_valid_counts_file, logical(1))]
    if (length(valid_payload_files) == 0) {
      broken_sample_dirs <- broken_sample_dirs + 1L
      next
    }
    valid_sample_dirs <- valid_sample_dirs + 1L
    valid_files_total <- valid_files_total + as.integer(length(valid_payload_files))
    dir_ids <- c(dir_ids, basename(dir_path))
    file_ids <- c(file_ids, basename(valid_payload_files))
  }
  file_uuid <- gsub("^([0-9a-fA-F-]{36}).*$", "\\1", file_ids)
  raw_values <- c(dir_ids, file_ids, file_uuid)
  inventory$tokens <- canonical_token_set(raw_values)
  inventory$scanned_sample_dirs <- as.integer(scanned_dirs)
  inventory$valid_sample_dirs <- as.integer(valid_sample_dirs)
  inventory$broken_sample_dirs <- as.integer(broken_sample_dirs)
  inventory$valid_files <- as.integer(valid_files_total)
  inventory
}

exclude_tcga_cached_rows <- function(result_df, cached_file_tokens = character()) {
  if (is.null(result_df) || !is.data.frame(result_df) || nrow(result_df) == 0) {
    out <- result_df
    attr(out, "cached_count") <- 0L
    return(out)
  }
  cached_tokens <- canonical_token_set(cached_file_tokens)
  if (length(cached_tokens) == 0) {
    out <- result_df
    attr(out, "cached_count") <- 0L
    return(out)
  }
  file_ids <- extract_tcga_result_file_ids(result_df)
  row_tokens <- canonical_sample_token(file_ids)
  keep_mask <- !(row_tokens %in% cached_tokens)
  cached_count <- as.integer(sum(!keep_mask))
  out <- result_df[keep_mask, , drop = FALSE]
  attr(out, "cached_count") <- cached_count
  out
}

normalize_counts_matrix <- function(mat, make_integer = TRUE) {
  mat <- as.matrix(mat)
  storage.mode(mat) <- "numeric"
  mat[is.na(mat)] <- 0

  if (make_integer) {
    # Some public endpoints expose log-scale expression; convert to pseudo-count scale.
    if (max(mat, na.rm = TRUE) < 50 && any(abs(mat - round(mat)) > 0.0001, na.rm = TRUE)) {
      mat <- (2^mat) - 1
    }
    mat <- round(pmax(mat, 0))
    storage.mode(mat) <- "integer"
  }

  mat
}

pick_column <- function(df, candidates) {
  if (is.null(df) || ncol(df) == 0) {
    return("")
  }
  names_l <- tolower(colnames(df))
  for (candidate in candidates) {
    idx <- which(names_l == tolower(candidate))[1]
    if (!is.na(idx)) {
      return(colnames(df)[idx])
    }
  }
  ""
}

to_counts_and_meta <- function(mat, metadata_df, provider, dataset) {
  genes <- rownames(mat)
  if (is.null(genes)) {
    genes <- paste0("gene_", seq_len(nrow(mat)))
  }
  genes <- as_hgnc_like(genes)
  genes[is.na(genes) | !nzchar(genes)] <- paste0("gene_", which(is.na(genes) | !nzchar(genes)))

  counts_df <- data.frame(gene = genes, mat, check.names = FALSE)
  counts_df <- counts_df[!duplicated(counts_df$gene), , drop = FALSE]

  metadata_df$sample_id <- colnames(mat)
  if (!("condition" %in% colnames(metadata_df))) {
    condition_col <- pick_column(metadata_df, c("condition", "group", "status", "sample_type", "subtype", "smtsd", "smts"))
    if (nzchar(condition_col)) {
      metadata_df$condition <- as.character(metadata_df[[condition_col]])
    } else {
      metadata_df$condition <- "unknown"
    }
  }

  if (!("provider" %in% colnames(metadata_df))) {
    metadata_df$provider <- provider
  }
  if (!("dataset_id" %in% colnames(metadata_df))) {
    metadata_df$dataset_id <- dataset
  }

  first <- metadata_df$sample_id
  metadata_df$sample_id <- NULL
  metadata_df <- cbind(data.frame(sample_id = first, stringsAsFactors = FALSE), metadata_df)

  list(counts_df = counts_df, metadata_df = metadata_df)
}

limit_samples <- function(mat, metadata_df, sample_limit = NA_integer_, excluded_ids = character(), target_new_samples = NA_integer_) {
  sample_ids <- as.character(colnames(mat))
  filtered <- filter_sample_ids(
    sample_ids,
    excluded_ids = excluded_ids,
    target_new_samples = target_new_samples,
    sample_limit = sample_limit
  )
  keep_samples <- as.character(filtered$sample_ids)
  if (length(keep_samples) == 0) {
    return(
      list(
        mat = mat[, FALSE, drop = FALSE],
        metadata_df = metadata_df[FALSE, , drop = FALSE],
        excluded_count = as.integer(filtered$excluded_count),
        target_limit = filtered$target_limit
      )
    )
  }
  keep_idx <- match(keep_samples, sample_ids)
  keep_idx <- keep_idx[is.finite(keep_idx)]
  list(
    mat = mat[, keep_idx, drop = FALSE],
    metadata_df = metadata_df[keep_idx, , drop = FALSE],
    excluded_count = as.integer(filtered$excluded_count),
    target_limit = filtered$target_limit
  )
}

exclude_sample_ids <- load_excluded_sample_ids(exclude_sample_ids_path)

run_tcga <- function(project_id) {
  emit_progress("study_download", "provider_start", list(provider_id = "tcga", dataset_id = project_id))
  if (!requireNamespace("TCGAbiolinks", quietly = TRUE)) {
    stop("TCGAbiolinks is not installed.")
  }
  if (!requireNamespace("SummarizedExperiment", quietly = TRUE)) {
    stop("SummarizedExperiment is not installed.")
  }
  cache_inventory <- collect_tcga_cached_file_inventory(project_id)
  emit_progress(
    "study_download",
    "local_cache_scanned",
    list(
      cached_samples_valid = as.integer(cache_inventory$valid_sample_dirs),
      cached_samples_broken = as.integer(cache_inventory$broken_sample_dirs),
      cached_files_valid = as.integer(cache_inventory$valid_files),
      cached_scan_sample_dirs = as.integer(cache_inventory$scanned_sample_dirs)
    )
  )

  query <- TCGAbiolinks::GDCquery(
    project = project_id,
    data.category = "Transcriptome Profiling",
    data.type = "Gene Expression Quantification",
    workflow.type = "STAR - Counts"
  )
  query_results <- query$results
  planned_samples <- NA_integer_
  full_samples <- NA_integer_
  full_unique_samples <- NA_integer_
  full_unique_cases <- NA_integer_
  full_duplicate_file_entries <- NA_integer_
  provider_projects <- as.character(project_id)
  provider_totals_source <- "GDC Files endpoint via TCGAbiolinks::GDCquery (TCGA STAR-Counts filter)"
  is_full_collection_mode <- !is.finite(sample_limit_arg) && !is.finite(target_new_samples_arg)
  desired_rows <- NULL
  download_rows <- NULL
  excluded_count <- 0L
  cached_count <- 0L
  cached_count_full <- 0L
  if (!is.null(query_results) && length(query_results) > 0) {
    first_result <- query_results[[1]]
    if (!is.null(first_result) && is.data.frame(first_result)) {
      full_samples <- suppressWarnings(as.integer(nrow(first_result)))
      full_sample_ids <- as.character(extract_tcga_result_sample_ids(first_result))
      full_case_ids <- as.character(extract_tcga_result_case_ids(first_result))
      full_project_ids <- as.character(extract_tcga_result_project_ids(first_result))
      full_project_ids <- trimws(full_project_ids)
      full_project_ids <- unique(full_project_ids[nzchar(full_project_ids)])
      if (length(full_project_ids) > 0) {
        provider_projects <- full_project_ids
      }
      full_unique_samples <- as.integer(length(canonical_token_set(full_sample_ids)))
      full_unique_cases <- as.integer(length(canonical_token_set(full_case_ids)))
      if (is.finite(full_samples) && full_samples >= 0 && is.finite(full_unique_samples) && full_unique_samples >= 0) {
        full_duplicate_file_entries <- as.integer(max(0, as.integer(full_samples) - as.integer(full_unique_samples)))
      }
      cached_tokens <- as.character(cache_inventory$tokens)
      cached_rows_full <- exclude_tcga_cached_rows(first_result, cached_file_tokens = cached_tokens)
      cached_attr_full <- attr(cached_rows_full, "cached_count")
      if (is.null(cached_attr_full) || !is.finite(as.numeric(cached_attr_full))) {
        cached_count_full <- 0L
      } else {
        cached_count_full <- as.integer(cached_attr_full)
      }
      desired_rows <- filter_tcga_query_rows(
        first_result,
        excluded_ids = exclude_sample_ids,
        target_new_samples = target_new_samples_arg,
        sample_limit = sample_limit_arg
      )
      excluded_attr <- attr(desired_rows, "excluded_count")
      if (is.null(excluded_attr) || !is.finite(as.numeric(excluded_attr))) {
        excluded_count <- 0L
      } else {
        excluded_count <- as.integer(excluded_attr)
      }
      download_rows <- exclude_tcga_cached_rows(desired_rows, cached_file_tokens = cached_tokens)
      cached_attr <- attr(download_rows, "cached_count")
      if (is.null(cached_attr) || !is.finite(as.numeric(cached_attr))) {
        cached_count <- 0L
      } else {
        cached_count <- as.integer(cached_attr)
      }
      planned_samples <- suppressWarnings(as.integer(nrow(desired_rows)))
    } else if (!is.null(first_result)) {
      planned_samples <- suppressWarnings(as.integer(nrow(first_result)))
    }
  }
  if (!is.finite(planned_samples) || planned_samples < 0) {
    planned_samples <- NA_integer_
  }
  progress_percent_for <- function(ready_planned, ready_total = NA_integer_) {
    if (is_full_collection_mode && is.finite(full_samples) && full_samples > 0 && is.finite(ready_total)) {
      return(as.numeric((as.integer(ready_total) / as.integer(full_samples)) * 100.0))
    }
    if (is.finite(planned_samples) && planned_samples > 0 && is.finite(ready_planned)) {
      return(as.numeric((as.integer(ready_planned) / as.integer(planned_samples)) * 100.0))
    }
    NULL
  }
  ready_total_full <- function(downloaded_new_rows) {
    if (!is_full_collection_mode || !is.finite(cached_count_full)) {
      return(NA_integer_)
    }
    ready <- as.integer(cached_count_full + max(0L, as.integer(downloaded_new_rows)))
    if (is.finite(full_samples) && full_samples >= 0) {
      ready <- as.integer(min(as.integer(full_samples), ready))
    }
    ready
  }
  query_ready_meta <- list(
    samples_total = if (is.finite(planned_samples)) as.integer(planned_samples) else NULL,
    samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
    samples_downloaded = as.integer(cached_count),
    samples_downloaded_cached = as.integer(cached_count),
    samples_downloaded_cached_all = as.integer(cached_count_full),
    samples_downloaded_total = if (is_full_collection_mode) as.integer(cached_count_full) else NULL,
    excluded_samples = as.integer(excluded_count),
    cached_samples = as.integer(cached_count),
    cached_samples_all = as.integer(cached_count_full),
    cached_samples_valid = as.integer(cache_inventory$valid_sample_dirs),
    cached_samples_broken = as.integer(cache_inventory$broken_sample_dirs),
    cached_files_valid = as.integer(cache_inventory$valid_files),
    cached_scan_sample_dirs = as.integer(cache_inventory$scanned_sample_dirs),
    provider_totals_source = provider_totals_source,
    provider_projects = as.character(provider_projects),
    provider_project_count = as.integer(length(provider_projects)),
    provider_total_files = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
    provider_total_unique_samples = if (is.finite(full_unique_samples) && full_unique_samples >= 0) {
      as.integer(full_unique_samples)
    } else {
      NULL
    },
    provider_total_unique_cases = if (is.finite(full_unique_cases) && full_unique_cases >= 0) {
      as.integer(full_unique_cases)
    } else {
      NULL
    },
    provider_duplicate_file_entries = if (is.finite(full_duplicate_file_entries) && full_duplicate_file_entries >= 0) {
      as.integer(full_duplicate_file_entries)
    } else {
      NULL
    }
  )
  emit_progress(
    "study_download",
    "query_ready",
    query_ready_meta
  )
  emit_progress(
    "study_download",
    "remote_download_started",
    query_ready_meta
  )
  if (is.finite(planned_samples) && planned_samples <= 0) {
    counts_df <- data.frame(gene = character(), check.names = FALSE, stringsAsFactors = FALSE)
    metadata_df <- data.frame(
      sample_id = character(),
      condition = character(),
      provider = character(),
      dataset_id = character(),
      stringsAsFactors = FALSE
    )
    summary <- list(genes = 0L, samples = 0L, metadata_columns = ncol(metadata_df))
    meta <- list(
      provider = "tcga",
      dataset_id = project_id,
      tissue = tissue,
      connector = "TCGAbiolinks",
      cached_samples = as.integer(cached_count),
      cached_samples_all = as.integer(cached_count_full),
      cached_samples_valid = as.integer(cache_inventory$valid_sample_dirs),
      cached_samples_broken = as.integer(cache_inventory$broken_sample_dirs),
      cached_files_valid = as.integer(cache_inventory$valid_files),
      cached_scan_sample_dirs = as.integer(cache_inventory$scanned_sample_dirs),
      excluded_samples = as.integer(excluded_count),
      provider_totals_source = provider_totals_source,
      provider_projects = as.character(provider_projects),
      provider_total_files = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
      provider_total_unique_samples = if (is.finite(full_unique_samples) && full_unique_samples >= 0) {
        as.integer(full_unique_samples)
      } else {
        NULL
      },
      provider_total_unique_cases = if (is.finite(full_unique_cases) && full_unique_cases >= 0) {
        as.integer(full_unique_cases)
      } else {
        NULL
      },
      provider_duplicate_file_entries = if (is.finite(full_duplicate_file_entries) && full_duplicate_file_entries >= 0) {
        as.integer(full_duplicate_file_entries)
      } else {
        NULL
      }
    )
    emit_progress(
      "study_download",
      "samples_limited",
      list(
        samples_downloaded = 0L,
        samples_downloaded_total = if (is_full_collection_mode) as.integer(cached_count_full) else NULL,
        samples_total = 0L,
        samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
        excluded_samples = as.integer(excluded_count)
      )
    )
    emit_progress("study_download", "writing_outputs", list(samples_downloaded = 0L, samples_total = 0L))
    write_output(counts_df, metadata_df, summary, meta)
    emit_progress("study_download", "outputs_written", list(samples_downloaded = 0L, samples_total = 0L))
    return(invisible(NULL))
  }

  if (is.finite(planned_samples) && planned_samples > 0 && !is.null(desired_rows) && nrow(desired_rows) == planned_samples) {
    query_prepare <- query
    query_prepare$results[[1]] <- desired_rows
    if (is.null(download_rows) || !is.data.frame(download_rows)) {
      download_rows <- desired_rows[FALSE, , drop = FALSE]
      attr(download_rows, "cached_count") <- 0L
    }
    total_to_download <- as.integer(nrow(download_rows))
    chunk_size <- 5L
    downloaded <- 0L
    if (total_to_download > 0) {
      starts <- seq(1L, total_to_download, by = chunk_size)
      for (idx in starts) {
        end_idx <- min(total_to_download, idx + chunk_size - 1L)
        pre_ready <- as.integer(cached_count + downloaded)
        pre_ready_total <- ready_total_full(downloaded)
        emit_progress(
          "study_download",
          "remote_progress",
          list(
            samples_downloaded = pre_ready,
            samples_downloaded_new = as.integer(downloaded),
            samples_downloaded_cached = as.integer(cached_count),
            samples_downloaded_cached_all = as.integer(cached_count_full),
            samples_downloaded_total = if (is_full_collection_mode && is.finite(pre_ready_total)) {
              as.integer(pre_ready_total)
            } else {
              NULL
            },
            samples_total = as.integer(planned_samples),
            samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
            progress_percent = progress_percent_for(pre_ready, pre_ready_total),
            message = sprintf("Downloading provider files %s-%s of %s.", idx, end_idx, total_to_download)
          )
        )
        query_chunk <- query
        query_chunk$results[[1]] <- download_rows[idx:end_idx, , drop = FALSE]
        TCGAbiolinks::GDCdownload(query_chunk, method = "api")
        downloaded <- end_idx
        total_ready <- as.integer(cached_count + downloaded)
        total_ready_all <- ready_total_full(downloaded)
        emit_progress(
          "study_download",
          "remote_progress",
          list(
            samples_downloaded = total_ready,
            samples_downloaded_new = as.integer(downloaded),
            samples_downloaded_cached = as.integer(cached_count),
            samples_downloaded_cached_all = as.integer(cached_count_full),
            samples_downloaded_total = if (is_full_collection_mode && is.finite(total_ready_all)) {
              as.integer(total_ready_all)
            } else {
              NULL
            },
            samples_total = as.integer(planned_samples),
            samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
            progress_percent = progress_percent_for(total_ready, total_ready_all)
          )
        )
      }
    } else {
      ready_total <- ready_total_full(0L)
      emit_progress(
        "study_download",
        "remote_progress",
        list(
          samples_downloaded = as.integer(cached_count),
          samples_downloaded_new = 0L,
          samples_downloaded_cached = as.integer(cached_count),
          samples_downloaded_cached_all = as.integer(cached_count_full),
          samples_downloaded_total = if (is_full_collection_mode && is.finite(ready_total)) as.integer(ready_total) else NULL,
          samples_total = as.integer(planned_samples),
          samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
          progress_percent = progress_percent_for(as.integer(cached_count), ready_total)
        )
      )
    }
    completed_total <- ready_total_full(max(0L, as.integer(planned_samples) - as.integer(cached_count)))
    if (!is.finite(completed_total) && is.finite(full_samples) && full_samples >= 0) {
      completed_total <- as.integer(full_samples)
    }
    emit_progress(
      "study_download",
      "remote_download_completed",
      list(
        samples_downloaded = as.integer(planned_samples),
        samples_total = as.integer(planned_samples),
        samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
        samples_downloaded_total = if (is_full_collection_mode && is.finite(completed_total)) {
          as.integer(completed_total)
        } else {
          NULL
        },
        progress_percent = 100.0
      )
    )
    se <- TCGAbiolinks::GDCprepare(query_prepare, summarizedExperiment = TRUE)
  } else if (is.finite(planned_samples) && planned_samples == 0) {
    emit_progress(
      "study_download",
      "remote_progress",
      list(samples_downloaded = 0L, samples_total = 0L, progress_percent = 100.0)
    )
    se <- TCGAbiolinks::GDCprepare(query, summarizedExperiment = TRUE)
  } else {
    TCGAbiolinks::GDCdownload(query, method = "api")
    emit_progress(
      "study_download",
      "remote_download_completed",
      list(
        samples_total = if (is.finite(planned_samples)) as.integer(planned_samples) else NULL,
        samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
        samples_downloaded_total = if (is_full_collection_mode && is.finite(cached_count_full)) {
          as.integer(cached_count_full)
        } else {
          NULL
        }
      )
    )
    se <- TCGAbiolinks::GDCprepare(query, summarizedExperiment = TRUE)
  }

  mat <- SummarizedExperiment::assay(se)
  if (is.null(mat) || nrow(mat) == 0 || ncol(mat) == 0) {
    stop("TCGA returned an empty expression matrix.")
  }

  mat <- normalize_counts_matrix(mat, make_integer = TRUE)
  matrix_downloaded_new <- max(0L, as.integer(ncol(mat)) - as.integer(cached_count))
  matrix_total_ready <- ready_total_full(matrix_downloaded_new)
  emit_progress(
    "study_download",
      "matrix_loaded",
      list(
        samples_downloaded = as.integer(ncol(mat)),
        samples_downloaded_total = if (is_full_collection_mode && is.finite(matrix_total_ready)) {
          as.integer(matrix_total_ready)
        } else {
          NULL
        },
        samples_total = if (is.finite(planned_samples)) as.integer(planned_samples) else as.integer(ncol(mat)),
        samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL
      )
  )
  metadata_df <- as.data.frame(SummarizedExperiment::colData(se), stringsAsFactors = FALSE)
  limited <- limit_samples(
    mat,
    metadata_df,
    sample_limit = sample_limit_arg,
    excluded_ids = exclude_sample_ids,
    target_new_samples = target_new_samples_arg
  )
  mat <- limited$mat
  metadata_df <- limited$metadata_df
  limited_downloaded_new <- max(0L, as.integer(ncol(mat)) - as.integer(cached_count))
  limited_total_ready <- ready_total_full(limited_downloaded_new)
  emit_progress(
    "study_download",
    "samples_limited",
    list(
      samples_downloaded = as.integer(ncol(mat)),
      samples_downloaded_total = if (is_full_collection_mode && is.finite(limited_total_ready)) {
        as.integer(limited_total_ready)
      } else {
        NULL
      },
      samples_total = if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
        as.integer(target_new_samples_arg)
      } else if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
        as.integer(sample_limit_arg)
      } else {
        as.integer(ncol(mat))
      },
      samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
      excluded_samples = as.integer(limited$excluded_count),
      cached_samples = as.integer(cached_count)
    )
  )

  merged <- to_counts_and_meta(mat, metadata_df, "tcga", project_id)
  counts_df <- merged$counts_df
  metadata_df <- merged$metadata_df

  if ("shortLetterCode" %in% colnames(metadata_df)) {
    code <- as.character(metadata_df$shortLetterCode)
    code <- ifelse(code == "TP", "tumor", code)
    code <- ifelse(code == "NT", "normal", code)
    metadata_df$condition <- code
  } else if ("sample_type" %in% colnames(metadata_df)) {
    metadata_df$condition <- as.character(metadata_df$sample_type)
  }

  summary <- list(
    genes = nrow(counts_df),
    samples = ncol(counts_df) - 1,
    metadata_columns = ncol(metadata_df)
  )
  meta <- list(
    provider = "tcga",
    dataset_id = project_id,
    tissue = tissue,
    connector = "TCGAbiolinks"
  )
  if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
    meta$sample_limit_requested <- as.integer(sample_limit_arg)
  }
  if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
    meta$target_new_samples <- as.integer(target_new_samples_arg)
  }
  meta$exclude_sample_count <- as.integer(length(exclude_sample_ids))
  meta$cached_samples <- as.integer(cached_count)
  meta$cached_samples_all <- as.integer(cached_count_full)
  meta$cached_samples_valid <- as.integer(cache_inventory$valid_sample_dirs)
  meta$cached_samples_broken <- as.integer(cache_inventory$broken_sample_dirs)
  meta$cached_files_valid <- as.integer(cache_inventory$valid_files)
  meta$cached_scan_sample_dirs <- as.integer(cache_inventory$scanned_sample_dirs)
  meta$provider_totals_source <- provider_totals_source
  meta$provider_projects <- as.character(provider_projects)
  if (is.finite(full_samples) && full_samples >= 0) {
    meta$provider_total_files <- as.integer(full_samples)
  }
  if (is.finite(full_unique_samples) && full_unique_samples >= 0) {
    meta$provider_total_unique_samples <- as.integer(full_unique_samples)
  }
  if (is.finite(full_unique_cases) && full_unique_cases >= 0) {
    meta$provider_total_unique_cases <- as.integer(full_unique_cases)
  }
  if (is.finite(full_duplicate_file_entries) && full_duplicate_file_entries >= 0) {
    meta$provider_duplicate_file_entries <- as.integer(full_duplicate_file_entries)
  }
  emit_progress(
    "study_download",
    "writing_outputs",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
      samples_downloaded_total = if (is_full_collection_mode && is.finite(full_samples) && full_samples >= 0) {
        as.integer(full_samples)
      } else {
        NULL
      }
    )
  )
  write_output(counts_df, metadata_df, summary, meta)
  emit_progress(
    "study_download",
    "outputs_written",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = if (is.finite(full_samples) && full_samples >= 0) as.integer(full_samples) else NULL,
      samples_downloaded_total = if (is_full_collection_mode && is.finite(full_samples) && full_samples >= 0) {
        as.integer(full_samples)
      } else {
        NULL
      }
    )
  )
}

resolve_gtex_tissue <- function(dataset_id_value, tissue_value) {
  if (nzchar(tissue_value)) {
    return(tolower(tissue_value))
  }
  dataset_u <- toupper(dataset_id_value)
  if (grepl("WHOLE_BLOOD", dataset_u, fixed = TRUE)) {
    return("whole blood")
  }
  if (grepl("LIVER", dataset_u, fixed = TRUE)) {
    return("liver")
  }
  ""
}

extract_gtex_release <- function(dataset_id_value) {
  token <- toupper(as.character(dataset_id_value))
  match <- regmatches(token, regexpr("V[0-9]+", token))
  if (!length(match) || !nzchar(match)) {
    return("")
  }
  tolower(match)
}

run_gtex <- function(dataset_id_value) {
  emit_progress("study_download", "provider_start", list(provider_id = "gtex", dataset_id = dataset_id_value))
  if (!requireNamespace("recount3", quietly = TRUE)) {
    stop("recount3 is not installed.")
  }
  if (!requireNamespace("SummarizedExperiment", quietly = TRUE)) {
    stop("SummarizedExperiment is not installed.")
  }

  projects <- recount3::available_projects()
  if (is.null(projects) || nrow(projects) == 0) {
    stop("recount3 available_projects returned no rows.")
  }

  combined <- apply(projects, 1, function(row) paste(tolower(as.character(row)), collapse = " "))
  gtex_mask <- grepl("gtex", combined, fixed = TRUE) | grepl("srp012682", combined, fixed = TRUE)
  if (!any(gtex_mask)) {
    stop("Could not find GTEx project in recount3::available_projects().")
  }

  target_release <- extract_gtex_release(dataset_id_value)
  if (nzchar(target_release)) {
    release_mask <- grepl(target_release, combined, fixed = TRUE)
    if (any(gtex_mask & release_mask)) {
      gtex_mask <- gtex_mask & release_mask
    }
  }
  emit_progress("study_download", "query_ready", list())

  project_info <- projects[which(gtex_mask)[1], , drop = FALSE]
  emit_progress("study_download", "remote_download_started", list())
  rse <- recount3::create_rse(project_info)

  assay_names <- SummarizedExperiment::assayNames(rse)
  assay_name <- if ("raw_counts" %in% assay_names) "raw_counts" else assay_names[[1]]
  mat <- SummarizedExperiment::assay(rse, assay_name)
  if (is.null(mat) || nrow(mat) == 0 || ncol(mat) == 0) {
    stop("GTEx recount3 connector returned an empty matrix.")
  }
  mat <- normalize_counts_matrix(mat, make_integer = TRUE)
  full_samples <- as.integer(ncol(mat))
  emit_progress(
    "study_download",
    "matrix_loaded",
    list(
      samples_downloaded = as.integer(ncol(mat)),
      samples_total = as.integer(ncol(mat)),
      samples_total_all = as.integer(full_samples)
    )
  )

  metadata_df <- as.data.frame(SummarizedExperiment::colData(rse), stringsAsFactors = FALSE)
  metadata_df$sample_id <- colnames(mat)

  target_tissue <- resolve_gtex_tissue(dataset_id_value, tissue)
  tissue_col <- pick_column(metadata_df, c("smtsd", "smts", "tissue", "primary_site", "site", "source_name"))
  if (nzchar(target_tissue) && nzchar(tissue_col)) {
    keep <- grepl(target_tissue, tolower(as.character(metadata_df[[tissue_col]])), fixed = TRUE)
    if (sum(keep, na.rm = TRUE) >= 12) {
      metadata_df <- metadata_df[keep, , drop = FALSE]
      keep_samples <- as.character(metadata_df$sample_id)
      mat <- mat[, keep_samples, drop = FALSE]
    }
  }

  limited <- limit_samples(
    mat,
    metadata_df,
    sample_limit = sample_limit_arg,
    excluded_ids = exclude_sample_ids,
    target_new_samples = target_new_samples_arg
  )
  mat <- limited$mat
  metadata_df <- limited$metadata_df
  emit_progress(
    "study_download",
    "samples_limited",
    list(
      samples_downloaded = as.integer(ncol(mat)),
      samples_total = if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
        as.integer(target_new_samples_arg)
      } else if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
        as.integer(sample_limit_arg)
      } else {
        as.integer(ncol(mat))
      },
      samples_total_all = as.integer(full_samples),
      excluded_samples = as.integer(limited$excluded_count)
    )
  )

  merged <- to_counts_and_meta(mat, metadata_df, "gtex", dataset_id_value)
  counts_df <- merged$counts_df
  metadata_df <- merged$metadata_df
  if (nzchar(tissue_col) && tissue_col %in% colnames(metadata_df)) {
    metadata_df$condition <- as.character(metadata_df[[tissue_col]])
  }

  summary <- list(
    genes = nrow(counts_df),
    samples = ncol(counts_df) - 1,
    metadata_columns = ncol(metadata_df)
  )
  meta <- list(
    provider = "gtex",
    dataset_id = dataset_id_value,
    tissue = tissue,
    connector = "recount3"
  )
  if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
    meta$sample_limit_requested <- as.integer(sample_limit_arg)
  }
  if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
    meta$target_new_samples <- as.integer(target_new_samples_arg)
  }
  meta$exclude_sample_count <- as.integer(length(exclude_sample_ids))
  emit_progress(
    "study_download",
    "writing_outputs",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = as.integer(full_samples)
    )
  )
  write_output(counts_df, metadata_df, summary, meta)
  emit_progress(
    "study_download",
    "outputs_written",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = as.integer(full_samples)
    )
  )
}

run_metabric <- function(dataset_id_value) {
  emit_progress("study_download", "provider_start", list(provider_id = "metabric", dataset_id = dataset_id_value))
  if (!requireNamespace("cBioPortalData", quietly = TRUE)) {
    stop("cBioPortalData is not installed.")
  }
  if (!requireNamespace("MultiAssayExperiment", quietly = TRUE)) {
    stop("MultiAssayExperiment is not installed.")
  }
  if (!requireNamespace("SummarizedExperiment", quietly = TRUE)) {
    stop("SummarizedExperiment is not installed.")
  }

  study_id <- "brca_metabric"
  if (nzchar(dataset_id_value) && !grepl("METABRIC", toupper(dataset_id_value), fixed = TRUE)) {
    study_id <- dataset_id_value
  }
  emit_progress("study_download", "query_ready", list())

  emit_progress("study_download", "remote_download_started", list())
  mae <- cBioPortalData::cBioDataPack(study_id, ask = FALSE)
  experiments <- MultiAssayExperiment::experiments(mae)
  if (length(experiments) == 0) {
    stop("METABRIC connector returned no experiments.")
  }

  exp_names <- names(experiments)
  pick_idx <- which(grepl("rna|mrna|expression", tolower(exp_names)))[1]
  if (is.na(pick_idx)) {
    pick_idx <- 1
  }
  se <- experiments[[pick_idx]]
  if (!methods::is(se, "SummarizedExperiment")) {
    stop("Selected METABRIC experiment is not a SummarizedExperiment.")
  }

  mat <- SummarizedExperiment::assay(se)
  if (is.null(mat) || nrow(mat) == 0 || ncol(mat) == 0) {
    stop("METABRIC connector returned an empty expression matrix.")
  }
  mat <- normalize_counts_matrix(mat, make_integer = TRUE)
  full_samples <- as.integer(ncol(mat))
  emit_progress(
    "study_download",
    "matrix_loaded",
    list(
      samples_downloaded = as.integer(ncol(mat)),
      samples_total = as.integer(ncol(mat)),
      samples_total_all = as.integer(full_samples)
    )
  )

  metadata_df <- as.data.frame(SummarizedExperiment::colData(se), stringsAsFactors = FALSE)
  if (nrow(metadata_df) != ncol(mat)) {
    metadata_df <- data.frame(sample_id = colnames(mat), stringsAsFactors = FALSE)
  }

  limited <- limit_samples(
    mat,
    metadata_df,
    sample_limit = sample_limit_arg,
    excluded_ids = exclude_sample_ids,
    target_new_samples = target_new_samples_arg
  )
  mat <- limited$mat
  metadata_df <- limited$metadata_df
  emit_progress(
    "study_download",
    "samples_limited",
    list(
      samples_downloaded = as.integer(ncol(mat)),
      samples_total = if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
        as.integer(target_new_samples_arg)
      } else if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
        as.integer(sample_limit_arg)
      } else {
        as.integer(ncol(mat))
      },
      samples_total_all = as.integer(full_samples),
      excluded_samples = as.integer(limited$excluded_count)
    )
  )

  merged <- to_counts_and_meta(mat, metadata_df, "metabric", dataset_id_value)
  counts_df <- merged$counts_df
  metadata_df <- merged$metadata_df

  subtype_col <- pick_column(metadata_df, c("pam50_subtype", "subtype", "er_status", "her2_status", "sample_type"))
  if (nzchar(subtype_col)) {
    metadata_df$condition <- as.character(metadata_df[[subtype_col]])
  } else {
    metadata_df$condition <- "metabric"
  }

  summary <- list(
    genes = nrow(counts_df),
    samples = ncol(counts_df) - 1,
    metadata_columns = ncol(metadata_df)
  )
  meta <- list(
    provider = "metabric",
    dataset_id = dataset_id_value,
    tissue = tissue,
    connector = "cBioPortalData"
  )
  if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
    meta$sample_limit_requested <- as.integer(sample_limit_arg)
  }
  if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
    meta$target_new_samples <- as.integer(target_new_samples_arg)
  }
  meta$exclude_sample_count <- as.integer(length(exclude_sample_ids))
  emit_progress(
    "study_download",
    "writing_outputs",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = as.integer(full_samples)
    )
  )
  write_output(counts_df, metadata_df, summary, meta)
  emit_progress(
    "study_download",
    "outputs_written",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = as.integer(full_samples)
    )
  )
}

evomed_read_csv <- function(base_url, endpoint, token = "") {
  if (!requireNamespace("httr", quietly = TRUE)) {
    stop("httr is required for EvoMedGenomics connector.")
  }

  url <- sprintf("%s%s", gsub("/$", "", base_url), endpoint)
  headers <- c(Accept = "text/csv,application/json")
  if (nzchar(token)) {
    headers <- c(headers, Authorization = sprintf("Bearer %s", token))
  }

  resp <- httr::GET(url, httr::add_headers(.headers = headers))
  status <- httr::status_code(resp)
  if (status >= 300) {
    body <- httr::content(resp, as = "text", encoding = "UTF-8")
    stop(sprintf("EvoMed API request failed (%s): %s", status, body))
  }

  content_type <- tolower(as.character(resp$headers[["content-type"]]))
  body <- httr::content(resp, as = "text", encoding = "UTF-8")

  if (grepl("application/json", content_type, fixed = TRUE)) {
    payload <- jsonlite::fromJSON(body, simplifyVector = FALSE)
    if (is.list(payload) && !is.null(payload$url)) {
      next_resp <- httr::GET(as.character(payload$url), httr::add_headers(.headers = headers))
      if (httr::status_code(next_resp) >= 300) {
        stop("EvoMed returned a URL for CSV but it could not be downloaded.")
      }
      csv_text <- httr::content(next_resp, as = "text", encoding = "UTF-8")
      return(read.csv(text = csv_text, check.names = FALSE, stringsAsFactors = FALSE))
    }
    if (is.list(payload) && !is.null(payload$data)) {
      return(as.data.frame(payload$data, stringsAsFactors = FALSE))
    }
    stop("EvoMed JSON response did not include 'url' or 'data'.")
  }

  read.csv(text = body, check.names = FALSE, stringsAsFactors = FALSE)
}

run_evomed <- function(dataset_id_value) {
  emit_progress("study_download", "provider_start", list(provider_id = "evomedgenomics", dataset_id = dataset_id_value))
  base_url <- Sys.getenv("EVMG_API_BASE_URL", unset = "")
  if (!nzchar(base_url)) {
    stop("EVMG_API_BASE_URL is not configured for EvoMedGenomics connector.")
  }
  token <- Sys.getenv("EVMG_API_TOKEN", unset = "")

  emit_progress("study_download", "remote_download_started", list())
  counts_df <- evomed_read_csv(base_url, sprintf("/studies/%s/counts?format=csv", dataset_id_value), token)
  metadata_df <- evomed_read_csv(base_url, sprintf("/studies/%s/metadata?format=csv", dataset_id_value), token)

  gene_col <- pick_column(counts_df, c("gene", "gene_name", "symbol", "hgnc_symbol", "external_gene_name", "ensembl_gene_id"))
  if (!nzchar(gene_col)) {
    gene_col <- colnames(counts_df)[1]
  }
  if (gene_col != "gene") {
    colnames(counts_df)[colnames(counts_df) == gene_col] <- "gene"
  }

  sample_col <- pick_column(metadata_df, c("sample_id", "sample", "sample_name", "subject_id", "id"))
  if (!nzchar(sample_col)) {
    sample_col <- colnames(metadata_df)[1]
  }
  if (sample_col != "sample_id") {
    colnames(metadata_df)[colnames(metadata_df) == sample_col] <- "sample_id"
  }

  if (nzchar(tissue)) {
    tissue_col <- pick_column(metadata_df, c("tissue", "site", "primary_site", "source"))
    if (nzchar(tissue_col)) {
      keep <- grepl(tolower(tissue), tolower(as.character(metadata_df[[tissue_col]])), fixed = TRUE)
      if (sum(keep, na.rm = TRUE) >= 8) {
        metadata_df <- metadata_df[keep, , drop = FALSE]
      }
    }
  }

  shared <- intersect(colnames(counts_df)[colnames(counts_df) != "gene"], as.character(metadata_df$sample_id))
  if (length(shared) < 1) {
    stop("EvoMed connector could not align any shared samples between counts and metadata.")
  }
  full_shared_total <- as.integer(length(shared))
  shared_filtered <- filter_sample_ids(
    shared,
    excluded_ids = exclude_sample_ids,
    target_new_samples = target_new_samples_arg,
    sample_limit = sample_limit_arg
  )
  shared <- as.character(shared_filtered$sample_ids)
  emit_progress(
    "study_download",
    "samples_limited",
    list(
      samples_downloaded = as.integer(length(shared)),
      samples_total = if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
        as.integer(target_new_samples_arg)
      } else if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
        as.integer(sample_limit_arg)
      } else {
        as.integer(length(shared))
      },
      samples_total_all = as.integer(full_shared_total),
      excluded_samples = as.integer(shared_filtered$excluded_count)
    )
  )

  counts_df <- counts_df[, c("gene", shared), drop = FALSE]
  metadata_df <- metadata_df[metadata_df$sample_id %in% shared, , drop = FALSE]
  metadata_df <- metadata_df[match(shared, metadata_df$sample_id), , drop = FALSE]

  if (!("condition" %in% colnames(metadata_df))) {
    condition_col <- pick_column(metadata_df, c("condition", "group", "status", "phenotype"))
    if (nzchar(condition_col)) {
      metadata_df$condition <- as.character(metadata_df[[condition_col]])
    } else {
      metadata_df$condition <- "evomed"
    }
  }

  metadata_df$provider <- "evomedgenomics"
  metadata_df$dataset_id <- dataset_id_value

  summary <- list(
    genes = nrow(counts_df),
    samples = ncol(counts_df) - 1,
    metadata_columns = ncol(metadata_df)
  )
  meta <- list(
    provider = "evomedgenomics",
    dataset_id = dataset_id_value,
    tissue = tissue,
    connector = "api"
  )
  if (is.finite(sample_limit_arg) && sample_limit_arg >= 2) {
    meta$sample_limit_requested <- as.integer(sample_limit_arg)
  }
  if (is.finite(target_new_samples_arg) && target_new_samples_arg >= 1) {
    meta$target_new_samples <- as.integer(target_new_samples_arg)
  }
  meta$exclude_sample_count <- as.integer(length(exclude_sample_ids))
  emit_progress(
    "study_download",
    "writing_outputs",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = as.integer(full_shared_total)
    )
  )
  write_output(counts_df, metadata_df, summary, meta)
  emit_progress(
    "study_download",
    "outputs_written",
    list(
      samples_downloaded = as.integer(ncol(counts_df) - 1),
      samples_total = as.integer(ncol(counts_df) - 1),
      samples_total_all = as.integer(full_shared_total)
    )
  )
}

if (provider_id == "tcga") {
  run_tcga(dataset_id)
} else if (provider_id == "gtex") {
  run_gtex(dataset_id)
} else if (provider_id == "metabric") {
  run_metabric(dataset_id)
} else if (provider_id == "evomedgenomics") {
  run_evomed(dataset_id)
} else {
  stop(sprintf("Unsupported provider: %s", provider_id))
}
