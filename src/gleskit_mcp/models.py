from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    return value


def dump_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), indent=2, sort_keys=True)


@dataclass(slots=True)
class SessionNote:
    note_id: str
    title: str
    body_markdown: str
    created_at: str
    updated_at: str
    author: str = "user"
    linked_artifact_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionReport:
    report_id: str
    title: str
    body_markdown: str
    created_at: str
    updated_at: str
    author: str = "assistant"
    focus: str = ""
    template_id: str = ""
    custom_guidance: str = ""
    linked_artifact_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisSession:
    session_id: str
    title: str
    description: str
    species: str
    status: str
    created_at: str
    updated_at: str
    notes: list[SessionNote] = field(default_factory=list)
    reports: list[SessionReport] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Artifact:
    artifact_id: str
    session_id: str
    kind: str
    title: str
    summary: str
    created_at: str
    provider: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Cohort:
    cohort_id: str
    title: str
    provider_id: str
    source_kind: str
    counts_path: str
    metadata_path: str
    summary: str
    created_at: str
    updated_at: str
    species: str = "human"
    status: str = "ready"
    profile: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    inspection: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorkspaceRuntime:
    workspace_root: Path
    updated_at: str
    active_session_id: str | None = None
    capture_enabled: bool = True
    ui_name: str = "GLESKit Desktop"
