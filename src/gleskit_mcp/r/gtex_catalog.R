#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 1) {
  stop("Usage: gtex_catalog.R <output_json>")
}

output_path <- args[[1]]

suppressPackageStartupMessages({
  library(jsonlite)
  library(httr)
})

api_base <- Sys.getenv("GTEX_API_BASE_URL", unset = "https://gtexportal.org/api/v2")
gtex_gcs_bucket <- Sys.getenv("GTEX_GCS_BUCKET", unset = "adult-gtex")
gtex_gcs_api <- sprintf("https://storage.googleapis.com/storage/v1/b/%s/o", gtex_gcs_bucket)

normalize_release <- function(dataset_id) {
  token <- tolower(gsub("^gtex_", "", as.character(dataset_id)))
  token <- gsub("[^a-z0-9]+", "", token)
  if (!nzchar(token)) {
    return("v8")
  }
  if (!startsWith(token, "v")) {
    token <- paste0("v", token)
  }
  token
}

normalize_release_token <- function(value) {
  token <- tolower(trimws(as.character(value)))
  token <- gsub("[^a-z0-9]+", "", token)
  if (!nzchar(token)) {
    return("")
  }
  if (grepl("^v[0-9]+$", token)) {
    return(token)
  }
  if (grepl("^[0-9]+$", token)) {
    return(paste0("v", token))
  }
  if (grepl("^gtexv[0-9]+$", token)) {
    return(sub("^gtex", "", token))
  }
  if (grepl("^gtex_[0-9]+$", token)) {
    return(paste0("v", sub("^gtex_", "", token)))
  }
  ""
}

slug_tissue <- function(tissue) {
  token <- gsub("[^A-Za-z0-9]+", "_", toupper(as.character(tissue)))
  token <- gsub("^_+|_+$", "", token)
  if (!nzchar(token)) {
    token <- "UNSPECIFIED"
  }
  token
}

read_json_data <- function(path, query = list()) {
  url <- paste0(gsub("/+$", "", api_base), path)
  resp <- httr::GET(url, query = query, httr::add_headers(Accept = "application/json"))
  status <- httr::status_code(resp)
  if (status >= 300) {
    body <- httr::content(resp, as = "text", encoding = "UTF-8")
    stop(sprintf("GTEx API request failed (%s) for %s: %s", status, path, body))
  }
  raw <- httr::content(resp, as = "text", encoding = "UTF-8")
  payload <- jsonlite::fromJSON(raw, simplifyVector = TRUE, flatten = TRUE)
  if (is.list(payload) && !is.null(payload$data)) {
    return(payload$data)
  }
  payload
}

read_json_url <- function(url, query = list()) {
  resp <- httr::GET(url, query = query, httr::add_headers(Accept = "application/json"))
  status <- httr::status_code(resp)
  if (status >= 300) {
    body <- httr::content(resp, as = "text", encoding = "UTF-8")
    stop(sprintf("JSON request failed (%s) for %s: %s", status, url, body))
  }
  raw <- httr::content(resp, as = "text", encoding = "UTF-8")
  payload <- jsonlite::fromJSON(raw, simplifyVector = TRUE, flatten = TRUE)
  if (is.list(payload) && !is.null(payload$data)) {
    return(payload$data)
  }
  payload
}

as_data_frame <- function(value) {
  if (is.null(value)) {
    return(data.frame(stringsAsFactors = FALSE))
  }
  if (is.data.frame(value)) {
    return(value)
  }
  if (is.list(value) && length(value) > 0) {
    if (all(vapply(value, function(item) is.list(item) || is.null(item), logical(1)))) {
      keys <- unique(unlist(lapply(value, names), use.names = FALSE))
      if (length(keys) == 0) {
        return(data.frame(stringsAsFactors = FALSE))
      }
      rows <- lapply(value, function(item) {
        row <- setNames(vector("list", length(keys)), keys)
        for (key in keys) {
          cell <- item[[key]]
          if (is.null(cell) || length(cell) == 0) {
            row[[key]] <- NA_character_
          } else if (is.atomic(cell) && length(cell) == 1) {
            row[[key]] <- as.character(cell[[1]])
          } else if (is.atomic(cell) && length(cell) > 1) {
            row[[key]] <- paste(as.character(cell), collapse = ";")
          } else {
            row[[key]] <- jsonlite::toJSON(cell, auto_unbox = TRUE)
          }
        }
        as.data.frame(row, stringsAsFactors = FALSE)
      })
      return(jsonlite::rbind_pages(rows))
    }
    return(tryCatch(
      as.data.frame(value, stringsAsFactors = FALSE),
      error = function(err) data.frame(stringsAsFactors = FALSE)
    ))
  }
  data.frame(stringsAsFactors = FALSE)
}

