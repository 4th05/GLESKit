get_script_path <- function() {
  args <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) == 0) {
    return("")
  }
  sub("^--file=", "", file_arg[[1]])
}

gleskit_read_cli_io <- function(script_name) {
  args <- commandArgs(trailingOnly = TRUE)

  read_arg <- function(flag) {
    idx <- match(flag, args)
    if (is.na(idx) || idx >= length(args)) {
      return("")
    }
    args[[idx + 1]]
  }

  input_path <- read_arg("--input")
  output_path <- read_arg("--output")
  if (input_path == "" || output_path == "") {
    stop(paste0("Usage: ", script_name, " --input INPUT.json --output OUTPUT.json"))
  }
  list(input_path = input_path, output_path = output_path)
}

gleskit_require_packages <- function(pkgs, context, jsonlite_only_ok = FALSE) {
  if (!requireNamespace("jsonlite", quietly = TRUE)) {
    stop("Missing required R package 'jsonlite'.")
  }
  required_pkgs <- unique(pkgs[nzchar(pkgs)])
  if (jsonlite_only_ok && length(required_pkgs) == 0) {
    return(invisible(NULL))
  }
  missing_pkgs <- required_pkgs[!vapply(required_pkgs, requireNamespace, logical(1), quietly = TRUE)]
  if (length(missing_pkgs) > 0) {
    stop(
      paste0(
        "Missing required R packages for GLESKit ",
        context,
        ": ",
        paste(missing_pkgs, collapse = ", "),
        "."
      )
    )
  }
  invisible(NULL)
}

gleskit_read_request <- function(input_path) {
  jsonlite::fromJSON(input_path, simplifyVector = FALSE)
}

gleskit_write_output <- function(output, output_path) {
  jsonlite::write_json(output, output_path, auto_unbox = TRUE, pretty = TRUE, null = "null")
}

gleskit_write_plot_files <- function(plot_obj, png_path, svg_path = "", width = 10, height = 7, dpi = 150) {
  if (!nzchar(png_path)) {
    stop("A PNG output path is required for plot rendering.")
  }
  dir.create(dirname(png_path), recursive = TRUE, showWarnings = FALSE)
  if (nzchar(svg_path)) {
    dir.create(dirname(svg_path), recursive = TRUE, showWarnings = FALSE)
  }

  render_once <- function(open_device, output_path) {
    success <- FALSE
    open_device()
    on.exit({
      try(grDevices::dev.off(), silent = TRUE)
      if (!success && file.exists(output_path)) {
        unlink(output_path)
      }
    }, add = TRUE)
    print(plot_obj)
    success <- TRUE
    invisible(NULL)
  }

  render_once(
    function() grDevices::png(filename = png_path, width = width, height = height, units = "in", res = dpi, bg = "white"),
    png_path
  )
  gc(verbose = FALSE)

  if (nzchar(svg_path)) {
    tryCatch(
      withCallingHandlers(
        {
          render_once(
            function() grDevices::svg(filename = svg_path, width = width, height = height, bg = "white"),
            svg_path
          )
        },
        warning = function(w) {
          invokeRestart("muffleWarning")
        }
      ),
      error = function(err) {
        if (file.exists(svg_path)) {
          unlink(svg_path)
        }
        invisible(NULL)
      }
    )
    gc(verbose = FALSE)
  }

  list(
    png_created = file.exists(png_path),
    svg_created = nzchar(svg_path) && file.exists(svg_path)
  )
}

gleskit_string_value <- function(x, default = "") {
  if (is.null(x)) {
    return(default)
  }
  token <- trimws(as.character(x[[1]]))
  if (identical(token, "")) {
    return(default)
  }
  token
}

gleskit_int_value <- function(x, default = 15L) {
  if (is.null(x)) {
    return(default)
  }
  value <- suppressWarnings(as.integer(x[[1]]))
  if (is.na(value)) {
    return(default)
  }
  value
}

