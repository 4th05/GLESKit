# Reproducible runtime baseline

The portable target is native **Linux ARM64**, tested under Docker Desktop on
Apple Silicon macOS. Exact public inputs are recorded in
[`docker/runtime.json`](../docker/runtime.json),
[`docker/python.lock`](../docker/python.lock) and
[`docker/r-packages.lock.json`](../docker/r-packages.lock.json).
These locks reconstruct a Linux runtime; they are not copies of a developer's
Mac Python/R installation.

## Contents

- [Inspect pinned versions](#inspect-pinned-versions)
- [Required R packages](#required-r-packages)
- [Build and runtime boundaries](#build-and-runtime-boundaries)

## Inspect pinned versions

| Component | Pinned version / boundary |
| --- | --- |
| Python | 3.12.13; 70 hash-locked runtime distributions, plus the pinned pip bootstrap |
| R | 4.3.2; 220 locked packages, including 10 bundled with R |
| Bioconductor package generation | 3.18 |
| OpenCode | 1.18.23, checksum-verified Linux ARM64 executable |
| Frontend builder | Node 24.22.1 / npm 11.19.1; build stage only |
| Linux userspace | Ubuntu 22.04, snapshot 20261024T000000Z; 385 exact package versions |

Base and Node builder images are pinned by platform digest. Python/OpenCode
archives, package artifacts and build helpers have recorded SHA-256 values.
No host credentials, local libraries, scientific sessions or data volumes are
build inputs. Source-only distribution does not ship a prebuilt runtime image.

## Required R packages

| Package | Version |
| --- | --- |
| apeglm | 1.24.0 |
| cBioPortalData | 2.14.2 |
| clusterProfiler | 4.10.1 |
| DESeq2 | 1.42.1 |
| DOSE | 3.28.2 |
| enrichplot | 1.22.0 |
| ggplot2 | 3.5.2 |
| ggtangle | 0.0.7 |
| httr | 1.4.7 |
| jsonlite | 2.0.0 |
| MultiAssayExperiment | 1.28.0 |
| org.Hs.eg.db | 3.18.0 |
| org.Mm.eg.db | 3.18.0 |
| ReactomePA | 1.46.0 |
| recount3 | 1.12.0 |
| SummarizedExperiment | 1.32.0 |
| TCGAbiolinks | 2.30.4 |

## Build and runtime boundaries

The final R library is `/opt/gleskit/r-library`, separate from the fresh private
`/app/var` volume. Installed versions are checked against the lock. The build-only
`tidytree 0.4.6` compatibility helper is discarded; the runtime retains the locked
`tidytree 0.4.7`. The pinned setuptools wheel is also build-only. See the recorded
compatibility notes in `docker/runtime.json`; scientific package versions are not
silently upgraded to make a build succeed.

The UI contains prebuilt frontend assets. Normal launches do not compile R,
install dependencies, rebuild the UI or download a scientific reference bundle.
Missing public sources fail the build rather than falling back to newer packages.
Subsequent app-only builds reuse the scientific layers.

The native development environment and its ignored captures under `var/` are
left unchanged. They are neither release inputs nor a portable runtime guarantee.
Intel, other operating systems and emulated images require separate validation.