collect_dataset_ids <- function() {
  ids <- character(0)
  endpoints <- list(
    list(path = "/reference/dataset", query = list(itemsPerPage = 2000, page = 0)),
    list(path = "/reference/dataset", query = list()),
    list(path = "/dataset", query = list(itemsPerPage = 2000, page = 0)),
    list(path = "/dataset", query = list())
  )

  for (endpoint in endpoints) {
    data <- tryCatch(
      read_json_data(endpoint$path, query = endpoint$query),
      error = function(err) {
        message("GTEx dataset listing endpoint unavailable: ", conditionMessage(err))
        NULL
      }
    )
    frame <- as_data_frame(data)
    if (nrow(frame) == 0) {
      next
    }
    names <- colnames(frame)
    lowered <- tolower(names)
    idx <- match("datasetid", lowered)
    if (is.na(idx)) {
      idx <- match("dataset_id", lowered)
    }
    if (is.na(idx)) {
      next
    }
    candidate <- unique(tolower(trimws(as.character(frame[[names[[idx]]]]))))
    candidate <- candidate[nzchar(candidate)]
    if (length(candidate) > 0) {
      ids <- unique(c(ids, candidate))
    }
  }

  ids <- ids[grepl("^gtex_", ids)]
  ids <- ids[!grepl("snrna|single", ids)]
  ids <- ids[!grepl("pilot", ids)]
  if (length(ids) == 0) {
    ids <- c("gtex_v8")
  }
  unique(ids)
}

collect_releases_from_dataset_ids <- function(dataset_ids) {
  if (length(dataset_ids) == 0) {
    return(character(0))
  }
  out <- unique(vapply(dataset_ids, normalize_release, character(1)))
  out <- out[nzchar(out)]
  unique(out)
}

collect_releases_from_gcs <- function() {
  data <- tryCatch(
    read_json_url(
      gtex_gcs_api,
      query = list(prefix = "annotations/", delimiter = "/", fields = "prefixes")
    ),
    error = function(err) {
      message("GTEx GCS release listing unavailable: ", conditionMessage(err))
      NULL
    }
  )
  frame <- as_data_frame(data)
  prefixes <- character(0)
  if (is.list(data) && !is.null(data$prefixes)) {
    prefixes <- as.character(unlist(data$prefixes, use.names = FALSE))
  } else if (nrow(frame) > 0) {
    cols <- colnames(frame)
    idx <- match("prefixes", tolower(cols))
    if (!is.na(idx)) {
      prefixes <- as.character(unlist(frame[[cols[[idx]]]], use.names = FALSE))
    }
  }
  if (length(prefixes) == 0) {
    return(character(0))
  }
  rel <- sub("^annotations/([^/]+)/.*$", "\\1", prefixes)
  rel <- unique(vapply(rel, normalize_release_token, character(1)))
  rel[nzchar(rel)]
}

release_rank <- function(value) {
  token <- tolower(trimws(as.character(value)))
  if (!nzchar(token)) {
    return(-1L)
  }
  numeric <- suppressWarnings(as.integer(gsub("[^0-9]", "", token)))
  if (!is.finite(numeric)) {
    return(-1L)
  }
  as.integer(numeric)
}

extract_tissue_rows <- function(dataset_id) {
  endpoints <- c("/reference/tissueSiteDetail", "/dataset/tissueSiteDetail")
  for (path in endpoints) {
    data <- tryCatch(
      read_json_data(path, query = list(datasetId = dataset_id, itemsPerPage = 1000, page = 0)),
      error = function(err) NULL
    )
    frame <- as_data_frame(data)
    if (nrow(frame) > 0) {
      return(frame)
    }
  }
  data.frame(stringsAsFactors = FALSE)
}

