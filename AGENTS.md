# GLESKit

## Repo Shape
- Main product is the Python package in `src/gleskit_mcp`; `apps/web` is the Vite/JavaScript frontend and `src/gleskit_mcp/web` is its local Python host.
- Primary Python entrypoints are `src/gleskit_mcp/cli.py` (`gleskit-mcp` / `python -m gleskit_mcp`) and `src/gleskit_mcp/mcp_server.py` for MCP serving.
- The web host reuses the Python workspace/scientific services and starts its own private OpenCode/MCP children on demand. The retired Rust/Tauri shell is not part of the current app.

## Commands
- Python tests: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`
- Focus one test module: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_services -v`
- Focus one test class: `PYTHONPATH=src .venv/bin/python -m unittest tests.test_services.GLESKitServiceTests -v`
- Install MCP-only extras when working on the server/tool bridge: `.venv/bin/python -m pip install -e ".[mcp]"` or `make install-mcp`
- Common Make targets are real sources of truth: `make test`, `make reference_status`, `make update_references`, `make workspace_status`, `make serve_mcp`, `make expose_mcp`, `make ui`, `make close_ui`
- UI: `make ui` / `make close_ui`; frontend checks: `npm --prefix apps/web test` and `npm --prefix apps/web run build`. `make desk_app` only aliases `make ui`.

## Config And Runtime
- Settings merge `config/gleskit.defaults.json` with ignored `config/gleskit.local.json`; env vars override both.
- Path values support `${PROJECT_ROOT}` expansion. Default runtime data goes under `var/`; the local config example points at `.gleskit/` instead.
- If you need a local override, start from `config/gleskit.local.json.example`; do not edit tracked defaults for machine-specific paths.
- Local workspace mode is optional. If the local UI runtime heartbeat is absent, Python/MCP behavior must still work in stateless mode.

## Behavior Guardrails
- Preserve the stateless-first model: chat must not silently create persistent sessions or switch the active session.
- The UI owns active-session selection; chat/storage actions should only happen when the user explicitly asks.
- Destructive workspace operations should keep preview/confirmation behavior.

## Integration Gotchas
- Workspace CLI commands are consumed by the UI and other clients as JSON parsed from stdout. Do not add incidental stdout logging to those code paths; errors should stay on stderr / nonzero exit.
- When changing figure delivery, browser-facing clients should use `download_links[].http_url` when present. Payload-local paths like `png_path`, `svg_path`, `display_path`, and `figure_files` are internal only and must not be surfaced as download URLs.

## Analysis Backends
- R-backed workflows are repo-significant: `gene-set-disease`, `enrich-genes`, `ranked-gsea`, and `compare-programs` depend on `Rscript` plus `jsonlite`, `DOSE`, `clusterProfiler`, `ReactomePA`, and `org.Hs.eg.db`.
- The default project-local R library path is `var/r_lib` unless overridden in config/env.
- Standard unit tests are `unittest`-based and mostly isolated, but the disease normalizer/golden suites are opt-in via `GLESKIT_RUN_EXTERNAL_DISEASE_NORMALIZER_TESTS=1`, `GLESKIT_RUN_EXTERNAL_DISEASE_PROTOTYPE_TESTS=1`, and `GLESKIT_RUN_EXTERNAL_DISEASE_GOLDEN_TESTS=1`.

## Ignore / Generated State
- Do not commit local runtime/output state under `var/`, frontend dependencies/builds under `apps/web/node_modules/` or `apps/web/dist-web/`, temporary material under `tmp/`, or developer-account profiles.
- Keep only `notebooks/eval/ncs_manuscript_figures_and_tables.ipynb`; preserve its inputs/assets/results. Release audits live in `scripts/release`, independently of the retired native installer.
- `config/gleskit.local.*` is intentionally ignored except for `config/gleskit.local.json.example`.
