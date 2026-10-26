# Use the local web app

Use Docker for the supported researcher installation on Apple Silicon macOS.
Run the commands below from the project root unless another directory is specified.

## Contents

- [Start and stop Docker](#docker-source-installation-apple-silicon)
- [Configure providers and models](#configure-providers-and-models)
- [Choose a workspace](#choose-a-workspace)
- [Upload files or download a cohort](#upload-files-or-download-a-cohort)
- [Continue a chat](#continue-a-chat)
- [Back up saved data](#persistent-data-and-backup)
- [Mount a selected input folder](#resource-and-file-access-boundaries)
- [Rebuild after a code update](#rebuild-after-a-code-update)
- [Troubleshoot](#troubleshoot)
- [Run natively for development](#start-from-a-source-checkout)
- [Verify a change](#verification)

## Docker source installation (Apple Silicon)

1. Start Docker Desktop with Engine 28+ and Docker Compose. Use a local Linux
   ARM64 engine; other architectures and remote engines are not supported.
2. Open the project folder in a terminal.
3. Run:

   ```sh
   sh scripts/docker_ui.sh open
   ```

The first launch builds the pinned environment and can take 20 minutes or more.
The final image is about **6 GB**, excluding research data. Build-cache and
first-build peak disk usage have not been measured. See [runtime versions](runtime-baseline.md).

| To do this | Run this |
| --- | --- |
| Open or reuse the app | `sh scripts/docker_ui.sh open` or `make ui_docker` |
| Check status | `sh scripts/docker_ui.sh status` or `make status_ui_docker` |
| Stop the app and all its children | `sh scripts/docker_ui.sh stop` or `make close_ui_docker` |
| Select Docker Desktop's local context explicitly | `DOCKER_CONTEXT=desktop-linux sh scripts/docker_ui.sh open` |

Only `127.0.0.1:8123` is published. The launcher opens an authenticated browser
session; typing that address alone does not bypass authentication. Keep the
private launch link to yourself. Closing the tab leaves analyses running; **stop**
quits the app without deleting data or stopping unrelated services.

## Configure providers and models

Open **Settings** in the sidebar footer on Workspace, Cohorts or Chat.

| To do this | In Settings |
| --- | --- |
| Add a provider | **Providers & models** → select a provider and enter its key |
| Choose models | Search the catalog, enable models, choose a default, then **Save & activate** |
| Edit an existing connection | Select the saved connection; a blank key field retains its saved key |
| Replace a key | Edit that connection, enter the new key, then save |
| Reuse a detected key | Choose **Or use an existing .env key**, then save and activate |
| Reload model metadata | **Providers & models** → **Refresh models** |
| Start, stop or reconnect the private engine | **OpenCode** |
| Add personal instructions | **Instructions** |
| Change workspace or update gene references | **Workspace & environment** |

Connections, enabled models and keys survive restarts. The Chat picker shows the
models enabled for the selected connection. An empty password input does not
mean its saved key was lost.

Model catalogs are not proof of account access. Default OpenAI/Anthropic catalogs
can use anonymous models.dev metadata before a key is supplied. Custom endpoints
query their own `/models` service; manual model IDs are available when discovery
is unsupported. Native OpenCode OAuth is not supported by the web provider form.

Docker keys live in the volume's private profile `.env`, not the checkout's
`.env`. Docker never imports host keys automatically. In native default-profile
mode, detected project `.env` keys can be explicitly imported; isolated/custom
profiles do not inherit them. See [credential handling](privacy.md#local-state-and-credentials).

In Docker, `127.0.0.1` provider URLs refer to the container, not the Mac. A host-local
model server is not automatically bridged or bundled.

## Choose a workspace

- To work without persistence, use Chat without creating or activating a session.
- To save research work, create or select a session explicitly in **Workspace**.
- To change storage, open **Settings → Workspace & environment**:

| Selection | Effect |
| --- | --- |
| **Use configured workspace** | Opens the workspace selected by GLESKit configuration |
| **Browse configured workspace (read-only)** | Allows inspection; disables edits and MCP capture |
| **Use separate web workspace** | Uses a separate workspace inside the private web profile |

Switching storage clears capture and stops the private engine. Confirm the
explained reload, then reopen Chat or start the engine explicitly. Do not run
other writers against the same sessions concurrently.

A fresh Docker volume starts empty. Existing host sessions and paper/evaluation
outputs are not imported. Native default launches use the configured workspace;
custom profiles default to a separate workspace unless explicitly overridden.

## Upload files or download a cohort

- Use browser uploads for local files. Copied uploads have a **1 GiB per-file limit**.
- For larger files, use an explicitly selected read-only input mount below.
- In **Cohorts**, select a provider/dataset and start the download. The source
  panel locks while its overlay shows progress and elapsed time. Use **Cancel**
  when available; completion, error or cancellation unlocks the panel.
- Download figures through their UI links. Internal paths are not browser URLs.

A typed Mac path is not a path inside Docker. Uploads and selected mounts do not
change the original input files.

## Continue a chat

1. Open **Chat** and select a saved thread in the sidebar.
2. Choose its provider/model and submit your next message.
3. Read partial output while the thinking indicator is active; inspect tool
   calls in the separate **Activity** panel.

GLESKit attaches its agent prompt, skill and matched references to every turn.
Additional instructions belong in Settings. Missing required instruction files
stop submission; injection does not guarantee model obedience.

Use **Cancel** to stop an active turn. After a lost connection, inspect the saved
conversation before sending again: GLESKit does not silently resubmit prompts.
Only one browser tab controls writes at a time; use explicit takeover to switch.
Errors, warnings and confirmations appear in dismissible dialogs.

## Persistent data and backup

Saved work, chat, settings and credentials are in **`gleskit-local_state`**.
Stopping preserves the volume. Deleting it erases that data.

1. Stop the app with `sh scripts/docker_ui.sh stop`.
2. Change to an existing private directory **outside the repository**.
3. Create an archive:

   ```sh
   umask 077
   docker --context desktop-linux run --rm --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true --mount type=volume,src=gleskit-local_state,dst=/state,readonly --entrypoint /bin/tar gleskit-local:arm64 -czf - -C /state . > gleskit-state-backup.tar.gz
   ```

Check the command succeeds and test the archive before relying on it. It includes
API keys, chat and research data: keep it private and out of Git. Do not use
`docker compose down -v` to stop the app; it deletes the volume.

## Resource and file access boundaries

The initial container limits are 2 CPUs, 6 GiB RAM, two analysis workers and one R
thread, with a 4 GiB R vector limit. Larger analyses may require higher limits.
The filesystem is read-only except for its private state volume and temporary
storage. OpenCode/MCP/R ports are not published.

To expose one selected host data folder read-only:

1. Stop the app.
2. Create the ignored `compose.override.yaml`, replacing only the source path:

   ```yaml
   services:
     gleskit:
       volumes:
         - type: bind
           source: /absolute/path/to/selected-data-directory
           target: /inputs
           read_only: true
           bind:
             create_host_path: false
   ```

3. Start and open it with the override explicitly:

   ```sh
   docker --context desktop-linux compose --env-file /dev/null -p gleskit-local -f compose.yaml -f compose.override.yaml up -d --wait --no-build --pull never gleskit
   open "$(docker --context desktop-linux compose --env-file /dev/null -p gleskit-local -f compose.yaml -f compose.override.yaml exec -T gleskit python -m gleskit_mcp.web.container --launch-info)"
   ```

4. Use `/inputs/...` in the UI. Stop this configuration with:

   ```sh
   docker --context desktop-linux compose --env-file /dev/null -p gleskit-local -f compose.yaml -f compose.override.yaml down
   ```

Keep the base state-volume mapping. Never mount the home directory, credentials,
Docker socket or development `var/`. The helper deliberately ignores overrides:
its status/stop still work, but open refuses to change an override-backed instance.
Stop it and use the normal helper to return to the default configuration.

## Rebuild after a code update

Normal open does not rebuild an existing image. After updating source:

```sh
sh scripts/docker_ui.sh stop
docker --context desktop-linux compose --env-file /dev/null -p gleskit-local -f compose.yaml build --builder desktop-linux --provenance=false gleskit
sh scripts/docker_ui.sh open
```

Scientific layers are reused when their inputs are unchanged. Rebuilds do not
import host environments, upgrade locked scientific dependencies or erase state.

## Troubleshoot

| Problem | Action |
| --- | --- |
| Docker engine unavailable | Start Docker Desktop; select a local context and unset `DOCKER_HOST`/TLS overrides |
| Port 8123 already used | Identify the owner; stop only your GLESKit instance or resolve the conflict explicitly |
| Page has no authenticated session | Run the open helper again; do not reuse a stale private launch link |
| Chat cannot send | Check the active connection in **Providers & models**, then engine status under **OpenCode** |
| Expected model is missing | Enable it in the connection; use **Refresh models** or the manual ID fallback |
| Saved key field is blank | Check the saved-key indicator before entering a replacement |
| Existing host sessions are absent in Docker | Docker uses its own volume; it does not import the host workspace |
| Connection lost during a turn | Reopen that conversation and inspect its saved answer before sending again |
| Need to refresh the interface | Reload the browser; this can discard unsaved input and does not cancel an active turn |

For external requests, credential permissions and unsupported public MCP exposure,
see [privacy boundaries](privacy.md).

## Start from a source checkout

**Development only.** The supported researcher web-app installation uses Docker.
Native setup needs Python 3.11+, Node/npm for the first build, OpenCode for Chat,
and host R/scientific packages for R-backed tools.

```sh
make ui
make close_ui
```

The launcher reuses a compatible `.venv`; otherwise it installs MCP/web extras in
`.venv-web`. It does not install host R or reconstruct the pinned scientific runtime.
`make close_ui` stops only this checkout's verified hosts and descendants, keeps
saved data and works without rebuilding the frontend. Closing a tab does not quit.

| To do this | Command |
| --- | --- |
| Choose a port | `python3 scripts/run_local_web.py --port 8123` |
| Use a fresh isolated profile | `python3 scripts/run_local_web.py --profile /absolute/path/to/new-profile` |
| Attach the configured workspace explicitly | `python3 scripts/run_local_web.py --workspace-source configured` |
| Use a separate workspace | `python3 scripts/run_local_web.py --workspace-source separate` |
| Choose OpenCode explicitly | `python3 scripts/run_local_web.py --opencode /absolute/path/to/opencode` |
| Close one registered profile | `make close_ui CLOSE_UI_ARGS="--profile /absolute/path/to/web-profile"` |

The default profile is `var/local-web/profile/`, separate from scientific storage.
Do not use an unrelated nonempty directory as a profile. `make web_app` aliases
`make ui`; `make kill_opencode` aliases scoped `make close_ui`.

## Verification

With development dependencies installed:

```sh
PYTHONPATH=src .venv/bin/python -m unittest tests.test_web_shell tests.test_web_relay tests.test_lazy_figures -v
npm --prefix apps/web test
npm --prefix apps/web run build
```

To test the pinned OpenCode protocol with synthetic local credentials only:

```sh
GLESKIT_RUN_WEB_ENGINE_TESTS=1 PYTHONPATH=src .venv/bin/python -m unittest tests.test_web_engine -v
```
