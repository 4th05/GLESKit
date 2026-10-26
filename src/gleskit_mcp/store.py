from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil
from typing import Any, Iterable

from gleskit_mcp.models import AnalysisSession, Artifact, Cohort, SessionNote, SessionReport, new_id, utc_now_iso
from gleskit_mcp.preview import read_preview_lines


def inspect_tabular_file(*args, **kwargs):
    from gleskit_mcp.cohort_library import inspect_tabular_file as inspect
    return inspect(*args, **kwargs)


def prepare_cohort_inputs(*args, **kwargs):
    from gleskit_mcp.cohort_library import prepare_cohort_inputs as prepare
    return prepare(*args, **kwargs)


def prepare_cohort_inputs_from_frames(*args, **kwargs):
    from gleskit_mcp.cohort_library import prepare_cohort_inputs_from_frames as prepare
    return prepare(*args, **kwargs)


class WorkspaceStore:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.sessions_root = self.workspace_root / "sessions"
        self.reports_root = self.workspace_root / "reports"
        self.cohorts_root = self.workspace_root / "cohorts"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.reports_root.mkdir(parents=True, exist_ok=True)
        self.cohorts_root.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        *,
        title: str,
        description: str,
        species: str,
        tags: list[str] | None = None,
    ) -> AnalysisSession:
        now = utc_now_iso()
        session = AnalysisSession(
            session_id=new_id("session"),
            title=title.strip(),
            description=description.strip(),
            species=species.strip(),
            status="active",
            created_at=now,
            updated_at=now,
            notes=[],
            reports=[],
            artifact_ids=[],
            tags=[tag.strip() for tag in (tags or []) if str(tag).strip()],
        )
        self._write_session(session)
        return session

    def get_session(self, session_id: str) -> AnalysisSession:
        path = self._session_metadata_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown session_id: {session_id}")
        data = json.loads(path.read_text())
        notes = [self._session_note_from_row(session_id, row) for row in data.get("notes", [])]
        reports = [self._session_report_from_row(session_id, row) for row in data.get("reports", [])]
        if not reports:
            legacy_report = self._legacy_report(session_id, data)
            if legacy_report is not None:
                reports = [legacy_report]
        return AnalysisSession(
            session_id=data["session_id"],
            title=data.get("title", ""),
            description=data.get("description") or data.get("question") or "",
            species=data.get("species", "human"),
            status=data.get("status", "active"),
            created_at=data["created_at"],
            updated_at=data.get("updated_at", data["created_at"]),
            notes=notes,
            reports=reports,
            artifact_ids=list(data.get("artifact_ids", [])),
            tags=list(data.get("tags", [])),
        )

    def list_sessions(self, *, status: str | None = None) -> list[AnalysisSession]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {"", "all", "any"}:
            normalized_status = ""
        sessions: list[AnalysisSession] = []
        for path in sorted(self.sessions_root.glob("*/session.json")):
            session = self.get_session(path.parent.name)
            if normalized_status and str(session.status or "").strip().lower() != normalized_status:
                continue
            sessions.append(session)
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

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
    ) -> Cohort:
        counts_file = Path(counts_path).expanduser().resolve()
        metadata_file = Path(metadata_path).expanduser().resolve()
        if not counts_file.exists():
            raise FileNotFoundError(f"Counts file not found: {counts_file}")
        if not metadata_file.exists():
            raise FileNotFoundError(f"Metadata file not found: {metadata_file}")
        cohort_id = new_id("cohort")
        cohort_dir = self._cohort_dir(cohort_id)
        cohort_dir.mkdir(parents=True, exist_ok=True)
        counts_target = cohort_dir / "counts.tsv"
        metadata_target = cohort_dir / "metadata.tsv"
        try:
            prepared = prepare_cohort_inputs(
                counts_path=counts_file,
                metadata_path=metadata_file,
                transpose=transpose,
                sample_column=sample_column,
                group_column=group_column,
                min_count=min_count,
                sample_limit=sample_limit,
            )
            prepared.counts.to_csv(counts_target, sep="\t", index_label="gene")
            prepared.metadata.to_csv(metadata_target, sep="\t", index=False)
            counts_inspection = inspect_tabular_file(
                counts_target,
                role_hint="counts",
                provenance={"cohort_id": cohort_id, "cohort_role": "counts", "cohort_title": title.strip() or cohort_id},
            )
            metadata_inspection = inspect_tabular_file(
                metadata_target,
                role_hint="metadata",
                provenance={"cohort_id": cohort_id, "cohort_role": "metadata", "cohort_title": title.strip() or cohort_id},
            )

            now = utc_now_iso()
            summary_parts = [
                title.strip() or cohort_id,
                f"{prepared.summary['filtered_gene_count']:,} genes",
                f"{prepared.summary['sample_count']:,} samples",
            ]
            if prepared.profile.get("recommended_group_column"):
                summary_parts.append(f"group: {prepared.profile.get('recommended_group_column')}")
            summary = " · ".join(part for part in summary_parts if part)
            provenance = {
                "source_kind": source_kind,
                "provider_id": provider_id,
                "counts_source_path": str(Path(source_counts_path).expanduser().resolve()) if source_counts_path else "",
                "metadata_source_path": str(Path(source_metadata_path).expanduser().resolve()) if source_metadata_path else "",
                "counts_source_url": source_counts_url.strip(),
                "metadata_source_url": source_metadata_url.strip(),
                "transpose": bool(transpose),
                "download_provider_id": download_provider_id.strip(),
                "download_dataset_id": download_dataset_id.strip(),
                "download_tissue": download_tissue.strip(),
                "download_meta": dict(download_meta or {}),
            }
            manifest = {
                "cohort_id": cohort_id,
                "title": title.strip() or cohort_id,
                "provider_id": provider_id.strip() or "local_import",
                "source_kind": source_kind.strip() or "imported",
                "species": species.strip() or "human",
                "status": "ready",
                "created_at": now,
                "updated_at": now,
                "counts_path": str(counts_target),
                "metadata_path": str(metadata_target),
                "summary": summary,
                "profile": prepared.profile,
                "validation": {
                    **prepared.summary,
                    "warnings": prepared.warnings,
                },
                "inspection": {
                    "counts": asdict(counts_inspection),
                    "metadata": asdict(metadata_inspection),
                },
                "provenance": provenance,
                "tags": [tag.strip() for tag in (tags or []) if str(tag).strip()],
            }
            self._write_json(self._cohort_manifest_path(cohort_id), manifest)
            return self.get_cohort(cohort_id)
        except Exception:
            if cohort_dir.exists():
                shutil.rmtree(cohort_dir)
            raise

    def create_cohort_from_frames(
        self,
        *,
        title: str,
        counts_frame: Any,
        metadata_frame: Any,
        provider_id: str = "local_import",
        source_kind: str = "imported",
        species: str = "human",
        transpose: bool = False,
        sample_column: str = "",
        group_column: str = "",
        min_count: int = 10,
        sample_limit: int = 0,
        download_provider_id: str = "",
        download_dataset_id: str = "",
        download_tissue: str = "",
        download_meta: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> Cohort:
        cohort_id = new_id("cohort")
        cohort_dir = self._cohort_dir(cohort_id)
        cohort_dir.mkdir(parents=True, exist_ok=True)
        counts_target = cohort_dir / "counts.tsv"
        metadata_target = cohort_dir / "metadata.tsv"
        try:
            prepared = prepare_cohort_inputs_from_frames(
                counts_frame=counts_frame,
                metadata_frame=metadata_frame,
                transpose=transpose,
                sample_column=sample_column,
                group_column=group_column,
                min_count=min_count,
                sample_limit=sample_limit,
            )
            prepared.counts.to_csv(counts_target, sep="\t", index_label="gene")
            prepared.metadata.to_csv(metadata_target, sep="\t", index=False)
            counts_inspection = inspect_tabular_file(
                counts_target,
                role_hint="counts",
                provenance={"cohort_id": cohort_id, "cohort_role": "counts", "cohort_title": title.strip() or cohort_id},
            )
            metadata_inspection = inspect_tabular_file(
                metadata_target,
                role_hint="metadata",
                provenance={"cohort_id": cohort_id, "cohort_role": "metadata", "cohort_title": title.strip() or cohort_id},
            )

            now = utc_now_iso()
            summary_parts = [
                title.strip() or cohort_id,
                f"{prepared.summary['filtered_gene_count']:,} genes",
                f"{prepared.summary['sample_count']:,} samples",
            ]
            if prepared.profile.get("recommended_group_column"):
                summary_parts.append(f"group: {prepared.profile.get('recommended_group_column')}")
            summary = " · ".join(part for part in summary_parts if part)
            provenance = {
                "source_kind": source_kind,
                "provider_id": provider_id,
                "counts_source_path": "",
                "metadata_source_path": "",
                "counts_source_url": "",
                "metadata_source_url": "",
                "transpose": bool(transpose),
                "download_provider_id": download_provider_id.strip(),
                "download_dataset_id": download_dataset_id.strip(),
                "download_tissue": download_tissue.strip(),
                "download_meta": dict(download_meta or {}),
            }
            manifest = {
                "cohort_id": cohort_id,
                "title": title.strip() or cohort_id,
                "provider_id": provider_id.strip() or "local_import",
                "source_kind": source_kind.strip() or "imported",
                "species": species.strip() or "human",
                "status": "ready",
                "created_at": now,
                "updated_at": now,
                "counts_path": str(counts_target),
                "metadata_path": str(metadata_target),
                "summary": summary,
                "profile": prepared.profile,
                "validation": {
                    **prepared.summary,
                    "warnings": prepared.warnings,
                },
                "inspection": {
                    "counts": asdict(counts_inspection),
                    "metadata": asdict(metadata_inspection),
                },
                "provenance": provenance,
                "tags": [tag.strip() for tag in (tags or []) if str(tag).strip()],
            }
            self._write_json(self._cohort_manifest_path(cohort_id), manifest)
            return self.get_cohort(cohort_id)
        except Exception:
            if cohort_dir.exists():
                shutil.rmtree(cohort_dir)
            raise

    def list_cohorts(self) -> list[Cohort]:
        cohorts: list[Cohort] = []
        for path in sorted(self.cohorts_root.glob("*/manifest.json")):
            cohorts.append(self.get_cohort(path.parent.name))
        cohorts.sort(key=lambda item: item.updated_at, reverse=True)
        return cohorts

    def get_cohort(self, cohort_id: str) -> Cohort:
        path = self._cohort_manifest_path(cohort_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown cohort_id: {cohort_id}")
        data = json.loads(path.read_text())
        return Cohort(
            cohort_id=data["cohort_id"],
            title=data.get("title", ""),
            provider_id=data.get("provider_id", "local_import"),
            source_kind=data.get("source_kind", "imported"),
            counts_path=data.get("counts_path", ""),
            metadata_path=data.get("metadata_path", ""),
            summary=data.get("summary", ""),
            created_at=data["created_at"],
            updated_at=data.get("updated_at", data["created_at"]),
            species=data.get("species", "human"),
            status=data.get("status", "ready"),
            profile=dict(data.get("profile") or {}),
            validation=dict(data.get("validation") or {}),
            inspection=dict(data.get("inspection") or {}),
            provenance=dict(data.get("provenance") or {}),
            tags=list(data.get("tags", [])),
        )

    def update_cohort(self, cohort_id: str, *, title: str | None = None) -> Cohort:
        path = self._cohort_manifest_path(cohort_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown cohort_id: {cohort_id}")
        data = json.loads(path.read_text())
        if title is not None:
            cleaned = title.strip()
            if not cleaned:
                raise ValueError("title must not be empty.")
            data["title"] = cleaned
        data["updated_at"] = utc_now_iso()
        self._write_json(path, data)
        return self.get_cohort(cohort_id)

    def delete_cohort(self, cohort_id: str) -> str:
        cohort_dir = self._cohort_dir(cohort_id)
        if not cohort_dir.exists():
            raise FileNotFoundError(f"Unknown cohort_id: {cohort_id}")
        self._delete_session_uploads_for_cohort(cohort_id)
        shutil.rmtree(cohort_dir)
        return cohort_id

    def _delete_session_uploads_for_cohort(self, cohort_id: str) -> None:
        token = str(cohort_id or "").strip()
        if not token:
            return
        for session_path in sorted(self.sessions_root.glob("*/session.json")):
            session_id = session_path.parent.name
            uploads = self.list_artifacts(session_id, kind="upload")
            cohort_upload_ids = [
                upload.artifact_id
                for upload in uploads
                if str((upload.provenance or {}).get("cohort_id") or "").strip() == token
            ]
            if cohort_upload_ids:
                self.delete_artifacts(session_id, cohort_upload_ids)

    def create_session_from_cohort(
        self,
        *,
        cohort_id: str,
        title: str,
        description: str,
        species: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        cohort = self.get_cohort(cohort_id)
        session_tags = [tag.strip() for tag in (tags or []) if str(tag).strip()]
        session_tags.append(f"cohort:{cohort_id}")
        session = self.create_session(
            title=title,
            description=description,
            species=species,
            tags=session_tags,
        )
        counts_upload = self.create_upload(
            session.session_id,
            data_path=cohort.counts_path,
            source_mode="linked",
            title=f"{cohort.title} counts",
            source_path=cohort.counts_path,
            stored_path=cohort.counts_path,
            file_name=Path(cohort.counts_path).name,
            mime_type="text/tab-separated-values",
            provider="cohort_library",
            provenance={
                "cohort_id": cohort_id,
                "cohort_role": "counts",
                "cohort_title": cohort.title,
            },
        )
        metadata_upload = self.create_upload(
            session.session_id,
            data_path=cohort.metadata_path,
            source_mode="linked",
            title=f"{cohort.title} metadata",
            source_path=cohort.metadata_path,
            stored_path=cohort.metadata_path,
            file_name=Path(cohort.metadata_path).name,
            mime_type="text/tab-separated-values",
            provider="cohort_library",
            provenance={
                "cohort_id": cohort_id,
                "cohort_role": "metadata",
                "cohort_title": cohort.title,
            },
        )
        return {
            **asdict(session),
            "linked_upload_ids": [counts_upload.artifact_id, metadata_upload.artifact_id],
            "cohort_id": cohort_id,
            "cohort_title": cohort.title,
        }

    def save_session(self, session: AnalysisSession) -> AnalysisSession:
        session.updated_at = utc_now_iso()
        self._write_session(session)
        return session

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> AnalysisSession:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            if title is not None:
                cleaned_title = title.strip()
                if not cleaned_title:
                    raise ValueError("title must not be empty.")
                session.title = cleaned_title
            if description is not None:
                cleaned_description = description.strip()
                if not cleaned_description:
                    raise ValueError("description must not be empty.")
                session.description = cleaned_description
            return self.save_session(session)

    def archive_session(self, session_id: str) -> AnalysisSession:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            session.status = "archived"
            return self.save_session(session)

    def add_note(
        self,
        session_id: str,
        *,
        title: str,
        body_markdown: str,
        author: str = "user",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> SessionNote:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            now = utc_now_iso()
            note = SessionNote(
                note_id=new_id("note"),
                title=title.strip(),
                body_markdown=body_markdown.strip(),
                created_at=now,
                updated_at=now,
                author=author.strip() or "user",
                linked_artifact_ids=[
                    artifact_id.strip()
                    for artifact_id in (linked_artifact_ids or [])
                    if str(artifact_id).strip()
                ],
                tags=[tag.strip() for tag in (tags or []) if str(tag).strip()],
            )
            session.notes.append(note)
            self.save_session(session)
        return note

    def get_note(self, session_id: str, note_id: str) -> SessionNote:
        session = self.get_session(session_id)
        for note in session.notes:
            if note.note_id == note_id:
                return note
        raise FileNotFoundError(f"Unknown note_id: {note_id}")

    def list_notes(self, session_id: str) -> list[SessionNote]:
        session = self.get_session(session_id)
        ranked = sorted(enumerate(session.notes), key=lambda item: (item[1].updated_at, item[0]), reverse=True)
        return [note for _, note in ranked]

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
    ) -> SessionNote:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            for index, note in enumerate(session.notes):
                if note.note_id != note_id:
                    continue
                updated = SessionNote(
                    note_id=note.note_id,
                    title=title.strip(),
                    body_markdown=body_markdown.strip(),
                    created_at=note.created_at,
                    updated_at=utc_now_iso(),
                    author=author.strip() or note.author or "user",
                    linked_artifact_ids=[
                        artifact_id.strip()
                        for artifact_id in (linked_artifact_ids or [])
                        if str(artifact_id).strip()
                    ],
                    tags=[tag.strip() for tag in (tags or []) if str(tag).strip()],
                )
                session.notes[index] = updated
                self.save_session(session)
                return updated
        raise FileNotFoundError(f"Unknown note_id: {note_id}")

    def delete_note(self, session_id: str, note_id: str) -> str:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            remaining = [note for note in session.notes if note.note_id != note_id]
            if len(remaining) == len(session.notes):
                raise FileNotFoundError(f"Unknown note_id: {note_id}")
            session.notes = remaining
            self.save_session(session)
            note_path = self._note_path(session_id, note_id)
            if note_path.exists():
                note_path.unlink()
        return note_id

    def delete_notes(self, session_id: str, note_ids: Iterable[str]) -> list[str]:
        targets = {str(note_id).strip() for note_id in note_ids if str(note_id).strip()}
        if not targets:
            return []
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            deleted = [note.note_id for note in session.notes if note.note_id in targets]
            if not deleted:
                return []
            session.notes = [note for note in session.notes if note.note_id not in targets]
            self.save_session(session)
            for note_id in deleted:
                note_path = self._note_path(session_id, note_id)
                if note_path.exists():
                    note_path.unlink()
        return deleted

    def create_report(
        self,
        session_id: str,
        *,
        title: str,
        body_markdown: str,
        author: str = "assistant",
        focus: str = "",
        template_id: str = "",
        custom_guidance: str = "",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> SessionReport:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            now = utc_now_iso()
            report = SessionReport(
                report_id=new_id("report"),
                title=title.strip(),
                body_markdown=body_markdown.strip(),
                created_at=now,
                updated_at=now,
                author=author.strip() or "assistant",
                focus=focus.strip(),
                template_id=template_id.strip(),
                custom_guidance=custom_guidance.strip(),
                linked_artifact_ids=[
                    artifact_id.strip()
                    for artifact_id in (linked_artifact_ids or [])
                    if str(artifact_id).strip()
                ],
                tags=[tag.strip() for tag in (tags or []) if str(tag).strip()],
            )
            session.reports.append(report)
            self.save_session(session)
        return report

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
    ) -> SessionReport:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            for index, report in enumerate(session.reports):
                if report.report_id != report_id:
                    continue
                updated = SessionReport(
                    report_id=report.report_id,
                    title=(title.strip() if title is not None else report.title) or report.title,
                    body_markdown=body_markdown.strip(),
                    created_at=report.created_at,
                    updated_at=utc_now_iso(),
                    author=(author.strip() if author is not None else report.author) or report.author,
                    focus=(focus.strip() if focus is not None else report.focus),
                    template_id=(template_id.strip() if template_id is not None else report.template_id),
                    custom_guidance=(
                        custom_guidance.strip() if custom_guidance is not None else report.custom_guidance
                    ),
                    linked_artifact_ids=[
                        artifact_id.strip()
                        for artifact_id in (
                            linked_artifact_ids if linked_artifact_ids is not None else report.linked_artifact_ids
                        )
                        if str(artifact_id).strip()
                    ],
                    tags=[
                        tag.strip()
                        for tag in (tags if tags is not None else report.tags)
                        if str(tag).strip()
                    ],
                )
                session.reports[index] = updated
                self.save_session(session)
                return updated
        raise FileNotFoundError(f"Unknown report_id: {report_id}")

    def delete_report(self, session_id: str, report_id: str) -> str:
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            remaining = [report for report in session.reports if report.report_id != report_id]
            if len(remaining) == len(session.reports):
                raise FileNotFoundError(f"Unknown report_id: {report_id}")
            session.reports = remaining
            self.save_session(session)
            report_path = self._report_path(session_id, report_id)
            if report_path.exists():
                report_path.unlink()
        return report_id

    def delete_reports(self, session_id: str, report_ids: Iterable[str]) -> list[str]:
        targets = {str(report_id).strip() for report_id in report_ids if str(report_id).strip()}
        if not targets:
            return []
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            deleted = [report.report_id for report in session.reports if report.report_id in targets]
            if not deleted:
                return []
            session.reports = [report for report in session.reports if report.report_id not in targets]
            self.save_session(session)
            for report_id in deleted:
                report_path = self._report_path(session_id, report_id)
                if report_path.exists():
                    report_path.unlink()
        return deleted

    def get_report(self, session_id: str, report_id: str) -> SessionReport:
        session = self.get_session(session_id)
        for report in session.reports:
            if report.report_id == report_id:
                return report
        raise FileNotFoundError(f"Unknown report_id: {report_id}")

    def list_reports(self, session_id: str) -> list[SessionReport]:
        session = self.get_session(session_id)
        ranked = sorted(enumerate(session.reports), key=lambda item: (item[1].updated_at, item[0]), reverse=True)
        return [report for _, report in ranked]

    def create_artifact(
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
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            artifact = Artifact(
                artifact_id=new_id("artifact"),
                session_id=session_id,
                kind=kind.strip(),
                title=title.strip(),
                summary=summary.strip(),
                created_at=utc_now_iso(),
                provider=provider.strip() if provider else None,
                provenance=dict(provenance or {}),
                payload=dict(payload or {}),
            )
            artifact_path = self._artifact_path(session_id, artifact.artifact_id)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_json(artifact_path, asdict(artifact))
            session.artifact_ids.append(artifact.artifact_id)
            self.save_session(session)
        return artifact

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
        preview_max_lines: int = 100,
    ) -> Artifact:
        normalized_mode = source_mode.strip().lower()
        if normalized_mode not in {"copied", "linked"}:
            raise ValueError("source_mode must be either 'copied' or 'linked'.")

        resolved_data_path = Path(data_path).expanduser().resolve()
        if not resolved_data_path.exists():
            raise FileNotFoundError(f"Upload file not found: {resolved_data_path}")

        stat = resolved_data_path.stat()
        upload_name = (file_name.strip() or resolved_data_path.name).strip() or "upload"
        preview_lines = self._read_upload_preview(resolved_data_path, max_lines=preview_max_lines)
        payload: dict[str, Any] = {
            "source_mode": normalized_mode,
            "data_path": str(resolved_data_path),
            "source_path": str(Path(source_path).expanduser().resolve()) if source_path.strip() else "",
            "stored_path": str(Path(stored_path).expanduser().resolve()) if stored_path.strip() else "",
            "file_name": upload_name,
            "mime_type": mime_type.strip(),
            "file_size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
            "preview_lines": preview_lines,
            "preview_excerpt": self._preview_excerpt(preview_lines, limit=220) if preview_lines else "",
        }
        provenance_payload = dict(provenance or {})
        if normalized_mode == "copied":
            provenance_payload.setdefault("upload_source_mode", "copied")
        else:
            provenance_payload.setdefault("upload_source_mode", "linked")

        try:
            inspection = inspect_tabular_file(
                resolved_data_path,
                role_hint=str(provenance_payload.get("cohort_role") or ""),
                provenance=provenance_payload,
            )
            payload.update(
                {
                    "data_type": inspection.data_type,
                    "role": inspection.role,
                    "sample_count": int(inspection.summary.get("sample_count", 0)),
                    "gene_count": int(inspection.summary.get("gene_count", 0)),
                    "has_metadata": bool(inspection.summary.get("has_metadata", False)),
                    "validation_status": str(inspection.summary.get("validation_status", "ok")),
                    "one_line_summary": str(inspection.summary.get("one_line_summary", "")),
                    "summary": dict(inspection.summary),
                    "technical": dict(inspection.technical),
                    "inspection": dict(inspection.inspection),
                    "validation": {
                        "status": str(inspection.summary.get("validation_status", "ok")),
                        "warnings": list(inspection.warnings),
                    },
                }
            )
            if inspection.warnings:
                payload["warnings"] = list(inspection.warnings)
        except Exception:
            payload.update(
                {
                    "data_type": "generic_table" if preview_lines else "file",
                    "role": "upload",
                    "sample_count": 0,
                    "gene_count": 0,
                    "has_metadata": bool(provenance_payload.get("cohort_id")),
                    "validation_status": "warning" if preview_lines else "unknown",
                    "one_line_summary": "Upload file" if not preview_lines else self._preview_excerpt(preview_lines, limit=220),
                    "summary": {
                        "data_type": "generic_table" if preview_lines else "file",
                        "role": "upload",
                        "sample_count": 0,
                        "gene_count": 0,
                        "has_metadata": bool(provenance_payload.get("cohort_id")),
                        "validation_status": "warning" if preview_lines else "unknown",
                        "one_line_summary": "Upload file" if not preview_lines else self._preview_excerpt(preview_lines, limit=220),
                    },
                    "technical": {
                        "path": str(resolved_data_path),
                        "file_name": upload_name,
                        "format": "tabular" if preview_lines else "file",
                        "row_count": 0,
                        "column_count": 0,
                        "first_column": "",
                        "columns": [],
                        "source_path": str(resolved_data_path),
                        "role_hint": str(provenance_payload.get("cohort_role") or ""),
                        "provenance": provenance_payload,
                    },
                    "inspection": {
                        "shape": {"rows": 0, "columns": 0},
                        "data_type": "generic_table" if preview_lines else "file",
                        "role": "upload",
                        "orientation": "unknown",
                        "row_identifier_column": "",
                        "column_names": [],
                        "sample_count": 0,
                        "gene_count": 0,
                        "candidate_sample_column": "",
                        "candidate_group_columns": [],
                        "candidate_covariates": [],
                        "detected_columns": {},
                        "validation": {
                            "status": "warning" if preview_lines else "unknown",
                            "warnings": [],
                        },
                        "next_step_hints": [],
                    },
                    "validation": {
                        "status": "warning" if preview_lines else "unknown",
                        "warnings": [],
                    },
                }
            )
        summary_bits = [normalized_mode.capitalize(), upload_name]
        size_text = self._format_file_size(stat.st_size)
        if size_text:
            summary_bits.append(size_text)
        if payload["preview_excerpt"]:
            summary_bits.append(payload["preview_excerpt"])
        summary = " · ".join(part for part in summary_bits if part)

        return self.create_artifact(
            session_id,
            kind="upload",
            title=title.strip() or upload_name,
            summary=summary,
            provider=provider or "local_upload",
            payload=payload,
            provenance=provenance_payload,
        )

    def copy_artifacts(
        self,
        target_session_id: str,
        sources: Iterable[tuple[str, str]],
    ) -> list[Artifact]:
        copied: list[Artifact] = []
        for source_session_id, artifact_id in sources:
            artifact = self.get_artifact(source_session_id, artifact_id)
            clone = self.create_artifact(
                target_session_id,
                kind=artifact.kind,
                title=artifact.title,
                summary=artifact.summary,
                provider=artifact.provider,
                payload=self._clone_artifact_payload(artifact, target_session_id),
                provenance={
                    **artifact.provenance,
                    "copied_from_session_id": source_session_id,
                    "copied_from_artifact_id": artifact_id,
                },
            )
            copied.append(clone)
        return copied

    def get_artifact(self, session_id: str, artifact_id: str) -> Artifact:
        path = self._artifact_path(session_id, artifact_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown artifact_id: {artifact_id}")
        data = json.loads(path.read_text())
        return Artifact(**data)

    def find_artifact_sessions(self, artifact_id: str) -> list[str]:
        token = str(artifact_id or "").strip()
        if not token:
            return []
        matches: list[str] = []
        for session in self.list_sessions():
            if token in session.artifact_ids:
                matches.append(session.session_id)
        return matches

    def list_artifacts(self, session_id: str, *, kind: str | None = None) -> list[Artifact]:
        session = self.get_session(session_id)
        artifacts = [self.get_artifact(session_id, artifact_id) for artifact_id in session.artifact_ids]
        if kind:
            artifacts = [item for item in artifacts if item.kind == kind]
        else:
            artifacts = [item for item in artifacts if item.kind not in {"report_draft", "upload"}]
        artifacts.sort(key=lambda item: item.created_at)
        return artifacts

    def delete_artifacts(self, session_id: str, artifact_ids: Iterable[str]) -> list[str]:
        targets = {str(artifact_id).strip() for artifact_id in artifact_ids if str(artifact_id).strip()}
        if not targets:
            return []
        with self._session_lock(session_id):
            session = self.get_session(session_id)
            existing = set(session.artifact_ids)
            deleted = [artifact_id for artifact_id in session.artifact_ids if artifact_id in targets]
            if not deleted:
                return []
            session.artifact_ids = [artifact_id for artifact_id in session.artifact_ids if artifact_id not in targets]
            self.save_session(session)
            for artifact_id in deleted:
                if artifact_id not in existing:
                    continue
                try:
                    artifact = self.get_artifact(session_id, artifact_id)
                except FileNotFoundError:
                    artifact = None
                if artifact is not None:
                    self._cleanup_artifact_files(artifact)
                artifact_path = self._artifact_path(session_id, artifact_id)
                if artifact_path.exists():
                    artifact_path.unlink()
        return deleted

    def read_report(self, session_id: str, report_id: str | None = None) -> str:
        if report_id:
            report_path = self._report_path(session_id, report_id)
            if not report_path.exists():
                raise FileNotFoundError(f"Unknown report_id: {report_id}")
            return self._read_markdown_body(report_path)
        reports = self.list_reports(session_id)
        if reports:
            return reports[0].body_markdown
        legacy = self.reports_root / session_id / "report.md"
        if legacy.exists():
            return self._read_markdown_body(legacy)
        raise FileNotFoundError(f"No saved report exists for session_id: {session_id}")

    def delete_sessions(self, session_ids: Iterable[str]) -> list[str]:
        deleted: list[str] = []
        for session_id in session_ids:
            session_dir = self._session_dir(session_id)
            report_dir = self.reports_root / session_id
            if session_dir.exists():
                shutil.rmtree(session_dir)
                deleted.append(session_id)
            if report_dir.exists():
                shutil.rmtree(report_dir)
        return deleted

    def _session_dir(self, session_id: str) -> Path:
        return self.sessions_root / session_id

    def _session_metadata_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.json"

    def _artifact_path(self, session_id: str, artifact_id: str) -> Path:
        return self._session_dir(session_id) / "artifacts" / f"{artifact_id}.json"

    def _notes_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "notes"

    def _note_path(self, session_id: str, note_id: str) -> Path:
        return self._notes_dir(session_id) / f"{note_id}.md"

    def _report_dir(self, session_id: str) -> Path:
        return self.reports_root / session_id

    def _figure_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "figures"

    def _cohort_dir(self, cohort_id: str) -> Path:
        return self.cohorts_root / cohort_id

    def _cohort_manifest_path(self, cohort_id: str) -> Path:
        return self._cohort_dir(cohort_id) / "manifest.json"

    def _report_path(self, session_id: str, report_id: str) -> Path:
        return self._report_dir(session_id) / f"{report_id}.md"

    def _write_session(self, session: AnalysisSession) -> None:
        session_dir = self._session_dir(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(session)
        self._write_json(self._session_metadata_path(session.session_id), payload)
        self._sync_note_files(session)
        self._sync_report_files(session)

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp_path.replace(path)

    def _sync_note_files(self, session: AnalysisSession) -> None:
        notes_dir = self._notes_dir(session.session_id)
        notes_dir.mkdir(parents=True, exist_ok=True)
        active_ids: set[str] = set()
        for note in session.notes:
            active_ids.add(note.note_id)
            self._write_note_markdown(session.session_id, note)
        for path in notes_dir.glob("*.md"):
            if path.stem not in active_ids:
                path.unlink()

    def _write_note_markdown(self, session_id: str, note: SessionNote) -> None:
        front_matter = [
            "---",
            f'note_id: "{note.note_id}"',
            f'title: "{self._escape_front_matter(note.title)}"',
            f'author: "{self._escape_front_matter(note.author)}"',
            f'created_at: "{note.created_at}"',
            f'updated_at: "{note.updated_at}"',
            f"tags: {self._format_front_matter_list(note.tags)}",
            f"linked_artifact_ids: {self._format_front_matter_list(note.linked_artifact_ids)}",
            "---",
            "",
            note.body_markdown.strip(),
            "",
        ]
        self._note_path(session_id, note.note_id).write_text("\n".join(front_matter))

    def _sync_report_files(self, session: AnalysisSession) -> None:
        report_dir = self._report_dir(session.session_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        active_ids: set[str] = set()
        for report in session.reports:
            active_ids.add(report.report_id)
            self._write_report_markdown(session.session_id, report)
        for path in report_dir.glob("*.md"):
            if path.name == "report.md":
                continue
            if path.stem not in active_ids:
                path.unlink()

    def _write_report_markdown(self, session_id: str, report: SessionReport) -> None:
        front_matter = [
            "---",
            f'report_id: "{report.report_id}"',
            f'title: "{self._escape_front_matter(report.title)}"',
            f'author: "{self._escape_front_matter(report.author)}"',
            f'created_at: "{report.created_at}"',
            f'updated_at: "{report.updated_at}"',
            f'focus: "{self._escape_front_matter(report.focus)}"',
            f'template_id: "{self._escape_front_matter(report.template_id)}"',
            f"tags: {self._format_front_matter_list(report.tags)}",
            f"linked_artifact_ids: {self._format_front_matter_list(report.linked_artifact_ids)}",
            "---",
            "",
            report.body_markdown.strip(),
            "",
        ]
        self._report_path(session_id, report.report_id).write_text("\n".join(front_matter))

    def _clone_artifact_payload(self, artifact: Artifact, target_session_id: str) -> dict[str, Any]:
        payload = json.loads(json.dumps(artifact.payload or {}))
        if artifact.kind != "figure":
            return payload

        copied_files: list[str] = []
        target_dir = self._figure_dir(target_session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        for key in ("png_path", "svg_path"):
            raw_path = str(payload.get(key) or "").strip()
            if not raw_path:
                continue
            source_path = Path(raw_path).expanduser()
            if not source_path.exists():
                continue
            destination = target_dir / f"{new_id('figure_file')}{source_path.suffix}"
            shutil.copy2(source_path, destination)
            payload[key] = str(destination)
            copied_files.append(str(destination))
        if copied_files:
            payload["figure_files"] = copied_files
            if payload.get("display_path") and str(payload["display_path"]).strip().endswith(".png"):
                payload["display_path"] = payload.get("png_path")
            if payload.get("image_markdown") and payload.get("png_path"):
                payload["image_markdown"] = f"![{payload.get('title') or artifact.title}]({payload['png_path']})"
        return payload

    def _cleanup_artifact_files(self, artifact: Artifact) -> None:
        files = []
        payload = artifact.payload or {}
        for key in ("png_path", "svg_path", "display_path", "stored_path"):
            raw_path = str(payload.get(key) or "").strip()
            if raw_path:
                files.append(raw_path)
        for raw_path in payload.get("figure_files") or []:
            token = str(raw_path).strip()
            if token:
                files.append(token)

        session_root = self._session_dir(artifact.session_id).resolve()
        for raw_path in set(files):
            path = Path(raw_path).expanduser()
            try:
                resolved = path.resolve()
            except FileNotFoundError:
                continue
            if session_root not in resolved.parents:
                continue
            if resolved.exists():
                resolved.unlink()

    def _read_upload_preview(
        self,
        path: Path,
        *,
        max_bytes: int = 8192,
        max_lines: int = 8,
        max_line_chars: int = 1000,
    ) -> list[str]:
        try:
            return read_preview_lines(
                path,
                max_lines=max_lines,
                max_bytes=max_bytes,
                max_line_chars=max_line_chars,
            )
        except OSError:
            return []

    @staticmethod
    def _format_file_size(size: int) -> str:
        value = max(0, int(size))
        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        number = float(value)
        while number >= 1024 and unit_index < len(units) - 1:
            number /= 1024.0
            unit_index += 1
        if unit_index == 0:
            return f"{value} B"
        return f"{number:.1f} {units[unit_index]}"

    @staticmethod
    def _preview_excerpt(lines: list[str], *, limit: int = 220) -> str:
        compact = " ".join(" ".join(str(line or "").split()) for line in lines if str(line or "").strip())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 1)] + "…"

    def _session_note_from_row(self, session_id: str, row: dict[str, Any]) -> SessionNote:
        body = str(row.get("body_markdown") or row.get("text") or "").strip()
        note_id = str(row.get("note_id") or new_id("note")).strip()
        if not body:
            note_path = self._note_path(session_id, note_id)
            if note_path.exists():
                body = self._read_markdown_body(note_path)
        title = str(row.get("title") or "").strip() or self._derive_note_title(body)
        created_at = str(row.get("created_at") or utc_now_iso()).strip()
        updated_at = str(row.get("updated_at") or created_at).strip() or created_at
        author = str(row.get("author") or "user").strip() or "user"
        return SessionNote(
            note_id=note_id,
            title=title,
            body_markdown=body,
            created_at=created_at,
            updated_at=updated_at,
            author=author,
            linked_artifact_ids=[str(item).strip() for item in row.get("linked_artifact_ids", []) if str(item).strip()],
            tags=[str(tag).strip() for tag in row.get("tags", []) if str(tag).strip()],
        )

    def _session_report_from_row(self, session_id: str, row: dict[str, Any]) -> SessionReport:
        body = str(row.get("body_markdown") or "").strip()
        report_id = str(row.get("report_id") or new_id("report")).strip()
        if not body:
            report_path = self._report_path(session_id, report_id)
            if report_path.exists():
                body = self._read_markdown_body(report_path)
        title = str(row.get("title") or "").strip() or self._derive_note_title(body)
        created_at = str(row.get("created_at") or utc_now_iso()).strip()
        updated_at = str(row.get("updated_at") or created_at).strip() or created_at
        author = str(row.get("author") or "assistant").strip() or "assistant"
        focus = str(row.get("focus") or "").strip()
        template_id = str(row.get("template_id") or "").strip()
        custom_guidance = str(row.get("custom_guidance") or "").strip()
        return SessionReport(
            report_id=report_id,
            title=title,
            body_markdown=body,
            created_at=created_at,
            updated_at=updated_at,
            author=author,
            focus=focus,
            template_id=template_id,
            custom_guidance=custom_guidance,
            linked_artifact_ids=[str(item).strip() for item in row.get("linked_artifact_ids", []) if str(item).strip()],
            tags=[str(tag).strip() for tag in row.get("tags", []) if str(tag).strip()],
        )

    def _legacy_report(self, session_id: str, row: dict[str, Any]) -> SessionReport | None:
        report_path = self.reports_root / session_id / "report.md"
        if not report_path.exists():
            return None
        body = self._read_markdown_body(report_path)
        timestamp = str(row.get("updated_at") or row.get("created_at") or utc_now_iso()).strip()
        return SessionReport(
            report_id="report_legacy",
            title="Legacy report",
            body_markdown=body,
            created_at=timestamp,
            updated_at=timestamp,
            author="assistant",
            focus="",
            template_id="",
            custom_guidance="",
            linked_artifact_ids=[],
            tags=["legacy"],
        )

    def _read_markdown_body(self, path: Path) -> str:
        raw = path.read_text()
        if raw.startswith("---\n"):
            marker = raw.find("\n---\n", 4)
            if marker != -1:
                return raw[marker + 5 :].strip()
        return raw.strip()

    def _derive_note_title(self, body_markdown: str) -> str:
        for line in body_markdown.splitlines():
            cleaned = line.strip().lstrip("#").strip()
            if cleaned:
                return cleaned[:80]
        return "Untitled note"

    def _escape_front_matter(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _format_front_matter_list(self, values: list[str]) -> str:
        quoted = [f'"{self._escape_front_matter(value)}"' for value in values if str(value).strip()]
        return f"[{', '.join(quoted)}]"

    @contextmanager
    def _session_lock(self, session_id: str):
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        lock_path = session_dir / ".lock"
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


DiskStore = WorkspaceStore


__all__ = ["WorkspaceStore", "DiskStore"]
