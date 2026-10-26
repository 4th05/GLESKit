# Install an exact, hashed R source lock. Run only in the Linux build environment.
# Bootstrap identity is supplied from the same lock by the Python build stage.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 5L) {
  stop("Usage: Rscript install-r.R LOCK LIBRARY CACHE JSONLITE_URL JSONLITE_SHA256")
}
lock_path <- args[[1]]
library_path <- args[[2]]
cache_path <- args[[3]]
bootstrap_url <- args[[4]]
bootstrap_sha <- args[[5]]
if (!identical(as.character(getRversion()), "4.3.2")) {
  stop("This lock requires R 4.3.2; no automatic version substitution is allowed.")
}
if (!identical(unname(Sys.info()["sysname"]), "Linux") ||
    !identical(unname(Sys.info()["machine"]), "aarch64")) {
  stop("This installer is only validated for Linux ARM64; the host library is not modified.")
}
dir.create(library_path, recursive = TRUE, showWarnings = FALSE)
dir.create(cache_path, recursive = TRUE, showWarnings = FALSE)
.libPaths(unique(c(library_path, .libPaths())))
Sys.setenv(MAKEFLAGS = "-j2", OMP_NUM_THREADS = "1", OPENBLAS_NUM_THREADS = "1")
options(timeout = max(900L, getOption("timeout", 60L)))

valid_url <- function(url) {
  is.character(url) && length(url) == 1L && !is.na(url) &&
    (grepl("^https://(cran[.]r-project[.]org|bioconductor[.]org|codeload[.]github[.]com)/[A-Za-z0-9._/-]+$", url) ||
     grepl("^https://ftp[.]gwdg[.]de/pub/misc/bioconductor/[A-Za-z0-9._/-]+$", url))
}

installed_version <- function(name, lib) {
  version <- tryCatch(
    suppressWarnings(utils::packageDescription(name, lib.loc = lib, fields = "Version")),
    error = function(e) NA_character_
  )
  if (is.character(version) && length(version) == 1L && !is.na(version)) version else ""
}

source_urls <- function(url) {
  cran <- "https://cran.r-project.org/src/contrib/"
  bioc <- "https://bioconductor.org/"
  mirror <- "https://ftp.gwdg.de/pub/misc/bioconductor/"
  if (startsWith(url, cran)) {
    filename <- basename(url)
    name <- sub("_.*$", "", filename)
    return(unique(c(url, paste0(cran, filename), paste0(cran, "Archive/", name, "/", filename))))
  }
  if (startsWith(url, bioc)) {
    return(c(paste0(mirror, substring(url, nchar(bioc) + 1L)), url))
  }
  if (startsWith(url, mirror)) {
    return(c(url, paste0(bioc, substring(url, nchar(mirror) + 1L))))
  }
  url
}

verify_sha <- function(path, expected) {
  if (!is.character(expected) || length(expected) != 1L || is.na(expected) ||
      !grepl("^[0-9a-f]{64}$", expected)) {
    stop("Invalid source SHA-256 in lock.")
  }
  output <- system2("sha256sum", shQuote(path), stdout = TRUE, stderr = TRUE)
  if (!is.null(attr(output, "status")) || length(output) != 1L ||
      !identical(substr(output[[1]], 1L, 64L), expected)) {
    stop("Source archive failed SHA-256 verification.")
  }
  invisible(TRUE)
}

fetch <- function(url, expected, filename) {
  if (!valid_url(url) || basename(filename) != filename) {
    stop("Unsafe source URL or filename in lock.")
  }
  path <- file.path(cache_path, filename)
  if (file.exists(path)) {
    verify_sha(path, expected)
    return(path)
  }
  partial <- paste0(path, ".part")
  on.exit(unlink(partial), add = TRUE)
  status <- 1L
  for (candidate in source_urls(url)) {
    if (!valid_url(candidate)) stop("Unsafe fallback URL.")
    status <- tryCatch(
      utils::download.file(candidate, partial, mode = "wb", method = "libcurl", quiet = TRUE),
      error = function(e) 1L
    )
    if (identical(status, 0L)) break
  }
  if (!identical(status, 0L)) {
    stop("Locked source download failed; do not substitute a different package version.")
  }
  verify_sha(partial, expected)
  if (!file.rename(partial, path)) stop("Cannot finalize verified source archive.")
  path
}

