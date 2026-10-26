from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    project_root: Path
    defaults_config_path: Path
    local_config_path: Path
    workspace_default_root: Path
    runtime_root: Path
    runtime_manifest_path: Path
    reference_root: Path
    reference_manifest_path: Path
    r_lib_root: Path
    rscript_bin: str
    default_species: str = "human"
    runtime_ttl_seconds: int = 30
    local_max_workers: int = 1
    r_threads: int = 1
    r_max_vsize: str = "100Gb"
    local_r_timeout_seconds: int = 45
    figure_r_timeout_seconds: int = 45
    fastmcp_host: str = "127.0.0.1"
    fastmcp_port: int = 8000
    fastmcp_streamable_http_path: str = "/mcp"
    public_base_url: str = ""
    download_public_base_url: str = ""
    download_path_prefix: str = "/figure-download"
    download_bind_host: str = "127.0.0.1"
    download_port: int = 0
    figure_download_ttl_seconds: int = 1800
    figure_download_max_entries: int = 256
    upload_preview_max_lines: int = 100
    artifact_preview_max_lines: int = 100


def _coerce_int(value: Any, default: int, *, minimum: int = 1) -> int:
    raw = str(value if value is not None else "").strip()
    if not raw:
        return max(minimum, int(default))
    try:
        parsed = int(raw)
    except ValueError:
        return max(minimum, int(default))
    return max(minimum, parsed)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _config_get(config: dict[str, Any], *keys: str) -> Any:
    node: Any = config
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _expand_project_root_token(value: Any, project_root: Path) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    return text.replace("${PROJECT_ROOT}", str(project_root))


def _resolve_path(value: Any, *, project_root: Path, default: str) -> Path:
    text = _expand_project_root_token(value, project_root) or _expand_project_root_token(default, project_root)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _normalize_path_prefix(value: Any, *, default: str) -> str:
    token = str(value if value is not None else "").strip()
    if not token:
        token = default
    return "/" + token.strip().strip("/")


def _looks_like_workspace_root(path: Path) -> bool:
    return any((path / marker).exists() for marker in ("sessions", "reports", "cohorts"))


def _prefer_existing_workspace_root(path: Path, fallback: Path) -> Path:
    resolved = path.resolve()
    fallback_resolved = fallback.resolve()
    if resolved.exists() or not _looks_like_workspace_root(fallback_resolved):
        return resolved
    return fallback_resolved


