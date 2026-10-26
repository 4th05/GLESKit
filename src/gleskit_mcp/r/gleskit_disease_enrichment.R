script_args <- commandArgs(trailingOnly = FALSE)
script_path <- sub("^--file=", "", grep("^--file=", script_args, value = TRUE)[1])
if (is.na(script_path) || script_path == "") {
  stop("Unable to determine the current script path for gleskit_disease_enrichment.R.")
}
source(file.path(dirname(normalizePath(script_path)), "gleskit_common.R"))

io <- gleskit_read_cli_io("gleskit_disease_enrichment.R")
input_path <- io$input_path
output_path <- io$output_path

gleskit_require_packages(c("DOSE"), "disease enrichment")
request <- gleskit_read_request(input_path)

entrez_gene_ids <- unique(trimws(as.character(unlist(request$entrez_gene_ids, use.names = FALSE))))
entrez_gene_ids <- entrez_gene_ids[nzchar(entrez_gene_ids)]
database <- toupper(gleskit_string_value(request$database, "DO"))
limit <- max(1L, min(gleskit_int_value(request$limit, 15L), 30L))

empty_result <- list(
  provider = "yulab_dose",
  summary = paste0(
    "Retrieved 0 disease enrichment term(s) from ",
    database,
    " for ",
    length(entrez_gene_ids),
    " resolved gene(s)."
  ),
  database = database,
  analysis_mode = "ora",
  input_gene_count = length(entrez_gene_ids),
  term_count = 0L,
  terms = list()
)

if (length(entrez_gene_ids) == 0) {
  gleskit_write_output(empty_result, output_path)
  quit(save = "no", status = 0)
}

run_enrichment <- function(db, gene_ids) {
  if (db == "DO") {
    return(DOSE::enrichDO(gene = gene_ids, pvalueCutoff = 1, pAdjustMethod = "BH", readable = FALSE))
  }
  if (db == "DGN") {
    return(DOSE::enrichDGN(gene = gene_ids, pvalueCutoff = 1, pAdjustMethod = "BH"))
  }
  if (db == "NCG") {
    if (!exists("enrichNCG", where = asNamespace("DOSE"), mode = "function")) {
      stop("The installed DOSE package does not expose enrichNCG().")
    }
    return(get("enrichNCG", envir = asNamespace("DOSE"))(gene = gene_ids, pvalueCutoff = 1, pAdjustMethod = "BH"))
  }
  stop(paste0("Unsupported disease enrichment database: ", db))
}

result_obj <- tryCatch(
  run_enrichment(database, entrez_gene_ids),
  error = function(err) {
    stop(conditionMessage(err))
  }
)

result_df <- if (is.null(result_obj)) {
  data.frame()
} else {
  as.data.frame(result_obj)
}

if (nrow(result_df) == 0) {
  jsonlite::write_json(empty_result, output_path, auto_unbox = TRUE, pretty = TRUE, null = "null")
  quit(save = "no", status = 0)
}

if (!("p.adjust" %in% colnames(result_df)) && ("pvalue" %in% colnames(result_df))) {
  result_df[["p.adjust"]] <- result_df[["pvalue"]]
}

if (!("Count" %in% colnames(result_df)) && ("geneID" %in% colnames(result_df))) {
  result_df[["Count"]] <- vapply(result_df[["geneID"]], function(x) length(gleskit_split_ids(x)), integer(1))
}

order_key <- if ("p.adjust" %in% colnames(result_df)) result_df[["p.adjust"]] else result_df[["pvalue"]]
result_df <- result_df[order(order_key, na.last = TRUE), , drop = FALSE]
result_df <- head(result_df, limit)

terms <- lapply(seq_len(nrow(result_df)), function(i) {
  row <- result_df[i, , drop = FALSE]
  list(
    term_id = gleskit_string_value(row[["ID"]]),
    term_name = gleskit_string_value(row[["Description"]]),
    source = database,
    p_value = if ("pvalue" %in% names(row)) suppressWarnings(as.numeric(row[["pvalue"]][[1]])) else NULL,
    adjusted_p_value = if ("p.adjust" %in% names(row)) suppressWarnings(as.numeric(row[["p.adjust"]][[1]])) else NULL,
    q_value = if ("qvalue" %in% names(row)) suppressWarnings(as.numeric(row[["qvalue"]][[1]])) else NULL,
    gene_ratio = gleskit_string_value(row[["GeneRatio"]]),
    background_ratio = gleskit_string_value(row[["BgRatio"]]),
    rich_factor = if ("RichFactor" %in% names(row)) suppressWarnings(as.numeric(row[["RichFactor"]][[1]])) else NULL,
    fold_enrichment = if ("FoldEnrichment" %in% names(row)) suppressWarnings(as.numeric(row[["FoldEnrichment"]][[1]])) else NULL,
    z_score = if ("zScore" %in% names(row)) suppressWarnings(as.numeric(row[["zScore"]][[1]])) else NULL,
    intersection_size = if ("Count" %in% names(row)) suppressWarnings(as.integer(row[["Count"]][[1]])) else length(gleskit_split_ids(row[["geneID"]][[1]])),
    gene_ids = gleskit_split_ids(row[["geneID"]][[1]])
  )
})

output <- list(
  provider = "yulab_dose",
  summary = paste0(
    "Retrieved ",
    length(terms),
    " disease enrichment term(s) from ",
    database,
    " for ",
    length(entrez_gene_ids),
    " resolved gene(s)."
  ),
  database = database,
  analysis_mode = "ora",
  input_gene_count = length(entrez_gene_ids),
  term_count = length(terms),
  terms = terms
)

gleskit_write_output(output, output_path)