install_source <- function(path, package_name = "jsonlite", lib = library_path) {
  log <- tempfile("gleskit-r-install-", fileext = ".log")
  on.exit(unlink(log), add = TRUE)
  build_env <- c(paste0("R_LIBS=", shQuote(library_path)),
                 paste0("R_LIBS_USER=", shQuote(lib)))
  if (identical(package_name, "fgsea")) {
    # fgsea 1.28 requests C++11; the locked BH 1.87 headers require C++14.
    makevars <- tempfile("gleskit-r-makevars-")
    writeLines("CXX11STD = -std=gnu++14", makevars)
    on.exit(unlink(makevars), add = TRUE)
    build_env <- c(build_env, paste0("R_MAKEVARS_USER=", shQuote(makevars)))
  }
  status <- system2(
    file.path(R.home("bin"), "R"),
    c("CMD", "INSTALL", "--no-multiarch",
      paste0("--library=", shQuote(normalizePath(lib))), shQuote(path)),
    stdout = log, stderr = log, env = build_env
  )
  if (!identical(status, 0L)) {
    cat(paste(utils::tail(readLines(log, warn = FALSE), 60L), collapse = "\n"), "\n", file = stderr())
    stop("Locked R source installation failed; see the build diagnostics above.")
  }
}

install_with_helpers <- function(path, row, helpers) {
  build_library <- tempfile("gleskit-r-build-library-")
  dir.create(build_library)
  on.exit(unlink(build_library, recursive = TRUE), add = TRUE)
  for (helper in helpers) {
    if (!grepl("^[A-Za-z][A-Za-z0-9.]*$", helper$name) ||
        !grepl("^[A-Za-z0-9.+-]+$", helper$version)) stop("Invalid build-only package identity.")
    source <- fetch(helper$url, helper$sha256, paste0(helper$name, "_", helper$version, ".tar.gz"))
    install_source(source, helper$name, build_library)
    if (!identical(installed_version(helper$name, build_library), helper$version)) {
      stop("Build-only package version check failed.")
    }
  }
  install_source(path, row$name, build_library)
  if (!identical(installed_version(row$name, build_library), row$version)) {
    stop("Isolated R build version check failed.")
  }
  # These R-only packages retain a citation closure during byte compilation.
  # Relocate only the requested package; never ship the older helper library.
  if (file.exists(file.path(library_path, row$name)) ||
      !file.copy(file.path(build_library, row$name), library_path,
                 recursive = TRUE, copy.mode = TRUE, copy.date = TRUE)) {
    stop("Cannot publish the isolated R build into the locked library.")
  }
}

bootstrap_file <- fetch(bootstrap_url, bootstrap_sha, "jsonlite-bootstrap.tar.gz")
install_source(bootstrap_file)
if (!requireNamespace("jsonlite", quietly = TRUE, lib.loc = library_path)) {
  stop("Verified JSON parser bootstrap failed.")
}
lock <- jsonlite::fromJSON(lock_path, simplifyVector = FALSE)
if (!identical(lock$schema, 1L) || !identical(lock$r_version, "4.3.2") ||
    !identical(lock$platform, "linux/arm64")) {
  stop("Unsupported R source lock schema, version or platform.")
}
packages <- lock$packages
names(packages) <- vapply(packages, function(row) row$name, character(1))
if (anyDuplicated(names(packages)) || anyDuplicated(unlist(lock$install_order)) ||
    !setequal(names(packages), unlist(lock$install_order))) {
  stop("Incomplete or duplicate locked package order.")
}
bootstrap <- packages[["jsonlite"]]
if (is.null(bootstrap) || !identical(bootstrap$url, bootstrap_url) ||
    !identical(bootstrap$sha256, bootstrap_sha) ||
    !identical(installed_version("jsonlite", library_path), bootstrap$version)) {
  stop("Bootstrap identity differs from the reviewed R lock.")
}

for (name in unlist(lock$install_order)) {
  row <- packages[[name]]
  if (!grepl("^[A-Za-z][A-Za-z0-9.]*$", name) ||
      !grepl("^[A-Za-z0-9.+-]+$", row$version)) {
    stop("Invalid package identity in lock.")
  }
  package_library <- if (isTRUE(row$bundled_with_r)) .Library else library_path
  installed <- installed_version(name, package_library)
  if (isTRUE(row$bundled_with_r)) {
    if (!identical(installed, row$version)) stop("Bundled R package version differs from lock.")
    next
  }
  if (identical(name, "jsonlite")) next
  if (identical(installed, row$version) &&
      name %in% rownames(utils::installed.packages(lib.loc = library_path))) next
  cat("Installing locked R package: ", name, " ", row$version, "\n", sep = "")
  path <- fetch(row$url, row$sha256, paste0(name, "_", row$version, ".tar.gz"))
  helpers <- Filter(function(helper) name %in% unlist(helper$used_to_build), lock$build_only_packages)
  if (length(helpers)) install_with_helpers(path, row, helpers) else install_source(path, name)
  installed <- installed_version(name, library_path)
  if (!identical(installed, row$version)) {
    stop(paste0("Locked R package installation failed: ", name))
  }
}
for (name in names(packages)) {
  package_library <- if (isTRUE(packages[[name]]$bundled_with_r)) .Library else library_path
  if (!identical(installed_version(name, package_library), packages[[name]]$version) ||
      !requireNamespace(name, quietly = TRUE, lib.loc = package_library)) {
    stop(paste0("Locked R namespace/version check failed: ", name))
  }
}
cat("R source lock verified: ", length(packages), " packages.\n", sep = "")