gleskit_split_ids <- function(value) {
  token <- gleskit_string_value(list(value), "")
  if (identical(token, "")) {
    return(character(0))
  }
  parts <- unlist(strsplit(token, "/", fixed = TRUE), use.names = FALSE)
  parts <- trimws(as.character(parts))
  unique(parts[nzchar(parts)])
}

gleskit_species_token <- function(species) {
  token <- gleskit_string_value(list(species), "")
  token <- tolower(token)
  token <- gsub("\\s+", "_", token)
  token
}

gleskit_is_human_species <- function(species) {
  token <- gleskit_species_token(species)
  token %in% c("human", "homo_sapiens", "9606")
}

gleskit_is_mouse_species <- function(species) {
  token <- gleskit_species_token(species)
  token %in% c("mouse", "mus_musculus", "10090")
}

gleskit_is_pseudomonas_species <- function(species) {
  token <- gleskit_species_token(species)
  token %in% c("pseudomonas_aeruginosa", "pseudomonas_aeruginosa_ucbpp_pa14", "pseudomonas_aeruginosa_pa14", "208963", "pau")
}

gleskit_species_orgdb_package <- function(species) {
  if (gleskit_is_human_species(species)) {
    return("org.Hs.eg.db")
  }
  if (gleskit_is_mouse_species(species)) {
    return("org.Mm.eg.db")
  }
  ""
}

gleskit_species_reactome_organism <- function(species) {
  if (gleskit_is_human_species(species)) {
    return("human")
  }
  if (gleskit_is_mouse_species(species)) {
    return("mouse")
  }
  ""
}

gleskit_species_kegg_organism <- function(species) {
  if (gleskit_is_human_species(species)) {
    return("hsa")
  }
  if (gleskit_is_mouse_species(species)) {
    return("mmu")
  }
  if (gleskit_is_pseudomonas_species(species)) {
    return("pau")
  }
  token <- gleskit_species_token(species)
  if (nzchar(token)) {
    return(substr(token, 1L, 3L))
  }
  ""
}

gleskit_safe_integer <- function(value, default = NA_integer_) {
  if (is.null(value) || length(value) == 0) {
    return(default)
  }
  parsed <- suppressWarnings(as.integer(value[[1]]))
  if (is.na(parsed)) {
    return(default)
  }
  parsed
}

gleskit_safe_numeric <- function(value, default = NA_real_) {
  if (is.null(value) || length(value) == 0) {
    return(default)
  }
  parsed <- suppressWarnings(as.numeric(value[[1]]))
  if (is.na(parsed)) {
    return(default)
  }
  parsed
}

gleskit_ratio_parts <- function(value) {
  token <- gleskit_string_value(list(value), "")
  if (!nzchar(token)) {
    return(list(numerator = NA_integer_, denominator = NA_integer_))
  }
  parts <- unlist(strsplit(token, "/", fixed = TRUE), use.names = FALSE)
  if (length(parts) < 2) {
    return(list(numerator = NA_integer_, denominator = NA_integer_))
  }
  list(
    numerator = suppressWarnings(as.integer(parts[[1]])),
    denominator = suppressWarnings(as.integer(parts[[2]]))
  )
}

gleskit_enrichment_odds_ratio <- function(query_size, term_size, intersection_size, background_size) {
  query_size <- suppressWarnings(as.numeric(query_size))
  term_size <- suppressWarnings(as.numeric(term_size))
  intersection_size <- suppressWarnings(as.numeric(intersection_size))
  background_size <- suppressWarnings(as.numeric(background_size))
  if (is.na(query_size) || is.na(term_size) || is.na(intersection_size) || is.na(background_size)) {
    return(NA_real_)
  }
  if (query_size <= 0 || term_size <= 0 || background_size <= 0) {
    return(NA_real_)
  }
  a <- intersection_size
  b <- max(0, query_size - intersection_size)
  c <- max(0, term_size - intersection_size)
  d <- max(0, background_size - a - b - c)
  if (b <= 0 || c <= 0) {
    if (a > 0 && d > 0) {
      return(Inf)
    }
    return(NA_real_)
  }
  (a * d) / (b * c)
}
