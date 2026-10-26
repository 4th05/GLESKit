#!/bin/sh
# Local Docker Desktop launcher. Do not trace the private browser handoff.
set +x
set -eu

fail() { printf '%s\n' "GLESKit: $*" >&2; exit 1; }
usage() { printf '%s\n' 'Usage: sh scripts/docker_ui.sh open|stop|status'; }
case ${1-} in
    open|stop|status) task_action=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
[ "$#" -eq 1 ] || { usage >&2; exit 2; }

task_root=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd -P)
task_project=gleskit-local
task_image=gleskit-local:arm64
task_volume=gleskit-local_state
[ -f "$task_root/compose.yaml" ] || fail 'Run the launcher from a complete GLESKit checkout.'
command -v docker >/dev/null 2>&1 || fail 'Install Docker Desktop for Apple Silicon, then start it.'
[ -z "${DOCKER_HOST-}${DOCKER_TLS_VERIFY-}${DOCKER_CERT_PATH-}" ] ||
    fail 'Unset DOCKER_HOST, DOCKER_TLS_VERIFY and DOCKER_CERT_PATH; select a local Docker context instead.'
task_context=$(docker context show 2>/dev/null) || fail 'Cannot read the selected Docker context.'
case $task_context in ''|*[!a-zA-Z0-9_.-]*) fail 'Invalid Docker context name.' ;; esac
task_endpoint=$(docker context inspect "$task_context" --format '{{.Endpoints.docker.Host}}' 2>/dev/null) ||
    fail 'Cannot inspect the selected Docker context.'
case $task_endpoint in unix:///*) ;; *) fail 'Only a local Unix-socket Docker engine is allowed; remote engines are refused.' ;; esac

engine() { docker --context "$task_context" "$@"; }
# Explicit file/project/env selection: never load developer .env or override files.
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
COMPOSE_DISABLE_ENV_FILE=1 COMPOSE_REMOVE_ORPHANS=0 COMPOSE_BAKE=false
export COMPOSE_DISABLE_ENV_FILE COMPOSE_REMOVE_ORPHANS COMPOSE_BAKE
compose() {
    engine compose --ansi never --env-file /dev/null --project-name "$task_project" \
        --project-directory "$task_root" -f "$task_root/compose.yaml" "$@"
}
task_runtime=$(engine info --format '{{.ServerVersion}}|{{.OSType}}|{{.Architecture}}' 2>/dev/null) ||
    fail 'The local Docker engine is unavailable. Start Docker Desktop and retry.'
task_version=${task_runtime%%|*}
task_major=${task_version%%.*}
case $task_major in ''|*[!0-9]*) fail 'Cannot verify the Docker Engine version.' ;; esac
[ "$task_major" -ge 28 ] || fail 'Docker Engine 28 or newer is required for safe localhost port publishing.'
case $task_runtime in "$task_version|linux|arm64"|"$task_version|linux|aarch64") ;; *)
    fail 'This image requires a native Linux ARM64 engine. Other architectures are not yet validated.' ;;
esac
compose version --short >/dev/null 2>&1 || fail 'Install the Docker Compose plugin supplied with Docker Desktop.'

verify_image() {
    task_metadata=$(engine image inspect "$1" --format '{{.Os}}|{{.Architecture}}|{{index .Config.Labels "org.gleskit.app"}}' 2>/dev/null) ||
        fail 'Cannot verify the local GLESKit image.'
    [ "$task_metadata" = 'linux|arm64|local-web' ] || fail 'The image is not the expected GLESKit ARM64 application.'
}

find_container() {
    task_container=$(engine ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$task_project") ||
        fail 'Cannot inspect GLESKit containers.'
    [ -n "$task_container" ] || return 0
    case $task_container in *[!a-f0-9]*) fail 'Multiple or unrecognized containers use the GLESKit project name; nothing was changed.' ;; esac
    [ "${#task_container}" -eq 64 ] || fail 'Invalid GLESKit container identity.'
    task_owner=$(engine inspect --type container --format '{{index .Config.Labels "org.gleskit.app"}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.oneoff"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$task_container") ||
        fail 'Cannot verify GLESKit container ownership.'
    task_expected="local-web|$task_project|gleskit|False|$task_root|$task_root/compose.yaml"
    # Explicit read-only input mounts may be used through the documented manual commands.
    case $task_owner in "$task_expected"|"$task_expected,$task_root/compose.override.yaml") ;; *)
        fail 'The GLESKit project belongs to another checkout or configuration; nothing was changed.' ;;
    esac
    task_container_image=$(engine inspect --type container --format '{{.Image}}' "$task_container") || fail 'Cannot verify the container image.'
    verify_image "$task_container_image"
    task_state=$(engine inspect --type container --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$task_container") ||
        fail 'Cannot inspect GLESKit health.'
}

verify_boundary() {
    task_boundary=$(engine inspect --type container --format '{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.Privileged}}|{{range $port, $bindings := .HostConfig.PortBindings}}{{$port}}={{range $bindings}}{{.HostIp}}:{{.HostPort}},{{end}};{{end}}' "$task_container") ||
        fail 'Cannot verify the container security boundary.'
    [ "$task_boundary" = '10001:10001|true|false|8123/tcp=127.0.0.1:8123,;' ] ||
        fail 'Unexpected container user, filesystem, privileges or published ports; no browser was opened.'
}

