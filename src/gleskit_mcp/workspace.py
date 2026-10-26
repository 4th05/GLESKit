from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from html import unescape
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, TYPE_CHECKING

from gleskit_mcp.config import Settings
from gleskit_mcp.cohort_providers import list_cohort_catalog, list_cohort_providers
from gleskit_mcp.models import Artifact, Cohort, WorkspaceRuntime, dump_json, utc_now_iso
from gleskit_mcp.utils import clean_text
from gleskit_mcp.report_templates import get_report_template
from gleskit_mcp.store import WorkspaceStore

if TYPE_CHECKING:
    from gleskit_mcp.cohort_download import CohortDownloadBackend
    from gleskit_mcp.figures import FigureCatalog


def inspect_tabular_file(*args, **kwargs):
    from gleskit_mcp.cohort_library import inspect_tabular_file as inspect
    return inspect(*args, **kwargs)


class WorkspaceUnavailableError(RuntimeError):
    """Raised when the desktop workspace bridge is not currently available."""


def write_runtime_manifest(
    path: Path,
    *,
    workspace_root: Path,
    active_session_id: str | None = None,
    capture_enabled: bool = True,
    ui_name: str = "GLESKit Desktop",
    connection_state: str = "connected",
) -> WorkspaceRuntime:
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime = WorkspaceRuntime(
        workspace_root=workspace_root.resolve(),
        active_session_id=active_session_id,
        capture_enabled=capture_enabled,
        ui_name=ui_name,
        updated_at=utc_now_iso(),
    )
    payload = json.loads(dump_json(runtime))
    payload["connection_state"] = str(connection_state or "connected").strip() or "connected"
    path.write_text(dump_json(payload))
    return runtime


def clear_runtime_manifest(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active_session_id": None,
        "capture_enabled": False,
        "connection_state": "cleared",
        "ui_name": "GLESKit Desktop",
        "updated_at": utc_now_iso(),
    }
    path.write_text(dump_json(payload))


def _looks_like_workspace_root(path: Path) -> bool:
    return any((path / marker).exists() for marker in ("sessions", "reports", "cohorts"))


MARKDOWN_LINK_RE = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<url>[^)]+)\)")
EUROPE_PMC_URL_RE = re.compile(r"https?://(?:www\.)?europepmc\.org/article/(?:MED|PMC)/(?P<id>[A-Za-z0-9]+)", re.IGNORECASE)
PUBMED_URL_RE = re.compile(r"https?://(?:pubmed\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov/pubmed)/(?P<id>\d+)", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s);]+", re.IGNORECASE)


def _normalized_text(value: Any) -> str:
    text = unescape(str(value or "")).strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _titles_match(observed_title: str, expected_title: str) -> bool:
    observed = _normalized_text(observed_title)
    expected = _normalized_text(expected_title)
    if not observed or not expected:
        return False
    if observed == expected:
        return True
    if observed in expected or expected in observed:
        return True
    return SequenceMatcher(None, observed, expected).ratio() >= 0.92


def _normalize_url(value: Any) -> str:
    return str(value or "").strip().rstrip(".,);")


def _extract_paper_locator(url: str, segment: str) -> tuple[str | None, str | None]:
    normalized_url = _normalize_url(url)
    pmid_match = EUROPE_PMC_URL_RE.search(normalized_url) or PUBMED_URL_RE.search(normalized_url)
    pmid = pmid_match.group("id") if pmid_match else None
    doi_match = DOI_RE.search(normalized_url) or DOI_RE.search(segment)
    doi = doi_match.group(0).strip().rstrip(".,);") if doi_match else None
    return pmid, doi.lower() if doi else None


def _extract_visible_paper_title(segment: str) -> str | None:
    match = MARKDOWN_LINK_RE.search(segment)
    if match is None:
        return None
    prefix = segment[:match.start()].strip()
    quoted = re.findall(r'"([^"]+)"', prefix)
    if quoted:
        return quoted[-1].strip()
    link_text = str(match.group("text") or "").strip()
    lowered = link_text.lower()
    if link_text and lowered not in {"europe pmc", "pubmed", "doi", "paper", "article"}:
        return link_text
    return None


def _author_year_link_matches(segment: str, paper_entry: dict[str, str]) -> bool:
    """Accept an explicit clickable ``(Surname et al., Year)`` citation form.

    The report contract permits reader-friendly author-year citations when the
    retrieved paper metadata contains both fields.  Restrict the check to the
    link text itself so an unrelated author-year phrase beside a generic
    ``[Europe PMC]`` link cannot accidentally validate a citation.
    """
    match = MARKDOWN_LINK_RE.search(segment)
    if match is None:
        return False
    surname = _normalized_text(paper_entry.get("first_author_surname"))
    year = str(paper_entry.get("year") or "").strip()
    observed = _normalized_text(match.group("text") or "")
    if not surname or not year or not observed:
        return False
    observed_words = set(observed.split())
    return surname in observed_words and year in observed_words


class WorkspaceGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.runtime_root.mkdir(parents=True, exist_ok=True)

    def _read_runtime(self) -> tuple[WorkspaceRuntime | None, str]:
        path = self.settings.runtime_manifest_path
        if not path.exists():
            return None, "workspace_missing"
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None, "workspace_unavailable"
        connection_state = str(raw.get("connection_state") or raw.get("state") or "connected").strip().lower()
        if connection_state == "cleared":
            return None, "workspace_cleared"
        configured_workspace_root = self.settings.workspace_default_root.resolve()
        workspace_root_raw = str(raw.get("workspace_root") or "").strip()
        if workspace_root_raw:
            manifest_workspace_root = Path(workspace_root_raw).expanduser()
            if not manifest_workspace_root.is_absolute():
                manifest_workspace_root = self.settings.project_root / manifest_workspace_root
            resolved_workspace_root = manifest_workspace_root.resolve()
        else:
            resolved_workspace_root = configured_workspace_root
        if not resolved_workspace_root.exists() and _looks_like_workspace_root(configured_workspace_root):
            resolved_workspace_root = configured_workspace_root
        runtime = WorkspaceRuntime(
            workspace_root=resolved_workspace_root,
            active_session_id=(str(raw.get("active_session_id") or "").strip() or None),
            capture_enabled=bool(raw.get("capture_enabled", True)),
            ui_name=str(raw.get("ui_name") or "GLESKit Desktop").strip() or "GLESKit Desktop",
            updated_at=str(raw.get("updated_at") or "").strip(),
        )
        if not runtime.updated_at or not _is_fresh(runtime.updated_at, self.settings.runtime_ttl_seconds):
            return None, "workspace_stale"
        return runtime, "workspace_connected"

    def get_runtime(self) -> WorkspaceRuntime | None:
        runtime, _reason = self._read_runtime()
        return runtime

    def status(self) -> dict[str, Any]:
        runtime, reason = self._read_runtime()
        if runtime is None:
            return {
                "available": False,
                "reason": reason,
                "active_session_id": None,
                "capture_enabled": False,
            }
        return {
            "available": True,
            "reason": reason,
            "workspace_root": str(runtime.workspace_root),
            "active_session_id": runtime.active_session_id,
            "capture_enabled": runtime.capture_enabled,
            "updated_at": runtime.updated_at,
            "ui_name": runtime.ui_name,
            "connection_state": "connected",
        }

    def get_workspace(self) -> tuple[WorkspaceRuntime, "WorkspaceService"] | None:
        runtime, _reason = self._read_runtime()
        if runtime is None:
            return None
        from gleskit_mcp.figures import FigureCatalog
        return runtime, WorkspaceService(
            WorkspaceStore(runtime.workspace_root),
            figure_catalog=FigureCatalog(self.settings),
            settings=self.settings,
        )

    def require_workspace(self) -> tuple[WorkspaceRuntime, "WorkspaceService"]:
        pair = self.get_workspace()
        if pair is None:
            raise WorkspaceUnavailableError(
                "The local GLESKit workspace is unavailable. Open the desktop app to manage sessions or artifacts."
            )
        return pair


