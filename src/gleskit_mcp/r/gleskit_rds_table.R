script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_rds_table.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_rds_table.R")
input_path <- io$input_path
response_path <- io$output_path

gleskit_require_packages(c("jsonlite"), "RDS result-table conversion", jsonlite_only_ok = TRUE)
request <- gleskit_read_request(input_path)

rds_path <- gleskit_string_value(request$rds_path, "")
if (!nzchar(rds_path) || !file.exists(rds_path)) {
  stop(paste0("RDS input file not found: ", rds_path))
}
table_output_path <- gleskit_string_value(request$output_path, "")
if (!nzchar(table_output_path)) {
  stop("RDS conversion requires a non-empty output_path.")
}

object <- readRDS(rds_path)
source_class <- class(object)

coerce_to_dataframe <- function(value) {
  if (methods::is(value, "DESeqResults")) {
    frame <- as.data.frame(value, stringsAsFactors = FALSE)
    row_ids <- rownames(frame)
    if (!is.null(row_ids) && length(row_ids) == nrow(frame)) {
      frame$gene <- as.character(row_ids)
      frame <- frame[, c("gene", setdiff(names(frame), "gene")), drop = FALSE]
    }
    return(frame)
  }
  if (is.data.frame(value)) {
    return(value)
  }
  if (is.matrix(value)) {
    return(as.data.frame(value, stringsAsFactors = FALSE))
  }
  if (is.list(value)) {
    if (!is.null(value$results) && is.data.frame(value$results)) {
      return(value$results)
    }
    if (!is.null(value$table) && is.data.frame(value$table)) {
      return(value$table)
    }
  }
  if (methods::is(value, "DataFrame")) {
    return(as.data.frame(value, stringsAsFactors = FALSE))
  }
  as.data.frame(value, stringsAsFactors = FALSE)
}

frame <- tryCatch(
  coerce_to_dataframe(object),
  error = function(err) {
    stop(
      paste0(
        "Unable to coerce the RDS object into a tabular result. ",
        "Object classes: ",
        paste(source_class, collapse = ", "),
        ". Error: ",
        conditionMessage(err)
      )
    )
  }
)

if (!nrow(frame)) {
  stop("The RDS object converted to an empty table.")
}

row_ids <- rownames(frame)
has_meaningful_rownames <- !is.null(row_ids) &&
  length(row_ids) == nrow(frame) &&
  any(nzchar(trimws(as.character(row_ids)))) &&
  !identical(as.character(row_ids), as.character(seq_len(nrow(frame))))

if (has_meaningful_rownames) {
  existing_names <- tolower(names(frame))
  preferred_column <- if ("gene" %in% existing_names) "row_id" else "gene"
  frame[[preferred_column]] <- as.character(row_ids)
  frame <- frame[, c(preferred_column, setdiff(names(frame), preferred_column)), drop = FALSE]
}

dir.create(dirname(table_output_path), recursive = TRUE, showWarnings = FALSE)
utils::write.table(
  frame,
  file = table_output_path,
  sep = "\t",
  quote = FALSE,
  row.names = FALSE,
  col.names = TRUE
)

gleskit_write_output(
  list(
    output_path = table_output_path,
    row_count = nrow(frame),
    column_count = ncol(frame),
    column_names = as.list(names(frame)),
    source_class = as.list(as.character(source_class))
  ),
  output_path = response_path
)