pick_column <- function(frame, candidates) {
  names <- colnames(frame)
  lowered <- tolower(names)
  for (candidate in candidates) {
    idx <- match(tolower(candidate), lowered)
    if (!is.na(idx)) {
      return(names[[idx]])
    }
  }
  ""
}

build_catalog_rows <- function(dataset_id) {
  frame <- extract_tissue_rows(dataset_id)
  if (nrow(frame) == 0) {
    return(list())
  }

  tissue_col <- pick_column(frame, c("tissueSiteDetail", "smtsd", "tissueSite", "tissue"))
  count_col <- pick_column(
    frame,
    c(
      "rnaSeqAndGenotypeSampleCount",
      "rna_seq_and_genotype_sample_count",
      "sampleCount",
      "sample_count",
      "rnaSeqSampleCount"
    )
  )

  if (!nzchar(tissue_col) || !nzchar(count_col)) {
    return(list())
  }

  release <- normalize_release(dataset_id)
  rows <- list()
  for (idx in seq_len(nrow(frame))) {
    tissue <- trimws(as.character(frame[[tissue_col]][[idx]]))
    if (!nzchar(tissue)) {
      next
    }
    sample_count <- suppressWarnings(as.numeric(frame[[count_col]][[idx]]))
    if (!is.finite(sample_count) || sample_count <= 0) {
      next
    }
    dataset_token <- slug_tissue(tissue)
    rows[[length(rows) + 1]] <- list(
      dataset_id = sprintf("GTEX-%s-%s", toupper(release), dataset_token),
      title = sprintf("GTEx %s %s", release, tissue),
      tissues = list(tissue),
      mode = "remote",
      access = "free",
      sample_count = as.integer(round(sample_count)),
      release = release,
      source_dataset_id = dataset_id
    )
  }
  rows
}

read_tsv_from_url <- function(url) {
  resp <- httr::GET(url)
  status <- httr::status_code(resp)
  if (status >= 300) {
    body <- httr::content(resp, as = "text", encoding = "UTF-8")
    stop(sprintf("GTEx sample table request failed (%s) for %s: %s", status, url, body))
  }
  body_raw <- httr::content(resp, as = "raw")
  text <- ""
  if (grepl("\\.gz$", tolower(url))) {
    unzipped <- memDecompress(body_raw, type = "gzip")
    text <- rawToChar(unzipped)
  } else {
    text <- rawToChar(body_raw)
  }
  read.delim(
    text = text,
    sep = "\t",
    header = TRUE,
    stringsAsFactors = FALSE,
    check.names = FALSE,
    quote = "",
    comment.char = "",
    fill = TRUE
  )
}

load_sample_attributes_table <- function(release) {
  rel <- normalize_release_token(release)
  if (!nzchar(rel)) {
    return(NULL)
  }
  urls <- c(
    sprintf(
      "https://storage.googleapis.com/%s/annotations/%s/metadata-files/GTEx_Analysis_%s_Annotations_SampleAttributesDS.txt",
      gtex_gcs_bucket,
      rel,
      rel
    ),
    sprintf(
      "https://storage.googleapis.com/%s/annotations/%s/metadata-files/GTEx_Analysis_%s_Annotations_SampleAttributesDS.txt.gz",
      gtex_gcs_bucket,
      rel,
      rel
    ),
    sprintf(
      "https://storage.googleapis.com/gtex_analysis_%s/annotations/GTEx_Analysis_%s_Annotations_SampleAttributesDS.txt",
      rel,
      rel
    ),
    sprintf(
      "https://storage.googleapis.com/gtex_analysis_%s/annotations/GTEx_Analysis_%s_Annotations_SampleAttributesDS.txt.gz",
      rel,
      rel
    )
  )
  for (url in unique(urls)) {
    frame <- tryCatch(
      read_tsv_from_url(url),
      error = function(err) NULL
    )
    if (!is.null(frame) && nrow(frame) > 0) {
      return(frame)
    }
  }
  NULL
}