class WorkspaceService:
    def __init__(
        self,
        store: WorkspaceStore,
        figure_catalog: FigureCatalog | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self._figure_catalog = figure_catalog
        self.settings = settings
        self._cohort_download_backend: CohortDownloadBackend | None = None

    @property
    def figure_catalog(self) -> FigureCatalog:
        if self._figure_catalog is None:
            from gleskit_mcp.figures import FigureCatalog
            self._figure_catalog = FigureCatalog()
        return self._figure_catalog

    @figure_catalog.setter
    def figure_catalog(self, value: FigureCatalog) -> None:
        self._figure_catalog = value

    def create_session(
        self,
        *,
        title: str,
        description: str,
        species: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self.store.create_session(title=title, description=description, species=species, tags=tags)
        return self._session_preview(session)

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        session = self.store.update_session(
            session_id,
            title=title,
            description=description,
        )
        return self._session_preview(session)

    def add_note(
        self,
        session_id: str,
        *,
        title: str,
        body_markdown: str,
        author: str = "user",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        created = self.store.add_note(
            session_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        return self._note_payload(session_id, created)

    def list_notes(self, session_id: str) -> dict[str, Any]:
        notes = self.store.list_notes(session_id)
        return {
            "session_id": session_id,
            "note_count": len(notes),
            "notes": [self._note_preview(session_id, note) for note in notes],
        }

    def get_note(self, session_id: str, note_id: str) -> dict[str, Any]:
        note = self.store.get_note(session_id, note_id)
        return self._note_payload(session_id, note)

    def update_note(
        self,
        session_id: str,
        note_id: str,
        *,
        title: str,
        body_markdown: str,
        author: str = "user",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        note = self.store.update_note(
            session_id,
            note_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        return self._note_payload(session_id, note)

    def delete_note(self, session_id: str, note_id: str) -> dict[str, Any]:
        deleted_note_id = self.store.delete_note(session_id, note_id)
        return {"session_id": session_id, "deleted_note_id": deleted_note_id}

    def delete_notes(self, session_id: str, note_ids: list[str]) -> dict[str, Any]:
        deleted = self.store.delete_notes(session_id, note_ids)
        return {
            "session_id": session_id,
            "deleted_count": len(deleted),
            "deleted_note_ids": deleted,
        }

    def list_reports(self, session_id: str) -> dict[str, Any]:
        reports = self.store.list_reports(session_id)
        return {
            "session_id": session_id,
            "report_count": len(reports),
            "reports": [self._report_preview(session_id, report) for report in reports],
        }

    def list_cohorts(self) -> dict[str, Any]:
        cohorts = self.store.list_cohorts()
        return {
            "cohort_count": len(cohorts),
            "cohorts": [self._cohort_preview(cohort) for cohort in cohorts],
        }

    def list_cohort_providers(self) -> dict[str, Any]:
        project_root = self.settings.project_root if self.settings is not None else None
        providers = list_cohort_providers(project_root)
        return {
            "provider_count": len(providers),
            "providers": providers,
        }

    def list_cohort_catalog(self, provider_id: str) -> dict[str, Any]:
        project_root = self.settings.project_root if self.settings is not None else None
        datasets = list_cohort_catalog(provider_id, project_root=project_root)
        return {
            "provider_id": provider_id,
            "dataset_count": len(datasets),
            "datasets": datasets,
        }

    def get_cohort(self, cohort_id: str) -> dict[str, Any]:
        cohort = self.store.get_cohort(cohort_id)
        return self._cohort_payload(cohort)

    def create_cohort(
        self,
        *,
        title: str,
        counts_path: str,
        metadata_path: str,
        provider_id: str = "local_import",
        source_kind: str = "imported",
        species: str = "human",
        transpose: bool = False,
        sample_column: str = "",
        group_column: str = "",
        min_count: int = 10,
        sample_limit: int = 0,
        source_counts_path: str = "",
        source_metadata_path: str = "",
        source_counts_url: str = "",
        source_metadata_url: str = "",
        download_provider_id: str = "",
        download_dataset_id: str = "",
        download_tissue: str = "",
        download_meta: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        cohort = self.store.create_cohort(
            title=title,
            counts_path=counts_path,
            metadata_path=metadata_path,
            provider_id=provider_id,
            source_kind=source_kind,
            species=species,
            transpose=transpose,
            sample_column=sample_column,
            group_column=group_column,
            min_count=min_count,
            sample_limit=sample_limit,
            source_counts_path=source_counts_path,
            source_metadata_path=source_metadata_path,
            source_counts_url=source_counts_url,
            source_metadata_url=source_metadata_url,
            download_provider_id=download_provider_id,
            download_dataset_id=download_dataset_id,
            download_tissue=download_tissue,
            download_meta=download_meta,
            tags=tags,
        )
        return self._cohort_payload(cohort)

    def download_cohort(
        self,
        *,
        title: str,
        provider_id: str,
        dataset_id: str,
        tissue: str = "",
        species: str = "human",
        sample_limit: int = 0,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.settings is None:
            raise RuntimeError("Cohort download requires workspace settings.")
        from gleskit_mcp.cohort_download import CohortDownloadBackend
        backend = self._cohort_download_backend or CohortDownloadBackend(self.settings)
        self._cohort_download_backend = backend
        downloaded = backend.download(
            provider_id=provider_id,
            dataset_id=dataset_id,
            tissue=tissue,
            sample_limit=sample_limit if sample_limit > 0 else None,
        )
        cohort = self.store.create_cohort_from_frames(
            title=title,
            counts_frame=downloaded.counts,
            metadata_frame=downloaded.metadata,
            provider_id=provider_id,
            source_kind="downloaded",
            species=species,
            min_count=10,
            sample_limit=sample_limit,
            download_provider_id=provider_id,
            download_dataset_id=dataset_id,
            download_tissue=tissue,
            download_meta=downloaded.meta,
            tags=tags,
        )
        return self._cohort_payload(cohort)

    def update_cohort(self, cohort_id: str, *, title: str | None = None) -> dict[str, Any]:
        cohort = self.store.update_cohort(cohort_id, title=title)
        return self._cohort_payload(cohort)

    def delete_cohort(self, cohort_id: str) -> dict[str, Any]:
        deleted_cohort_id = self.store.delete_cohort(cohort_id)
        return {"deleted_cohort_id": deleted_cohort_id}

    def create_session_from_cohort(
        self,
        *,
        cohort_id: str,
        title: str,
        description: str,
        species: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self.store.create_session_from_cohort(
            cohort_id=cohort_id,
            title=title,
            description=description,
            species=species,
            tags=tags,
        )
        session["cohort_id"] = cohort_id
        return session

    def get_report(self, session_id: str, report_id: str) -> dict[str, Any]:
        report = self.store.get_report(session_id, report_id)
        return self._report_payload(session_id, report)

    def delete_reports(self, session_id: str, report_ids: list[str]) -> dict[str, Any]:
        deleted = self.store.delete_reports(session_id, report_ids)
        return {
            "session_id": session_id,
            "deleted_count": len(deleted),
            "deleted_report_ids": deleted,
        }

    def list_sessions(
        self,
        *,
        query: str = "",
        status: str | None = None,
        active_session_id: str | None = None,
    ) -> dict[str, Any]:
        token = query.strip().lower()
        sessions = []
        for session in self.store.list_sessions(status=status):
            haystack = " ".join(
                [
                    session.title,
                    session.description,
                    " ".join(session.tags),
                    " ".join(f"{note.title} {note.body_markdown}" for note in session.notes),
                ]
            ).lower()
            if token and token not in haystack:
                continue
            preview = self._session_preview(session)
            preview["artifact_count"] = len(self.store.list_artifacts(session.session_id))
            preview["is_active"] = session.session_id == active_session_id
            sessions.append(preview)
        return {"session_count": len(sessions), "sessions": sessions}

    def get_session_summary(self, session_id: str, *, active_session_id: str | None = None) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        artifacts = self.store.list_artifacts(session_id)
        counts: dict[str, int] = {}
        for artifact in artifacts:
            counts[artifact.kind] = counts.get(artifact.kind, 0) + 1
        return {
            **self._session_preview(session),
            "is_active": session.session_id == active_session_id,
            "artifact_count": len(artifacts),
            "artifact_counts": counts,
            "figure_count": counts.get("figure", 0),
            "note_count": len(session.notes),
            "report_count": len(session.reports),
            "notes": [self._note_preview(session_id, note) for note in self.store.list_notes(session_id)[:5]],
            "reports": [self._report_preview(session_id, report) for report in self.store.list_reports(session_id)[:5]],
        }

    def list_artifacts(self, session_id: str, *, kind: str | None = None) -> dict[str, Any]:
        artifacts = self.store.list_artifacts(session_id, kind=kind)
        return {
            "session_id": session_id,
            "artifact_count": len(artifacts),
            "artifacts": [self._artifact_preview(item) for item in artifacts],
        }

    def list_uploads(self, session_id: str) -> dict[str, Any]:
        uploads = self.store.list_artifacts(session_id, kind="upload")
        return {
            "session_id": session_id,
            "upload_count": len(uploads),
            "uploads": [self._upload_preview(item) for item in uploads],
        }

    def get_artifact(
        self,
        session_id: str | None,
        artifact_id: str,
        *,
        active_session_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_session_id = self._resolve_artifact_session_id(
            artifact_id,
            session_id=session_id,
            active_session_id=active_session_id,
        )
        artifact = self.store.get_artifact(resolved_session_id, artifact_id)
        return {
            "artifact_id": artifact.artifact_id,
            "session_id": artifact.session_id,
            "kind": artifact.kind,
            "title": artifact.title,
            "summary": artifact.summary,
            "created_at": artifact.created_at,
            "provider": artifact.provider,
            "provenance": artifact.provenance,
            "payload": artifact.payload,
        }

    def get_upload(
        self,
        session_id: str | None,
        upload_id: str,
        *,
        active_session_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self.get_artifact(session_id, upload_id, active_session_id=active_session_id)
        return self._upload_payload(payload)

    def _upload_preview_max_lines(self) -> int:
        return max(1, int(getattr(self.settings, "upload_preview_max_lines", 100) or 100))

    def _upload_preview_lines(self, payload: dict[str, Any]) -> list[str]:
        upload_payload = dict(payload.get("payload") or {})
        technical_block = dict(upload_payload.get("technical") or {})
        path_token = str(technical_block.get("path") or upload_payload.get("data_path") or "").strip()
        if path_token:
            resolved_path = Path(path_token).expanduser()
            if resolved_path.exists():
                return self.store._read_upload_preview(
                    resolved_path,
                    max_lines=self._upload_preview_max_lines(),
                )
        stored_lines = upload_payload.get("preview_lines")
        if isinstance(stored_lines, list):
            return [str(line).rstrip("\r\n") for line in stored_lines[: self._upload_preview_max_lines()]]
        return []

    def delete_artifacts(self, session_id: str, artifact_ids: list[str]) -> dict[str, Any]:
        deleted = self.store.delete_artifacts(session_id, artifact_ids)
        return {
            "session_id": session_id,
            "deleted_count": len(deleted),
            "deleted_artifact_ids": deleted,
        }

    def delete_uploads(self, session_id: str, upload_ids: list[str]) -> dict[str, Any]:
        deleted = self.store.delete_artifacts(session_id, upload_ids)
        return {
            "session_id": session_id,
            "deleted_count": len(deleted),
            "deleted_upload_ids": deleted,
        }

    def create_figure(
        self,
        session_id: str,
        artifact_id: str,
        *,
        figure_type: str,
        title: str = "",
        top_n: int = 10,
    ) -> dict[str, Any]:
        source_artifact = self.store.get_artifact(session_id, artifact_id)
        source_artifact_id = artifact_id
        if source_artifact.kind == "figure":
            upstream_artifact_id = clean_text((source_artifact.provenance or {}).get("source_artifact_id"))
            if upstream_artifact_id:
                source_artifact = self.store.get_artifact(session_id, upstream_artifact_id)
                source_artifact_id = upstream_artifact_id
        figure_payload = self.figure_catalog.create_figure(
            source_artifact,
            output_dir=self.store._figure_dir(session_id),
            figure_type=figure_type,
            title=title,
            top_n=top_n,
        )
        created = self.store.create_artifact(
            session_id,
            kind="figure",
            title=figure_payload["title"],
            summary=figure_payload["caption"],
            provider="gleskit_figures",
            payload=figure_payload,
            provenance={
                "source_artifact_id": source_artifact_id,
                "source_artifact_kind": source_artifact.kind,
                "figure_type": figure_payload["figure_type"],
            },
        )
        return self.get_artifact(session_id, created.artifact_id)

    def _resolve_artifact_session_id(
        self,
        artifact_id: str,
        *,
        session_id: str | None = None,
        active_session_id: str | None = None,
    ) -> str:
        cleaned_artifact_id = str(artifact_id or "").strip()
        cleaned_session_id = str(session_id or "").strip()
        cleaned_active_session_id = str(active_session_id or "").strip()

        if not cleaned_artifact_id:
            raise ValueError(
                "artifact_id must not be empty. Call get_artifact like "
                "get_artifact(session_id='session_x', artifact_id='artifact_y')."
            )

        if cleaned_session_id:
            return cleaned_session_id

        if cleaned_active_session_id:
            try:
                self.store.get_artifact(cleaned_active_session_id, cleaned_artifact_id)
                return cleaned_active_session_id
            except FileNotFoundError:
                pass

        matches = self.store.find_artifact_sessions(cleaned_artifact_id)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                "Artifact lookup is ambiguous because this artifact_id appears in multiple sessions. "
                "Call get_artifact with both session_id and artifact_id."
            )
        raise FileNotFoundError(
            f"Could not resolve artifact_id '{cleaned_artifact_id}'. "
            "Call get_artifact like get_artifact(session_id='session_x', artifact_id='artifact_y')."
        )

    def archive_session(self, session_id: str) -> dict[str, Any]:
        session = self.store.archive_session(session_id)
        return {
            "session_id": session.session_id,
            "status": session.status,
            "updated_at": session.updated_at,
        }

    def plan_delete_sessions(self, session_ids: list[str]) -> dict[str, Any]:
        sessions = []
        for session_id in session_ids:
            try:
                session = self.store.get_session(session_id)
            except FileNotFoundError:
                continue
            sessions.append(self._session_preview(session))
        return {"session_count": len(sessions), "sessions": sessions}

    def delete_sessions(self, session_ids: list[str]) -> dict[str, Any]:
        deleted = self.store.delete_sessions(session_ids)
        return {"deleted_count": len(deleted), "deleted_session_ids": deleted}

    def capture_artifact(
        self,
        session_id: str,
        *,
        kind: str,
        title: str,
        summary: str,
        payload: dict[str, Any],
        provider: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> Artifact:
        return self.store.create_artifact(
            session_id,
            kind=kind,
            title=title,
            summary=summary,
            provider=provider,
            payload=payload,
            provenance=provenance,
        )

    def create_upload(
        self,
        session_id: str,
        *,
        data_path: str,
        source_mode: str,
        title: str = "",
        source_path: str = "",
        stored_path: str = "",
        file_name: str = "",
        mime_type: str = "",
        provider: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        preview_max_lines = self._upload_preview_max_lines()
        created = self.store.create_upload(
            session_id,
            data_path=data_path,
            source_mode=source_mode,
            title=title,
            source_path=source_path,
            stored_path=stored_path,
            file_name=file_name,
            mime_type=mime_type,
            provider=provider,
            provenance=provenance,
            preview_max_lines=preview_max_lines,
        )
        return self.get_upload(session_id, created.artifact_id)

    def create_session_from_artifacts(
        self,
        *,
        title: str,
        description: str,
        sources: list[dict[str, str]],
        species: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self.store.create_session(title=title, description=description, species=species, tags=tags)
        copied = self.store.copy_artifacts(
            session.session_id,
            [
                (str(source["session_id"]).strip(), str(source["artifact_id"]).strip())
                for source in sources
                if str(source.get("session_id") or "").strip() and str(source.get("artifact_id") or "").strip()
            ],
        )
        return {
            **self._session_preview(session),
            "copied_artifact_count": len(copied),
            "copied_artifact_ids": [artifact.artifact_id for artifact in copied],
        }

    def create_report(
        self,
        session_id: str,
        *,
        body_markdown: str,
        template_id: str,
        title: str = "",
        author: str = "assistant",
        focus: str = "",
        custom_guidance: str = "",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        normalized_body = body_markdown.strip()
        if not normalized_body:
            raise ValueError("body_markdown is required")
        normalized_template_id = template_id.strip()
        if not normalized_template_id:
            raise ValueError(
                "template_id is required for new reports. Ask the user to choose a report type first, "
                "or use template_id 'custom' only when the user explicitly chose or delegated a custom structure."
            )
        effective_custom_guidance = custom_guidance.strip()
        template = get_report_template(normalized_template_id)
        if not template.custom_guidance_allowed:
            effective_custom_guidance = ""
        if template.custom_guidance_required and not effective_custom_guidance:
            raise ValueError(f"custom_guidance is required for template_id '{normalized_template_id}'")
        report_title = _derive_report_title(
            title=title,
            body_markdown=normalized_body,
            fallback=f"{session.title} report {len(session.reports) + 1}",
        )
        normalized_body = self._apply_auto_evidence_links(
            session_id,
            normalized_body,
            linked_artifact_ids=linked_artifact_ids or [],
        )
        report = self.store.create_report(
            session_id,
            title=report_title,
            body_markdown=normalized_body,
            author=author,
            focus=focus,
            template_id=normalized_template_id,
            custom_guidance=effective_custom_guidance,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        payload = self._report_payload(session_id, report)
        payload["markdown"] = report.body_markdown
        return payload

    def update_report(
        self,
        session_id: str,
        report_id: str,
        *,
        title: str | None = None,
        body_markdown: str,
        author: str | None = None,
        focus: str | None = None,
        template_id: str | None = None,
        custom_guidance: str | None = None,
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_body = body_markdown.strip()
        if not normalized_body:
            raise ValueError("body_markdown is required")
        existing = self.store.get_report(session_id, report_id)
        normalized_template_id = template_id.strip() if template_id is not None else None
        effective_template_id = existing.template_id if normalized_template_id is None else normalized_template_id
        effective_custom_guidance = existing.custom_guidance if custom_guidance is None else custom_guidance.strip()
        if effective_template_id:
            template = get_report_template(effective_template_id)
            if not template.custom_guidance_allowed:
                effective_custom_guidance = ""
            if template.custom_guidance_required and not effective_custom_guidance:
                raise ValueError(f"custom_guidance is required for template_id '{effective_template_id}'")
        else:
            effective_custom_guidance = ""
        effective_linked_artifact_ids = (
            linked_artifact_ids if linked_artifact_ids is not None else existing.linked_artifact_ids
        )
        normalized_body = self._apply_auto_evidence_links(
            session_id,
            normalized_body,
            linked_artifact_ids=effective_linked_artifact_ids,
        )
        report = self.store.update_report(
            session_id,
            report_id,
            title=title,
            body_markdown=normalized_body,
            author=author,
            focus=focus,
            template_id=effective_template_id,
            custom_guidance=effective_custom_guidance,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        return self._report_payload(session_id, report)

    def delete_report(self, session_id: str, report_id: str) -> dict[str, Any]:
        deleted_report_id = self.store.delete_report(session_id, report_id)
        return {"session_id": session_id, "deleted_report_id": deleted_report_id}

    def get_session_report(self, session_id: str, report_id: str | None = None) -> dict[str, Any]:
        reports = self.store.list_reports(session_id)
        if not reports:
            return {
                "session_id": session_id,
                "exists": False,
                "report_count": 0,
                "report_id": None,
                "path": "",
                "body_markdown": "",
                "markdown": "",
                "template_id": "",
                "custom_guidance": "",
            }
        selected = reports[0] if not report_id else self.store.get_report(session_id, report_id)
        payload = {
            **self._report_payload(session_id, selected),
            "exists": True,
            "report_count": len(reports),
            "latest_report_id": reports[0].report_id,
        }
        payload["markdown"] = selected.body_markdown
        return payload

    def get_report_markdown(self, session_id: str, report_id: str | None = None) -> str:
        return self.store.read_report(session_id, report_id)

    @staticmethod
    def _session_preview(session) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "title": session.title,
            "description": session.description,
            "species": session.species,
            "status": session.status,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "artifact_count": len(session.artifact_ids),
            "note_count": len(session.notes),
            "report_count": len(session.reports),
            "tags": session.tags,
        }

    @staticmethod
    def _artifact_preview(artifact: Artifact) -> dict[str, Any]:
        payload = artifact.payload or {}
        return {
            "artifact_id": artifact.artifact_id,
            "kind": artifact.kind,
            "title": artifact.title,
            "summary": artifact.summary,
            "created_at": artifact.created_at,
            "provider": artifact.provider,
            "source_mode": payload.get("source_mode", ""),
            "data_path": payload.get("data_path", ""),
            "source_path": payload.get("source_path", ""),
            "stored_path": payload.get("stored_path", ""),
            "file_name": payload.get("file_name", ""),
            "file_size": payload.get("file_size", 0),
            "mime_type": payload.get("mime_type", ""),
        }

    @staticmethod
    def _upload_preview(artifact: Artifact) -> dict[str, Any]:
        preview = WorkspaceService._artifact_preview(artifact)
        payload = dict(artifact.payload or {})
        summary_block = dict(payload.get("summary") or {})
        technical_block = dict(payload.get("technical") or {})
        inspection_block = dict(payload.get("inspection") or {})
        validation_block = dict(payload.get("validation") or inspection_block.get("validation") or {})
        upload_id = preview.pop("artifact_id")
        preview["upload_id"] = upload_id
        if (
            (not summary_block or not summary_block.get("data_type") or not summary_block.get("sample_count") or not summary_block.get("gene_count"))
            and str(technical_block.get("path") or payload.get("data_path") or "").strip()
        ):
            resolved_path = Path(str(technical_block.get("path") or payload.get("data_path") or "")).expanduser()
            if resolved_path.exists():
                provenance = dict(technical_block.get("provenance") or artifact.provenance or {})
                inspection = inspect_tabular_file(
                    resolved_path,
                    role_hint=str(provenance.get("cohort_role") or payload.get("role") or ""),
                    provenance=provenance,
                )
                summary_block = dict(inspection.summary)
                technical_block = {**technical_block, **dict(inspection.technical)}
                inspection_block = dict(inspection.inspection)
                validation_block = {
                    "status": inspection.summary.get("validation_status", "ok"),
                    "warnings": list(inspection.warnings),
                }
                payload = {
                    **payload,
                    "data_type": inspection.data_type,
                    "role": inspection.role,
                    "summary": dict(inspection.summary),
                    "technical": dict(inspection.technical),
                    "inspection": dict(inspection.inspection),
                    "validation": dict(validation_block),
                    "sample_count": int(inspection.summary.get("sample_count", 0)),
                    "gene_count": int(inspection.summary.get("gene_count", 0)),
                    "has_metadata": bool(inspection.summary.get("has_metadata", False)),
                    "validation_status": str(inspection.summary.get("validation_status", "unknown")),
                    "one_line_summary": str(inspection.summary.get("one_line_summary", "")),
                    "next_step_hints": list(inspection.inspection.get("next_step_hints", [])),
                    "warnings": list(inspection.warnings),
                }
        preview["summary_block"] = summary_block or {
            "data_type": payload.get("data_type", ""),
            "role": payload.get("role", "upload"),
            "sample_count": int(payload.get("sample_count", 0) or 0),
            "gene_count": int(payload.get("gene_count", 0) or 0),
            "has_metadata": bool(payload.get("has_metadata", False)),
            "validation_status": payload.get("validation_status", "unknown"),
            "one_line_summary": artifact.summary,
        }
        technical_block = {
            **technical_block,
            "artifact_id": upload_id,
            "upload_id": upload_id,
            "path": technical_block.get("path") or payload.get("data_path", ""),
            "file_name": technical_block.get("file_name") or payload.get("file_name", ""),
            "mime_type": technical_block.get("mime_type") or payload.get("mime_type", ""),
            "source_mode": technical_block.get("source_mode") or payload.get("source_mode", ""),
            "provenance": technical_block.get("provenance") or artifact.provenance or {},
        }
        preview["technical"] = technical_block
        preview["inspection"] = inspection_block
        preview["validation"] = validation_block or {
            "status": payload.get("validation_status", "unknown"),
            "warnings": list(payload.get("warnings") or []),
        }
        preview["data_type"] = preview["summary_block"].get("data_type", "")
        preview["role"] = preview["summary_block"].get("role", "")
        preview["sample_count"] = int(preview["summary_block"].get("sample_count", 0) or 0)
        preview["gene_count"] = int(preview["summary_block"].get("gene_count", 0) or 0)
        preview["has_metadata"] = bool(preview["summary_block"].get("has_metadata", False))
        preview["validation_status"] = str(preview["summary_block"].get("validation_status", "unknown"))
        preview["one_line_summary"] = str(preview["summary_block"].get("one_line_summary", preview.get("summary", "")))
        preview["next_step_hints"] = list(inspection_block.get("next_step_hints") or payload.get("next_step_hints") or [])
        preview["warnings"] = list(validation_block.get("warnings") or payload.get("warnings") or [])
        return preview

    def _upload_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        next_payload = dict(payload)
        artifact_id = str(next_payload.pop("artifact_id", "")).strip()
        if artifact_id:
            next_payload["upload_id"] = artifact_id
        upload_payload = dict(next_payload.get("payload") or {})
        summary_block = dict(upload_payload.get("summary") or {})
        technical_block = dict(upload_payload.get("technical") or {})
        inspection_block = dict(upload_payload.get("inspection") or {})
        validation_block = dict(upload_payload.get("validation") or inspection_block.get("validation") or {})
        if artifact_id:
            technical_block.setdefault("artifact_id", artifact_id)
            technical_block.setdefault("upload_id", artifact_id)
        if upload_payload:
            technical_block.setdefault("path", upload_payload.get("data_path", ""))
            technical_block.setdefault("file_name", upload_payload.get("file_name", ""))
            technical_block.setdefault("mime_type", upload_payload.get("mime_type", ""))
            technical_block.setdefault("source_mode", upload_payload.get("source_mode", ""))
            technical_block.setdefault("provenance", next_payload.get("provenance") or {})
        preview_lines = self._upload_preview_lines(next_payload)
        upload_payload = {
            **upload_payload,
            "preview_lines": preview_lines,
            "preview_excerpt": self.store._preview_excerpt(preview_lines, limit=220) if preview_lines else "",
        }
        if (
            (not summary_block or not summary_block.get("data_type") or not summary_block.get("sample_count") or not summary_block.get("gene_count"))
            and str(technical_block.get("path") or upload_payload.get("data_path") or "").strip()
        ):
            resolved_path = Path(str(technical_block.get("path") or upload_payload.get("data_path") or "")).expanduser()
            if resolved_path.exists():
                provenance = dict(technical_block.get("provenance") or next_payload.get("provenance") or {})
                inspection = inspect_tabular_file(
                    resolved_path,
                    role_hint=str(provenance.get("cohort_role") or upload_payload.get("role") or ""),
                    provenance=provenance,
                )
                summary_block = dict(inspection.summary)
                technical_block = {**technical_block, **dict(inspection.technical)}
                inspection_block = dict(inspection.inspection)
                validation_block = {
                    "status": inspection.summary.get("validation_status", "ok"),
                    "warnings": list(inspection.warnings),
                }
                upload_payload = {
                    **upload_payload,
                    "data_type": inspection.data_type,
                    "role": inspection.role,
                    "summary": dict(inspection.summary),
                    "technical": dict(inspection.technical),
                    "inspection": dict(inspection.inspection),
                    "validation": dict(validation_block),
                    "sample_count": int(inspection.summary.get("sample_count", 0)),
                    "gene_count": int(inspection.summary.get("gene_count", 0)),
                    "has_metadata": bool(inspection.summary.get("has_metadata", False)),
                    "validation_status": str(inspection.summary.get("validation_status", "unknown")),
                    "one_line_summary": str(inspection.summary.get("one_line_summary", "")),
                    "next_step_hints": list(inspection.inspection.get("next_step_hints", [])),
                    "warnings": list(inspection.warnings),
                }
        next_payload["summary_block"] = summary_block or {
            "data_type": upload_payload.get("data_type", ""),
            "role": upload_payload.get("role", "upload"),
            "sample_count": int(upload_payload.get("sample_count", 0) or 0),
            "gene_count": int(upload_payload.get("gene_count", 0) or 0),
            "has_metadata": bool(upload_payload.get("has_metadata", False)),
            "validation_status": upload_payload.get("validation_status", "unknown"),
            "one_line_summary": upload_payload.get("one_line_summary", next_payload.get("summary", "")),
        }
        next_payload["technical"] = technical_block
        next_payload["inspection"] = inspection_block
        next_payload["validation"] = validation_block or {
            "status": upload_payload.get("validation_status", "unknown"),
            "warnings": list(upload_payload.get("warnings") or []),
        }
        next_payload["data_type"] = next_payload["summary_block"].get("data_type", "")
        next_payload["role"] = next_payload["summary_block"].get("role", "")
        next_payload["sample_count"] = int(next_payload["summary_block"].get("sample_count", 0) or 0)
        next_payload["gene_count"] = int(next_payload["summary_block"].get("gene_count", 0) or 0)
        next_payload["has_metadata"] = bool(next_payload["summary_block"].get("has_metadata", False))
        next_payload["validation_status"] = str(next_payload["summary_block"].get("validation_status", "unknown"))
        next_payload["one_line_summary"] = str(next_payload["summary_block"].get("one_line_summary", next_payload.get("summary", "")))
        next_payload["next_step_hints"] = list(inspection_block.get("next_step_hints") or upload_payload.get("next_step_hints") or [])
        next_payload["warnings"] = list(validation_block.get("warnings") or upload_payload.get("warnings") or [])
        return next_payload

    def _note_preview(self, session_id: str, note) -> dict[str, Any]:
        return {
            "note_id": note.note_id,
            "session_id": session_id,
            "title": note.title,
            "excerpt": _note_excerpt(note.body_markdown),
            "created_at": note.created_at,
            "updated_at": note.updated_at,
            "author": note.author,
            "linked_artifact_ids": note.linked_artifact_ids,
            "tags": note.tags,
            "path": str(self.store._note_path(session_id, note.note_id)),
        }

    def _note_payload(self, session_id: str, note) -> dict[str, Any]:
        return {
            **self._note_preview(session_id, note),
            "body_markdown": note.body_markdown,
        }

    _AUTO_EVIDENCE_START = "<!-- gleskit:auto-evidence-links:start -->"
    _AUTO_EVIDENCE_END = "<!-- gleskit:auto-evidence-links:end -->"

    @staticmethod
    def _trim_inline_text(value: str, *, limit: int = 140) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _markdown_escape(value: str) -> str:
        text = str(value or "")
        return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")

    @staticmethod
    def _provider_label(value: str | None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text == "europe_pmc":
            return "Europe PMC"
        if text == "string":
            return "STRING"
        if text == "gleskit_aggregate":
            return "GLESKit aggregate"
        return text.replace("_", " ")

    def _strip_auto_evidence_links(self, body_markdown: str) -> str:
        text = str(body_markdown or "")
        start = text.find(self._AUTO_EVIDENCE_START)
        end = text.find(self._AUTO_EVIDENCE_END)
        if start == -1 or end == -1 or end < start:
            return text.strip()
        stripped = (text[:start] + text[end + len(self._AUTO_EVIDENCE_END) :]).strip()
        return stripped

    def _session_paper_registry(self, session_id: str) -> dict[str, dict[str, dict[str, str]]]:
        by_url: dict[str, dict[str, str]] = {}
        by_pmid: dict[str, dict[str, str]] = {}
        by_doi: dict[str, dict[str, str]] = {}
        for artifact in self.store.list_artifacts(session_id):
            if str(artifact.kind or "").strip().lower() != "paper_search":
                continue
            papers = artifact.payload.get("papers") if isinstance(artifact.payload, dict) else []
            if not isinstance(papers, list):
                continue
            for paper in papers:
                if not isinstance(paper, dict):
                    continue
                entry = {
                    "artifact_id": str(artifact.artifact_id).strip(),
                    "artifact_title": str(artifact.title or "").strip(),
                    "title": str(paper.get("title") or "").strip(),
                    "url": _normalize_url(paper.get("url")),
                    "pmid": str(paper.get("pmid") or paper.get("id") or "").strip(),
                    "doi": str(paper.get("doi") or "").strip().lower(),
                    "first_author_surname": str(paper.get("first_author_surname") or "").strip(),
                    "year": str(paper.get("year") or "").strip(),
                }
                if entry["url"]:
                    by_url[entry["url"]] = entry
                if entry["pmid"]:
                    by_pmid[entry["pmid"]] = entry
                if entry["doi"]:
                    by_doi[entry["doi"]] = entry
        return {"by_url": by_url, "by_pmid": by_pmid, "by_doi": by_doi}

    def _report_citation_audit(
        self,
        session_id: str,
        body_markdown: str,
        *,
        linked_artifact_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        authored_body = self._strip_auto_evidence_links(body_markdown)
        linked_set = {
            clean_text(artifact_id)
            for artifact_id in (linked_artifact_ids or [])
            if clean_text(artifact_id)
        }
        registry = self._session_paper_registry(session_id)
        issues: list[dict[str, Any]] = []
        paper_checks: list[dict[str, Any]] = []
        source_lines: list[tuple[int, str]] = []
        for line_number, raw_line in enumerate(authored_body.splitlines(), start=1):
            stripped = raw_line.strip()
            if stripped.startswith("Sources:"):
                source_lines.append((line_number, stripped))
        for line_number, line_text in source_lines:
            segments = [segment.strip() for segment in line_text[len("Sources:") :].split(";") if segment.strip()]
            for segment in segments:
                match = MARKDOWN_LINK_RE.search(segment)
                if match is None:
                    continue
                url = _normalize_url(match.group("url"))
                if not url:
                    continue
                pmid, doi = _extract_paper_locator(url, segment)
                paper_entry = (
                    registry["by_url"].get(url)
                    or (registry["by_pmid"].get(pmid) if pmid else None)
                    or (registry["by_doi"].get(doi) if doi else None)
                )
                if paper_entry is None and (
                    pmid is not None
                    or doi is not None
                    or "europepmc.org" in url.lower()
                    or "pubmed" in url.lower()
                    or "doi.org" in url.lower()
                ):
                    issues.append(
                        {
                            "code": "citation_identity_mismatch",
                            "severity": "major",
                            "line_number": line_number,
                            "url": url,
                            "detail": "The report cites a paper-like link that does not resolve to any saved paper-search artifact entry in this session.",
                        }
                    )
                    continue
                if paper_entry is None:
                    continue
                observed_title = _extract_visible_paper_title(segment)
                title_matches = observed_title is not None and _titles_match(observed_title, paper_entry["title"])
                author_year_matches = _author_year_link_matches(segment, paper_entry)
                suggested_exact_link = f"[{self._markdown_escape(str(paper_entry.get('title') or 'paper'))}]({url})"
                paper_checks.append(
                    {
                        "line_number": line_number,
                        "url": url,
                        "artifact_id": paper_entry.get("artifact_id"),
                        "observed_title": observed_title,
                        "expected_title": paper_entry.get("title"),
                        "matched": bool(title_matches or author_year_matches or observed_title is None),
                        "citation_style": "author_year" if author_year_matches else "title" if title_matches else "unknown",
                        "suggested_exact_link": suggested_exact_link,
                    }
                )
                if observed_title is None:
                    issues.append(
                        {
                            "code": "citation_title_missing",
                            "severity": "minor",
                            "line_number": line_number,
                            "url": url,
                            "expected_title": paper_entry.get("title"),
                            "detail": "The paper link is present, but the visible citation does not expose the exact paper title.",
                            "suggested_exact_link": suggested_exact_link,
                            "artifact_id": paper_entry.get("artifact_id"),
                        }
                    )
                elif not title_matches and not author_year_matches:
                    issues.append(
                        {
                            "code": "citation_title_mismatch",
                            "severity": "major",
                            "line_number": line_number,
                            "url": url,
                            "observed_title": observed_title,
                            "expected_title": paper_entry.get("title"),
                            "detail": "The visible paper title does not match the saved paper metadata for the cited link.",
                            "suggested_exact_link": suggested_exact_link,
                            "artifact_id": paper_entry.get("artifact_id"),
                        }
                    )
                if linked_set and paper_entry.get("artifact_id") and paper_entry["artifact_id"] not in linked_set:
                    issues.append(
                        {
                            "code": "artifact_cited_not_linked",
                            "severity": "minor",
                            "line_number": line_number,
                            "url": url,
                            "artifact_id": paper_entry["artifact_id"],
                            "detail": "The paper comes from a saved artifact in this session, but that artifact is not included in linked_artifact_ids.",
                        }
                    )
        issue_codes = sorted({str(item.get("code") or "").strip() for item in issues if str(item.get("code") or "").strip()})
        major_issue_count = sum(1 for item in issues if str(item.get("severity") or "").strip().lower() == "major")
        return {
            "status": "ok",
            "source_line_count": len(source_lines),
            "issue_count": len(issues),
            "major_issue_count": major_issue_count,
            "issue_codes": issue_codes,
            "issues": issues[:20],
            "paper_checks": paper_checks[:20],
        }

    def _linked_artifact_entries(self, session_id: str, artifact_ids: list[str] | None = None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        report_dir = self.store._report_dir(session_id)
        for raw_artifact_id in artifact_ids or []:
            artifact_id = clean_text(raw_artifact_id)
            if not artifact_id:
                continue
            try:
                artifact = self.store.get_artifact(session_id, artifact_id)
            except FileNotFoundError:
                continue
            artifact_path = self.store._artifact_path(session_id, artifact_id)
            artifact_rel_path = os.path.relpath(artifact_path, report_dir)
            payload = dict(artifact.payload or {})
            paper_links: list[dict[str, str]] = []
            for paper in list(payload.get("papers") or [])[:2]:
                if not isinstance(paper, dict):
                    continue
                title = clean_text(paper.get("title"))
                url = clean_text(paper.get("url"))
                if title and url:
                    paper_links.append({"title": title, "url": url})
            file_links: list[dict[str, str]] = []
            for key, label in (("png_path", "PNG"), ("svg_path", "SVG")):
                raw_path = clean_text(payload.get(key))
                if not raw_path:
                    continue
                candidate = Path(raw_path).expanduser()
                if not candidate.exists():
                    continue
                file_links.append(
                    {
                        "label": label,
                        "path": str(candidate),
                        "relative_report_path": os.path.relpath(candidate, report_dir),
                    }
                )
            entries.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "kind": artifact.kind,
                    "title": artifact.title,
                    "summary": artifact.summary,
                    "provider": artifact.provider,
                    "path": str(artifact_path),
                    "relative_report_path": artifact_rel_path,
                    "paper_links": paper_links,
                    "file_links": file_links,
                }
            )
        return entries

    def _build_auto_evidence_links(self, session_id: str, artifact_ids: list[str] | None = None) -> str:
        entries = self._linked_artifact_entries(session_id, artifact_ids)
        if not entries:
            return ""
        lines = [
            self._AUTO_EVIDENCE_START,
            "## Evidence links",
            "These saved GLESKit evidence artifacts support the report and can be opened directly for audit.",
            "",
        ]
        for entry in entries:
            label = self._markdown_escape(entry.get("title") or entry.get("artifact_id") or "Evidence artifact")
            artifact_link = f"[{label}]({entry['relative_report_path']})"
            details: list[str] = []
            provider_label = self._provider_label(entry.get("provider"))
            if provider_label:
                details.append(provider_label)
            summary = self._trim_inline_text(str(entry.get("summary") or ""))
            if summary:
                details.append(summary)
            paper_bits = []
            for paper in entry.get("paper_links") or []:
                paper_bits.append(
                    f"[{self._markdown_escape(str(paper.get('title') or 'paper'))}]({str(paper.get('url') or '').strip()})"
                )
            if paper_bits:
                details.append("representative papers: " + "; ".join(paper_bits))
            file_bits = []
            for file_link in entry.get("file_links") or []:
                file_bits.append(
                    f"[{self._markdown_escape(str(file_link.get('label') or 'file'))}]({str(file_link.get('relative_report_path') or '').strip()})"
                )
            if file_bits:
                details.append("files: " + ", ".join(file_bits))
            bullet = f"- {artifact_link}"
            if details:
                bullet += " — " + "; ".join(details)
            lines.append(bullet)
        lines.extend(["", self._AUTO_EVIDENCE_END])
        return "\n".join(lines).strip()

    def _apply_auto_evidence_links(
        self,
        session_id: str,
        body_markdown: str,
        *,
        linked_artifact_ids: list[str] | None = None,
    ) -> str:
        base = self._strip_auto_evidence_links(body_markdown)
        appendix = self._build_auto_evidence_links(session_id, linked_artifact_ids)
        if not appendix:
            return base
        if not base:
            return appendix
        return f"{base}\n\n{appendix}".strip()

    def _report_preview(self, session_id: str, report) -> dict[str, Any]:
        path = self.store._report_path(session_id, report.report_id)
        legacy_path = self.store.reports_root / session_id / "report.md"
        if report.report_id == "report_legacy" and legacy_path.exists():
            path = legacy_path
        return {
            "report_id": report.report_id,
            "session_id": session_id,
            "title": report.title,
            "excerpt": _note_excerpt(report.body_markdown),
            "created_at": report.created_at,
            "updated_at": report.updated_at,
            "author": report.author,
            "focus": report.focus,
            "template_id": report.template_id,
            "linked_artifact_ids": report.linked_artifact_ids,
            "linked_artifacts": self._linked_artifact_entries(session_id, report.linked_artifact_ids),
            "tags": report.tags,
            "path": str(path),
        }

    def _report_payload(self, session_id: str, report) -> dict[str, Any]:
        citation_audit = self._report_citation_audit(
            session_id,
            report.body_markdown,
            linked_artifact_ids=report.linked_artifact_ids,
        )
        return {
            **self._report_preview(session_id, report),
            "body_markdown": report.body_markdown,
            "custom_guidance": report.custom_guidance,
            "citation_audit": citation_audit,
        }

    def _cohort_preview(self, cohort: Cohort) -> dict[str, Any]:
        profile = cohort.profile or {}
        validation = cohort.validation or {}
        inspection = cohort.inspection or {}
        summary_block = {
            "data_type": "cohort",
            "role": cohort.source_kind or "cohort",
            "sample_count": int(profile.get("sample_count") or 0),
            "gene_count": int(profile.get("filtered_gene_count") or profile.get("row_count") or 0),
            "raw_gene_count": int(profile.get("row_count") or 0),
            "filtered_gene_count": int(profile.get("filtered_gene_count") or 0),
            "has_metadata": True,
            "validation_status": "warning" if validation.get("warnings") else cohort.status,
            "one_line_summary": cohort.summary,
        }
        technical_block = {
            "cohort_id": cohort.cohort_id,
            "counts_path": cohort.counts_path,
            "metadata_path": cohort.metadata_path,
            "provenance": cohort.provenance,
            "created_at": cohort.created_at,
            "updated_at": cohort.updated_at,
        }
        return {
            "cohort_id": cohort.cohort_id,
            "title": cohort.title,
            "provider_id": cohort.provider_id,
            "source_kind": cohort.source_kind,
            "counts_path": cohort.counts_path,
            "metadata_path": cohort.metadata_path,
            "summary": cohort.summary,
            "created_at": cohort.created_at,
            "updated_at": cohort.updated_at,
            "species": cohort.species,
            "status": cohort.status,
            "profile": profile,
            "validation": validation,
            "summary_block": summary_block,
            "technical": technical_block,
            "inspection": inspection,
            "provenance": cohort.provenance,
            "tags": cohort.tags,
            "cohort_size": {
                "genes": int(profile.get("filtered_gene_count") or profile.get("row_count") or 0),
                "samples": int(profile.get("sample_count") or 0),
            },
            "data_type": "cohort",
            "role": cohort.source_kind or "cohort",
            "sample_count": int(profile.get("sample_count") or 0),
            "gene_count": int(profile.get("filtered_gene_count") or profile.get("row_count") or 0),
            "raw_gene_count": int(profile.get("row_count") or 0),
            "filtered_gene_count": int(profile.get("filtered_gene_count") or 0),
            "has_metadata": True,
            "validation_status": summary_block["validation_status"],
            "one_line_summary": cohort.summary,
        }

    def _cohort_payload(self, cohort: Cohort) -> dict[str, Any]:
        payload = self._cohort_preview(cohort)
        payload["technical"] = {
            **payload.get("technical", {}),
            "cohort_id": cohort.cohort_id,
            "title": cohort.title,
            "provider_id": cohort.provider_id,
            "source_kind": cohort.source_kind,
        }
        return payload


def _is_fresh(updated_at: str, ttl_seconds: int) -> bool:
    try:
        seen = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    return age <= max(1, int(ttl_seconds))


def _note_excerpt(body_markdown: str, *, limit: int = 160) -> str:
    single_line = " ".join(str(body_markdown or "").split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"


def _derive_report_title(*, title: str, body_markdown: str, fallback: str) -> str:
    normalized = title.strip()
    if normalized:
        return normalized
    for line in str(body_markdown or "").splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:80]
    return fallback


__all__ = [
    "WorkspaceGateway",
    "WorkspaceService",
    "WorkspaceUnavailableError",
    "clear_runtime_manifest",
    "write_runtime_manifest",
]
