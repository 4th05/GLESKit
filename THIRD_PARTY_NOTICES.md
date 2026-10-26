# Third-party materials

The [GLESKit license](LICENSE) applies only to original GLESKit materials.
Dependencies and external data/services retain their own licenses and terms;
they are not relicensed under GLESKit's CC BY-NC-SA 4.0 license.

This inventory is not a replacement for upstream license texts or legal advice.
The exact-byte source-only review is recorded in `docs/licensing-review.json`.
It does not approve redistribution of installed dependencies or a prebuilt image;
that separate review remains required.

## Source-only distribution boundary

The intended GitHub source snapshot does not include installed Python packages,
`node_modules`, compiled frontend assets, R, installed R libraries, an OpenCode
binary, local databases, research datasets, credentials, or Git development
history. Installation downloads dependencies separately. This boundary does not
by itself establish license compatibility: authored integrations, copied code,
assets, fonts, external service terms, and any combined distribution still need
review. A built web bundle includes third-party code and needs its own notices.

## Python dependencies

Declared dependencies are listed in `pyproject.toml`. Local metadata inspected
on 2026-10-25 reports the following license families; versions below are observed
locally, not a reproducible environment lock or approval of future versions.

| Package | Observed version | License indicated by installed metadata |
| --- | --- | --- |
| anndata | 0.12.17 | BSD-3-Clause |
| matplotlib | 3.10.8 | Matplotlib/PSF-style agreement; inspect bundled notices |
| openpyxl | 3.1.5 | MIT |
| pandas | 2.3.3 | BSD-3-Clause |
| scikit-learn | 1.9.0 | BSD-3-Clause |
| scipy | 1.17.1 | BSD; inspect bundled third-party notices |
| xlrd | 2.0.2 | BSD |
| mcp | 1.26.0 | MIT |
| starlette | 1.0.0 | BSD-3-Clause |
| uvicorn | 0.42.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| keyring | Not present in the inspected environment | Review required |
| ipykernel | 7.2.0 | BSD-3-Clause |
| jupyter | 1.1.1 | BSD |
| langchain | 1.2.15 | MIT |
| langchain-openai | 1.1.12 | MIT |
| langgraph | 1.1.6 | MIT |
| openai | 2.30.0 | Apache-2.0 |
| rapidfuzz | 3.14.5 | MIT |

Transitive dependencies, optional extras and native libraries retain their own
licenses. If redistributing installed artifacts, include the required notices
and meet any source/relinking requirements; those artifacts are not contained
in the GitHub source snapshot.

## Web dependencies

The exact npm dependency graph is recorded in `apps/web/package-lock.json`.

| Direct package | Locked version | Reported license |
| --- | --- | --- |
| jszip | 3.10.1 | MIT OR GPL-3.0-or-later; review the MIT alternative |
| katex | 0.16.44 | MIT; review font licenses separately |
| mermaid | 11.13.0 | MIT |
| vite (build tool) | 6.4.1 | MIT |

The locked graph also reports ISC, BSD-3-Clause, Apache-2.0, Unlicense,
MIT AND Zlib, and MPL-2.0 OR Apache-2.0 dependencies. Alternative license choices
must be documented for redistribution; do not treat an `OR` expression as requiring
both alternatives. Khroma 2.1.0 has no license field in the lockfile, but its
installed `license` file contains an MIT notice. Preserve that notice if its
code is redistributed. Do not silently assume missing metadata means permission.

## R analysis integrations

GLESKit invokes R-backed workflows through an external `Rscript` subprocess.
The source snapshot does not redistribute R or an installed R package library.
This process separation is an architectural fact, not a legal determination.

Observed project-local package `DESCRIPTION` files report:

| Package | Observed version | Reported license |
| --- | --- | --- |
| DOSE | 3.28.2 | Artistic-2.0 |
| clusterProfiler | 4.10.1 | Artistic-2.0 |
| ReactomePA | 1.46.0 | GPL-2 |
| org.Hs.eg.db | 3.18.0 | Artistic-2.0 |

R itself, jsonlite, transitive Bioconductor packages, and any package installed
outside the project-local library still need an exact-version review.
In particular, GPL-covered components cannot simply be relicensed with a
non-commercial restriction. Review whether the integration or any proposed
distribution creates a combined work with incompatible obligations before
approving publication or packaging. Do not remove or replace scientific
backends merely to make a licensing check pass.

## OpenCode, data, models, and assets

OpenCode is an independently installed executable, not part of the intended
source snapshot. Review the exact executable's license and any proposed
redistribution separately; do not assume the GLESKit license covers it.

Biology APIs, public cohort sources, benchmark datasets, model/API providers,
logos, fonts, and other assets retain their upstream rights and terms. A public
endpoint or downloadable dataset is not evidence of unrestricted redistribution.
Original GLESKit branding and any included copied assets must be reviewed too.
No local credentials, personal accounts, unpublished data, or runtime profiles
belong in a notice file or public source release.

## Pinned container boundary (Linux ARM64)

Docker build instructions are now source-release candidates. A locally built
image additionally contains Python 3.12.13, R 4.3.2, OpenCode 1.18.23, compiled
frontend code and native/system libraries. Publishing that image is a separate
redistribution decision; it is not authorized by the source-release license.

The machine-readable [container inventory](docker/third-party-inventory.json)
records 71 installed Python dependency/build-runtime metadata entries (including
pip, excluding GLESKit), 220 locked R packages, the build-only R helper, and the
204-entry npm lock graph, including optional/build packages. Exact R source URLs,
versions, hashes and reported licenses are in `docker/r-packages.lock.json`;
Python wheel hashes are in `docker/python.lock`. Base-image digests, CPython and
OpenCode archives, the Ubuntu snapshot and all 385 OS package versions are in
`docker/runtime.json`. Metadata is evidence for review, not legal approval.

OpenCode's exact upstream tag reports MIT terms:
[v1.18.23 LICENSE](https://github.com/anomalyco/opencode/blob/v1.18.23/LICENSE).
Its bundled libraries/notices still need a complete binary redistribution review.
Installed Python/R package license files and Ubuntu copyright files remain in
the image; this inventory does not replace those notices or a corresponding-source
package where a component's license requires one. In particular, review the
R/GPL/LGPL and OS-library source, modification/relinking and notice obligations,
frontend/font notices and the original-brand asset rights before distribution.

GLESKit's non-commercial restriction applies only to original GLESKit material.
Do not apply that restriction to upstream R, GPL/LGPL-covered libraries or other
independently licensed components. Whether the integrations are a permissible
aggregate rather than a combined work remains an explicit compatibility-review
question; process separation alone is not an approval. No scientific dependency
was removed or replaced to avoid this review. `container_review_complete` stays
false until the exact proposed distribution has been reviewed.