def load_settings(project_root: Path | None = None) -> Settings:
    root = (project_root or Path.cwd()).resolve()
    defaults_config_path = Path(
        os.environ.get("GLESKIT_DEFAULT_CONFIG_PATH", root / "config" / "gleskit.defaults.json")
    ).expanduser()
    local_config_path = Path(os.environ.get("GLESKIT_LOCAL_CONFIG_PATH", root / "config" / "gleskit.local.json")).expanduser()
    defaults_config = _read_json_object(defaults_config_path)
    local_config = _read_json_object(local_config_path)
    config = _deep_merge(defaults_config, local_config)

    configured_workspace_root = _resolve_path(
        os.environ.get("GLESKIT_WORKSPACE_HOME")
        or _config_get(config, "paths", "workspace_root"),
        project_root=root,
        default="${PROJECT_ROOT}/var/gleskit_mcp",
    )
    default_workspace_root = _resolve_path(
        None,
        project_root=root,
        default="${PROJECT_ROOT}/var/gleskit_mcp",
    )
    workspace_default_root = _prefer_existing_workspace_root(
        configured_workspace_root,
        default_workspace_root,
    )
    runtime_root = _resolve_path(
        os.environ.get("GLESKIT_RUNTIME_HOME")
        or _config_get(config, "paths", "runtime_root"),
        project_root=root,
        default="${PROJECT_ROOT}/var/gleskit_runtime",
    )
    reference_root = _resolve_path(
        os.environ.get("GLESKIT_REFERENCE_HOME")
        or _config_get(config, "paths", "reference_root"),
        project_root=root,
        default="${PROJECT_ROOT}/var/gleskit_reference",
    )
    r_lib_root = _resolve_path(
        os.environ.get("GLESKIT_R_LIB_HOME")
        or _config_get(config, "paths", "r_lib_root"),
        project_root=root,
        default="${PROJECT_ROOT}/var/r_lib",
    )

    default_species = str(
        os.environ.get("GLESKIT_DEFAULT_SPECIES")
        or _config_get(config, "runtime", "default_species")
        or "human"
    ).strip() or "human"

    local_max_workers_raw = os.environ.get("GLESKIT_LOCAL_MAX_WORKERS")
    if local_max_workers_raw is None:
        local_max_workers_raw = _config_get(config, "runtime", "local_max_workers")

    cpu_count = os.cpu_count() or 1
    if str(local_max_workers_raw if local_max_workers_raw is not None else "").strip().lower() == "auto":
        local_max_workers = max(1, cpu_count)
    else:
        local_max_workers = _coerce_int(local_max_workers_raw, cpu_count)

    rscript_bin = str(
        os.environ.get("GLESKIT_RSCRIPT_BIN")
        or _config_get(config, "tools", "rscript_bin")
        or "Rscript"
    ).strip() or "Rscript"

    return Settings(
        project_root=root,
        defaults_config_path=defaults_config_path,
        local_config_path=local_config_path,
        workspace_default_root=workspace_default_root,
        runtime_root=runtime_root,
        runtime_manifest_path=runtime_root / "workspace.json",
        reference_root=reference_root,
        reference_manifest_path=reference_root / "manifest.json",
        r_lib_root=r_lib_root,
        rscript_bin=rscript_bin,
        default_species=default_species,
        runtime_ttl_seconds=_coerce_int(
            os.environ.get("GLESKIT_RUNTIME_TTL_SECONDS") or _config_get(config, "runtime", "runtime_ttl_seconds"),
            30,
        ),
        local_max_workers=local_max_workers,
        r_threads=_coerce_int(os.environ.get("GLESKIT_R_THREADS") or _config_get(config, "runtime", "r_threads"), 1),
        r_max_vsize=(
            os.environ.get("GLESKIT_R_MAX_VSIZE")
            or str(_config_get(config, "runtime", "r_max_vsize") or "100Gb")
            or "100Gb"
        ).strip()
        or "100Gb",
        local_r_timeout_seconds=_coerce_int(
            os.environ.get("GLESKIT_LOCAL_R_TIMEOUT_SECONDS")
            or _config_get(config, "runtime", "local_r_timeout_seconds"),
            45,
        ),
        figure_r_timeout_seconds=_coerce_int(
            os.environ.get("GLESKIT_FIGURE_R_TIMEOUT_SECONDS")
            or _config_get(config, "runtime", "figure_r_timeout_seconds"),
            45,
        ),
        fastmcp_host=(
            os.environ.get("FASTMCP_HOST")
            or str(_config_get(config, "server", "host") or "127.0.0.1")
            or "127.0.0.1"
        ).strip()
        or "127.0.0.1",
        fastmcp_port=_coerce_int(
            os.environ.get("FASTMCP_PORT") or _config_get(config, "server", "port"),
            8000,
            minimum=0,
        ),
        fastmcp_streamable_http_path=_normalize_path_prefix(
            os.environ.get("FASTMCP_STREAMABLE_HTTP_PATH")
            or _config_get(config, "server", "streamable_http_path"),
            default="/mcp",
        ),
        public_base_url=(
            os.environ.get("GLESKIT_PUBLIC_BASE_URL")
            or str(_config_get(config, "server", "public_base_url") or "")
        ).strip(),
        download_public_base_url=(
            os.environ.get("GLESKIT_DOWNLOAD_PUBLIC_BASE_URL")
            or str(_config_get(config, "server", "download_public_base_url") or "")
        ).strip(),
        download_path_prefix=_normalize_path_prefix(
            os.environ.get("GLESKIT_DOWNLOAD_PATH_PREFIX")
            or _config_get(config, "server", "download_path_prefix"),
            default="/figure-download",
        ),
        download_bind_host=(
            os.environ.get("GLESKIT_DOWNLOAD_BIND_HOST")
            or str(_config_get(config, "server", "download_bind_host") or "127.0.0.1")
            or "127.0.0.1"
        ).strip()
        or "127.0.0.1",
        download_port=_coerce_int(
            os.environ.get("GLESKIT_DOWNLOAD_PORT") or _config_get(config, "server", "download_port"),
            0,
            minimum=0,
        ),
        figure_download_ttl_seconds=_coerce_int(
            os.environ.get("GLESKIT_FIGURE_DOWNLOAD_TTL_SECONDS")
            or _config_get(config, "server", "figure_download_ttl_seconds"),
            1800,
        ),
        figure_download_max_entries=_coerce_int(
            os.environ.get("GLESKIT_FIGURE_DOWNLOAD_MAX_ENTRIES")
            or _config_get(config, "server", "figure_download_max_entries"),
            256,
        ),
        upload_preview_max_lines=_coerce_int(
            os.environ.get("GLESKIT_UPLOAD_PREVIEW_MAX_LINES")
            or _config_get(config, "runtime", "upload_preview_max_lines"),
            100,
        ),
        artifact_preview_max_lines=_coerce_int(
            os.environ.get("GLESKIT_ARTIFACT_PREVIEW_MAX_LINES")
            or _config_get(config, "runtime", "artifact_preview_max_lines"),
            100,
        ),
    )