build_catalog_rows_from_annotations <- function(release) {
  frame <- load_sample_attributes_table(release)
  if (is.null(frame) || nrow(frame) == 0) {
    return(list())
  }

  tissue_col <- pick_column(frame, c("SMTSD", "tissueSiteDetail", "tissue", "tissue_site_detail"))
  if (!nzchar(tissue_col)) {
    return(list())
  }

  assay_col <- pick_column(frame, c("SMAFRZE", "sampleFreeze", "assay", "dataType"))
  if (nzchar(assay_col)) {
    assay_value <- toupper(trimws(as.character(frame[[assay_col]])))
    keep <- assay_value == "RNASEQ"
    if (any(keep, na.rm = TRUE)) {
      frame <- frame[keep, , drop = FALSE]
    }
  }
  if (nrow(frame) == 0) {
    return(list())
  }

  tissues <- trimws(as.character(frame[[tissue_col]]))
  tissues <- tissues[nzchar(tissues)]
  if (length(tissues) == 0) {
    return(list())
  }

  release_token <- normalize_release_token(release)
  dataset_token <- sprintf("gtex_%s", gsub("^v", "", release_token))
  counts <- sort(table(tissues), decreasing = TRUE)
  rows <- list()
  for (name in names(counts)) {
    sample_count <- suppressWarnings(as.integer(counts[[name]]))
    if (!is.finite(sample_count) || sample_count <= 0) {
      next
    }
    rows[[length(rows) + 1]] <- list(
      dataset_id = sprintf("GTEX-%s-%s", toupper(release_token), slug_tissue(name)),
      title = sprintf("GTEx %s %s", release_token, name),
      tissues = list(name),
      mode = "remote",
      access = "free",
      sample_count = sample_count,
      release = release_token,
      source_dataset_id = dataset_token
    )
  }
  rows
}

dedupe_rows <- function(rows) {
  if (length(rows) == 0) {
    return(list())
  }
  best_by_id <- new.env(parent = emptyenv())
  for (row in rows) {
    key <- as.character(row$dataset_id)
    if (!exists(key, envir = best_by_id, inherits = FALSE)) {
      assign(key, row, envir = best_by_id)
      next
    }
    current <- get(key, envir = best_by_id, inherits = FALSE)
    current_count <- suppressWarnings(as.numeric(current$sample_count))
    next_count <- suppressWarnings(as.numeric(row$sample_count))
    if (!is.finite(current_count)) current_count <- -1
    if (!is.finite(next_count)) next_count <- -1
    if (next_count > current_count) {
      assign(key, row, envir = best_by_id)
    }
  }
  keys <- ls(best_by_id)
  out <- lapply(keys, function(key) get(key, envir = best_by_id, inherits = FALSE))
  out <- out[order(
    vapply(out, function(item) {
      rel <- tolower(as.character(item$release))
      rel_num <- suppressWarnings(as.integer(gsub("[^0-9]", "", rel)))
      if (!is.finite(rel_num)) rel_num <- -1
      -rel_num
    }, integer(1)),
    vapply(out, function(item) tolower(as.character(item$title)), character(1))
  )]
  out
}

dataset_ids <- collect_dataset_ids()
release_ids <- unique(c(
  collect_releases_from_dataset_ids(dataset_ids),
  collect_releases_from_gcs(),
  "v8"
))
release_ids <- unique(release_ids[nzchar(release_ids)])
release_ids <- release_ids[order(vapply(release_ids, release_rank, integer(1)), decreasing = TRUE)]

all_rows <- list()
for (dataset_id in dataset_ids) {
  rows <- build_catalog_rows(dataset_id)
  if (length(rows) > 0) {
    all_rows <- c(all_rows, rows)
  }
}
for (release in release_ids) {
  rows <- build_catalog_rows_from_annotations(release)
  if (length(rows) > 0) {
    all_rows <- c(all_rows, rows)
  }
}

if (length(all_rows) == 0) {
  stop("GTEx catalog refresh returned no datasets.")
}

catalog_rows <- dedupe_rows(all_rows)
payload <- list(
  provider_id = "gtex",
  fetched_at = as.numeric(Sys.time()),
  source = "gtex_api_v2",
  datasets = catalog_rows
)

write(jsonlite::toJSON(payload, auto_unbox = TRUE, pretty = TRUE), file = output_path)