# A fixed volume name must not adopt an unrelated pre-existing Docker volume.
task_existing_volume=$(engine volume ls --quiet --filter "name=^${task_volume}$") || fail 'Cannot inspect the state volume.'
if [ -n "$task_existing_volume" ]; then
    [ "$task_existing_volume" = "$task_volume" ] || fail 'Ambiguous GLESKit state volume.'
    task_volume_owner=$(engine volume inspect "$task_volume" --format '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}') ||
        fail 'Cannot verify the state volume.'
    [ "$task_volume_owner" = "$task_project|state" ] || fail 'The state volume is not owned by this GLESKit Compose project; nothing was changed.'
fi
find_container

case $task_action in
    status)
        if [ -n "$task_container" ]; then
            printf 'GLESKit Docker: %s (%s).\n' "${task_state%%|*}" "${task_state#*|}"
        else
            printf '%s\n' 'GLESKit Docker: stopped.'
        fi
        printf '%s\n' 'Private state: gleskit-local_state (preserved by stop).'
        exit 0 ;;
    stop)
        if [ -n "$task_container" ]; then
            compose down --timeout 20 || fail 'Shutdown was incomplete. No data volume was deleted; retry status/stop.'
        fi
        printf '%s\n' 'GLESKit Docker stopped. Saved data and credentials are preserved.'
        exit 0 ;;
esac

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] ||
    fail 'Automatic browser opening is currently validated only on Apple Silicon macOS.'
command -v open >/dev/null 2>&1 || fail 'The macOS browser-open command is unavailable.'
if [ -n "$task_container" ]; then
    [ "$task_owner" = "$task_expected" ] ||
        fail 'This app uses an explicit input override. Use the documented override launch commands; stop remains available here.'
    # Check the actual published boundary before handing a capability to a browser.
    verify_boundary
fi

if [ -z "$task_container" ] || [ "$task_state" != 'running|healthy' ]; then
    task_available=$(engine image ls --quiet --filter "reference=$task_image") || fail 'Cannot inspect local images.'
    if [ -z "$task_available" ]; then
        # Use only the built-in builder for the verified local context, never a selected remote builder.
        task_builder=$(engine buildx inspect "$task_context" 2>/dev/null) ||
            fail 'The local Docker Buildx plugin is required for the first build.'
        printf '%s\n' "$task_builder" | awk -v context="$task_context" '
            $1 == "Driver:" { drivers++; if ($2 != "docker" || NF != 2) bad = 1 }
            $1 == "Endpoint:" { nodes++; if ($2 != context || NF != 2) bad = 1 }
            END { exit !(drivers == 1 && nodes == 1 && !bad) }
        ' ||
            fail 'Cannot verify a context-local Docker builder. No source files were sent to a builder.'
        printf '%s\n' 'First launch: building the pinned Python/R/OpenCode runtime locally. This can take 20 minutes or more and several GB; later opens reuse it.'
        DOCKER_BUILDKIT=1 BUILDX_GIT_INFO=0 BUILDX_GIT_LABELS=0 BUILDX_NO_DEFAULT_ATTESTATIONS=1 \
            compose build --builder "$task_context" --provenance=false gleskit || fail 'The initial local build failed; no browser was opened.'
    fi
    verify_image "$task_image"
    printf '%s\n' 'Starting GLESKit on http://127.0.0.1:8123; waiting for readiness (up to 60 seconds).'
    compose up -d --wait --wait-timeout 60 --no-build --no-recreate --pull never --no-deps gleskit ||
        fail 'GLESKit did not become ready. Port 8123 may be occupied; no other process was stopped. Use status or stop to inspect/close this app.'
    find_container
    [ -n "$task_container" ] && [ "$task_state" = 'running|healthy' ] || fail 'No verified healthy GLESKit instance is available.'
    verify_boundary
else
    printf '%s\n' 'Reusing the running GLESKit app.'
fi

# Never print the URL, raw helper errors, or browser errors: they may contain the capability.
task_url=$(compose exec -T gleskit python -m gleskit_mcp.web.container --launch-info 2>/dev/null) ||
    fail 'Cannot obtain a verified private browser handoff. Retry open; no prompt or action was resent.'
task_nonce=${task_url#http://127.0.0.1:8123/\#launch=}
[ "$task_nonce" != "$task_url" ] && [ "${#task_nonce}" -eq 43 ] || fail 'Invalid private browser handoff.'
case $task_nonce in *[!a-zA-Z0-9_-]*) fail 'Invalid private browser handoff.' ;; esac
if ! open "$task_url" >/dev/null 2>&1; then
    unset task_url task_nonce
    fail 'The app is running, but the browser could not open. Retry open.'
fi
unset task_url task_nonce
printf '%s\n' 'GLESKit opened. Closing its tab keeps analyses running; use stop to quit the app.'
