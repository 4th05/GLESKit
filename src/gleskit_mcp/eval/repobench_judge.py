from __future__ import annotations

import argparse
import asyncio
from difflib import SequenceMatcher
from html import unescape
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any, TypedDict

from gleskit_mcp.eval.bixbench import (
    current_utc_iso,
    read_json,
    sha256_file,
    sha256_text,
    write_json_atomic,
    write_text_atomic,
)
from gleskit_mcp.eval.bixbench_judge import (
    DEFAULT_JUDGE_PROVIDER,
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_OPENROUTER_BASE_URL,
    JudgeProgressTracker,
    JudgeStructuredOutputError,
    _invoke_structured,
    _normalize_judge_provider,
    _require_dependencies,
    _resolve_judge_provider_credentials,
    _resolve_model_name_for_provider,
)
from gleskit_mcp.eval.judge_bands import (
    STANDARD_JUDGE_SCORE_BANDS,
    judge_category,
    judge_category_disagreement_span,
    judge_category_level,
    judge_level_mean_normalized,
    judge_mean_level_category,
    judge_score_band,
)
from gleskit_mcp.eval.repobench import load_repobench_rows, select_repobench_rows
from gleskit_mcp.eval.scoring import visible_sources_blocks
from gleskit_mcp.eval.repobench_runner import (
    _write_csv,
    build_overall_summary,
    load_episode_record,
    write_reports,
    write_summary_artifacts,
)
from gleskit_mcp.eval.runtime import compact_json
from gleskit_mcp.store import WorkspaceStore

try:  # pragma: no cover - optional eval dependency
    from langgraph.graph import END, START, StateGraph
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover - optional dependency fallback
    START = None
    END = None
    StateGraph = None
    BaseModel = object
    Field = None


DEFAULT_MODEL = "gpt-4.1-mini"
SPECIALIST_WEIGHTS = {
    "biomedical_content_specialist": 0.35,
    "evidence_support_specialist": 0.30,
    "template_compliance_specialist": 0.20,
    "provenance_quality_specialist": 0.15,
}
JUDGE_SCORE_BANDS = STANDARD_JUDGE_SCORE_BANDS
JUDGE_SCHEMA_VERSION = 6
HUMAN_AUDIT_SCHEMA_VERSION = 1
# The committee returns short assessments, not reports. Bounding completions
# prevents a provider-side generation stall while leaving the rubric unchanged.
SPECIALIST_MAX_OUTPUT_TOKENS = 1600
ARBITER_MAX_OUTPUT_TOKENS = 900
MATERIAL_EVIDENCE_FAILURE_CODES = {
    "contradiction",
    "citation_identity_mismatch",
    "citation_title_mismatch",
    "missing_trace_support",
    "overclaim",
    "source_not_in_artifacts",
    "trace_mismatch",
    "unsupported_claim",
}
OFFLINE_PROVENANCE_MAJOR_CODES = {
    "citation_identity_mismatch",
    "citation_title_mismatch",
    "missing_sources_line",
    "source_not_in_artifacts",
}
MARKDOWN_LINK_RE = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<url>[^)]+)\)")
ARTIFACT_ID_RE = re.compile(r"\bartifact_[0-9a-f]{12}\b", re.IGNORECASE)
EUROPE_PMC_URL_RE = re.compile(r"https?://(?:www\.)?europepmc\.org/article/(?:MED|PMC)/(?P<id>[A-Za-z0-9]+)", re.IGNORECASE)
PUBMED_URL_RE = re.compile(r"https?://(?:pubmed\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov/pubmed)/(?P<id>\d+)", re.IGNORECASE)
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s);]+", re.IGNORECASE)
EXPLICIT_CITATION_TITLE_RE = re.compile(
    r"\b(?:19|20)\d{2}\s*\)?\s*[,;:]?\s*[\"\u201c](?P<title>[^\"\u201d]+)[\"\u201d]"
)


class ReportSpecialistAssessment(BaseModel):  # type: ignore[misc]
    category: str
    summary: str
    key_observations: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    satisfied_criteria: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    failed_criteria: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    failure_codes: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]


class EvidenceSupportAssessment(BaseModel):  # type: ignore[misc]
    category: str
    summary: str
    key_observations: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    satisfied_criteria: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    failed_criteria: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    failure_codes: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    supported_claims: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    unsupported_claims: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    evidence_gaps: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]


class ArbiterVerdict(BaseModel):  # type: ignore[misc]
    short_review: str
    rationale: str
    major_strengths: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    major_issues: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    disagreement_flags: list[str] = Field(default_factory=list, max_length=6)  # type: ignore[operator]
    review_required: bool = False
    review_reasons: list[str] = Field(default_factory=list)  # type: ignore[operator]


class JudgeState(TypedDict, total=False):
    run_root: str
    episode_root: str
    selection_row: dict[str, Any]
    episode_record: dict[str, Any]
    prompt_context: str
    offline_provenance_audit: dict[str, Any]
    report_freeze_verification: dict[str, Any]
    specialist_scores: dict[str, float]
    content_assessment: dict[str, Any]
    evidence_assessment: dict[str, Any]
    template_assessment: dict[str, Any]
    provenance_assessment: dict[str, Any]
    arbiter_verdict: dict[str, Any]
    model_map: dict[str, str]


def _context_text(record: dict[str, Any]) -> str:
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    # The saved report is the primary evaluation object. Keep it intact rather
    # than applying a global context truncation that can make a complete report
    # appear to end mid-section. Trace-heavy fields are compacted separately.
    report_metadata = {
        key: value
        for key, value in report.items()
        if key not in {"body_markdown", "linked_artifacts"}
    }
    prompt_context = {
        "task_id": record.get("task_id"),
        "report_type": record.get("report_type"),
        "template_requested": record.get("template_id_requested"),
        "template_used": record.get("template_id"),
        "title": record.get("title"),
        "task_prompt": record.get("task_prompt"),
        "kickoff_context": record.get("kickoff_context") or {},
        "expected_content": record.get("expected_content"),
        "expected_nuggets": record.get("expected_nuggets") or [],
        "forbidden_overclaims": record.get("forbidden_overclaims") or [],
        "report_saved": bool(record.get("report_saved")),
        "saved_report": report.get("body_markdown"),
        "report_metadata": report_metadata,
        "deterministic_metrics": record.get("deterministic_metrics") or {},
        "visible_transcript_summary": compact_json((record.get("transcript") or [])[-10:], 6000),
        "tool_provenance_summary": compact_json(record.get("tool_provenance") or [], 6000),
    }
    return json.dumps(prompt_context, ensure_ascii=False, sort_keys=True, default=str)


def _verify_report_freeze(episode_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Verify that the on-disk report is the captured report the judge sees.

    This is provenance metadata only.  It never changes an agent result or a
    committee score; it lets a later reviewer detect post-capture edits.
    """

    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    report_text = str(report.get("body_markdown") or "")
    freeze = record.get("report_freeze") if isinstance(record.get("report_freeze"), dict) else {}
    expected_digest = str(freeze.get("content_sha256") or "").strip() or None
    captured_digest = sha256_text(report_text) if report_text else None
    report_path = episode_root / "report.md"
    file_digest = sha256_file(report_path) if report_path.is_file() else None

    verification: dict[str, Any] = {
        "schema_version": 1,
        "algorithm": "sha256",
        "report_path": "report.md",
        "expected_content_sha256": expected_digest,
        "captured_content_sha256": captured_digest,
        "report_file_sha256": file_digest,
        "captured_matches_expected": (
            None if expected_digest is None or captured_digest is None else captured_digest == expected_digest
        ),
        "file_matches_expected": (
            None if expected_digest is None or file_digest is None else file_digest == expected_digest
        ),
    }
    if not report_text:
        verification["status"] = "not_applicable"
    elif expected_digest is None:
        verification["status"] = "legacy_unverified"
    elif captured_digest != expected_digest:
        verification["status"] = "captured_record_mismatch"
    elif file_digest is None:
        verification["status"] = "report_file_missing"
    elif file_digest != expected_digest:
        verification["status"] = "report_file_mismatch"
    else:
        verification["status"] = "verified"
    return verification


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


def _sources_lines(report_text: str) -> list[tuple[int, str]]:
    return visible_sources_blocks(report_text)


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
    explicit_title = EXPLICIT_CITATION_TITLE_RE.search(prefix)
    if explicit_title is not None:
        return str(explicit_title.group("title") or "").strip() or None
    link_text = str(match.group("text") or "").strip()
    lowered = link_text.lower()
    if (
        link_text
        and lowered not in {"europe pmc", "pubmed", "doi", "paper", "article"}
        and re.fullmatch(r"(?:pmc|pmid)[\s:_-]*\d+", lowered) is None
    ):
        return link_text
    return None


def _resolve_workspace_root(run_root: Path) -> Path:
    manifest_path = run_root / "run_manifest.json"
    if manifest_path.exists():
        try:
            manifest = read_json(manifest_path)
            workspace_home = str((manifest or {}).get("workspace_home") or "").strip()
            if workspace_home:
                return Path(workspace_home)
        except Exception:
            pass
    return run_root / "workspace"


def _build_session_paper_registry(store: WorkspaceStore, session_id: str) -> dict[str, dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    by_pmid: dict[str, dict[str, Any]] = {}
    by_doi: dict[str, dict[str, Any]] = {}
    for artifact in store.list_artifacts(session_id):
        if str(artifact.kind or "").strip().lower() != "paper_search":
            continue
        papers = artifact.payload.get("papers") if isinstance(artifact.payload, dict) else []
        if not isinstance(papers, list):
            continue
        for paper in papers:
            if not isinstance(paper, dict):
                continue
            entry = {
                "artifact_id": artifact.artifact_id,
                "artifact_title": artifact.title,
                "title": str(paper.get("title") or "").strip(),
                "pmid": str(paper.get("pmid") or paper.get("id") or "").strip(),
                "doi": str(paper.get("doi") or "").strip().lower(),
                "url": _normalize_url(paper.get("url")),
            }
            if entry["url"]:
                by_url[entry["url"]] = entry
            if entry["pmid"]:
                by_pmid[entry["pmid"]] = entry
            if entry["doi"]:
                by_doi[entry["doi"]] = entry
    return {"by_url": by_url, "by_pmid": by_pmid, "by_doi": by_doi}


def _prompt_friendly_offline_audit(audit: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(audit, dict):
        return {}
    return {
        "status": audit.get("status"),
        "source_line_count": audit.get("source_line_count"),
        "session_artifact_count": audit.get("session_artifact_count"),
        "linked_artifact_count": audit.get("linked_artifact_count"),
        "cited_artifact_count": audit.get("cited_artifact_count"),
        "major_issue_count": audit.get("major_issue_count"),
        "issue_codes": audit.get("issue_codes") or [],
        "issues": (audit.get("issues") or [])[:8],
        "paper_checks": (audit.get("paper_checks") or [])[:8],
    }


def _offline_provenance_audit(run_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    report_text = str(report.get("body_markdown") or "").strip()
    session_id = str(report.get("session_id") or record.get("session_id") or "").strip()
    linked_artifact_ids = [
        str(item).strip()
        for item in (report.get("linked_artifact_ids") or [])
        if str(item).strip()
    ]
    if not report_text:
        return {"status": "skipped", "reason": "no_saved_report_text"}
    if not session_id:
        return {"status": "skipped", "reason": "missing_session_id"}

    workspace_root = _resolve_workspace_root(run_root)
    if not workspace_root.exists():
        return {"status": "error", "reason": f"workspace_root_missing:{workspace_root}"}

    try:
        store = WorkspaceStore(workspace_root)
        session_artifacts = store.list_artifacts(session_id)
    except Exception as exc:
        return {"status": "error", "reason": str(exc).strip() or type(exc).__name__}

    session_artifact_ids = {str(artifact.artifact_id).strip() for artifact in session_artifacts if str(artifact.artifact_id).strip()}
    registry = _build_session_paper_registry(store, session_id)
    source_lines = _sources_lines(report_text)
    issues: list[dict[str, Any]] = []
    paper_checks: list[dict[str, Any]] = []

    cited_artifact_ids = sorted({match.group(0) for match in ARTIFACT_ID_RE.finditer(report_text)})
    for artifact_id in cited_artifact_ids:
        if artifact_id not in session_artifact_ids:
            issues.append(
                {
                    "code": "source_not_in_artifacts",
                    "severity": "major",
                    "artifact_id": artifact_id,
                    "detail": "Report cites an artifact id that is not present in the saved session artifacts.",
                }
            )
        elif linked_artifact_ids and artifact_id not in linked_artifact_ids:
            issues.append(
                {
                    "code": "artifact_cited_not_linked",
                    "severity": "minor",
                    "artifact_id": artifact_id,
                    "detail": "Report cites an artifact id in the body but does not include it in linked_artifact_ids.",
                }
            )

    if not source_lines:
        issues.append(
            {
                "code": "missing_sources_line",
                "severity": "major",
                "detail": "Saved report has no visible `Sources:` lines.",
            }
        )

    for line_number, line_text in source_lines:
        segments = [segment.strip() for segment in line_text[len("Sources:"):].split(";") if segment.strip()]
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
            if paper_entry is None and (pmid is not None or doi is not None or "europepmc.org" in url.lower() or "pubmed" in url.lower() or "doi.org" in url.lower()):
                issues.append(
                    {
                        "code": "citation_identity_mismatch",
                        "severity": "major",
                        "line_number": line_number,
                        "url": url,
                        "detail": "Report cites a paper-like link that does not resolve to any saved paper-search artifact entry.",
                    }
                )
                continue
            if paper_entry is None:
                continue
            observed_title = _extract_visible_paper_title(segment)
            title_matches = observed_title is None or _titles_match(observed_title, paper_entry["title"])
            paper_checks.append(
                {
                    "line_number": line_number,
                    "url": url,
                    "pmid": paper_entry.get("pmid"),
                    "observed_title": observed_title,
                    "expected_title": paper_entry.get("title"),
                    "matched": bool(title_matches),
                }
            )
            if observed_title and not title_matches:
                issues.append(
                    {
                        "code": "citation_title_mismatch",
                        "severity": "major",
                        "line_number": line_number,
                        "url": url,
                        "observed_title": observed_title,
                        "expected_title": paper_entry.get("title"),
                        "detail": "Visible paper title does not match the locally saved paper metadata for the cited link.",
                    }
                )

    issue_codes = sorted({str(issue.get("code") or "").strip() for issue in issues if str(issue.get("code") or "").strip()})
    major_issue_count = sum(1 for issue in issues if str(issue.get("severity") or "").strip().lower() == "major")
    return {
        "status": "ok",
        "workspace_root": str(workspace_root),
        "session_id": session_id,
        "source_line_count": len(source_lines),
        "session_artifact_count": len(session_artifacts),
        "linked_artifact_count": len(linked_artifact_ids),
        "cited_artifact_count": len(cited_artifact_ids),
        "issue_count": len(issues),
        "major_issue_count": major_issue_count,
        "issue_codes": issue_codes,
        "issues": issues[:20],
        "paper_checks": paper_checks[:20],
    }


def _preceding_report_context(lines: list[str], source_line_number: int) -> str:
    """Return nearby visible report text for a reviewer without inferring claims."""

    context: list[str] = []
    for index in range(source_line_number - 2, -1, -1):
        line = lines[index].strip()
        if not line:
            if context:
                break
            continue
        if line.lower().startswith("sources:"):
            continue
        context.append(line)
        if len(context) >= 3:
            break
    return "\n".join(reversed(context))


def _human_audit_rows(record: dict[str, Any], *, review_id: str, report_file: str) -> list[dict[str, Any]]:
    """Build reviewer prompts from visible citations, without assigning validity."""

    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    report_text = str(report.get("body_markdown") or "")
    lines = report_text.splitlines()
    report_digest = sha256_text(report_text)
    base = {
        "review_id": review_id,
        "task_id": str(record.get("task_id") or ""),
        "report_type": str(record.get("report_type") or ""),
        "report_file": report_file,
        "report_sha256": report_digest,
        "reviewer_id": "",
        "citation_resolves_to_retrieved_artifact": "",
        "citation_supports_preceding_content": "",
        "overclaim_in_preceding_content": "",
        "reviewer_confidence": "",
        "reviewer_notes": "",
    }
    rows: list[dict[str, Any]] = []
    for line_number, source_line in _sources_lines(report_text):
        context = _preceding_report_context(lines, line_number)
        links = list(MARKDOWN_LINK_RE.finditer(source_line))
        if not links:
            rows.append(
                {
                    **base,
                    "source_line_number": line_number,
                    "preceding_report_context": context,
                    "reported_source": source_line,
                    "reported_link_text": "",
                    "reported_link_url": "",
                }
            )
            continue
        for link in links:
            rows.append(
                {
                    **base,
                    "source_line_number": line_number,
                    "preceding_report_context": context,
                    "reported_source": source_line,
                    "reported_link_text": link.group("text"),
                    "reported_link_url": link.group("url"),
                }
            )
    if not rows:
        rows.append(
            {
                **base,
                "source_line_number": "",
                "preceding_report_context": "",
                "reported_source": "",
                "reported_link_text": "",
                "reported_link_url": "",
            }
        )
    return rows


def write_human_audit_package(summary_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Write a blinded, non-scoring citation review package for saved reports.

    The package contains only the task-facing prompt and report text.  It does
    not expose the solve model, judge outcome, or evaluator gold criteria, and
    it is never read by the agent or committee.
    """

    audit_root = summary_root / "human_audit"
    reports_root = audit_root / "reports"
    reports_root.mkdir(parents=True, exist_ok=True)

    saved_records = [
        record
        for record in sorted(records, key=lambda item: str(item.get("task_id") or ""))
        if bool(record.get("report_saved")) and str((record.get("report") or {}).get("body_markdown") or "").strip()
    ]
    queue_rows: list[dict[str, Any]] = []
    citation_rows: list[dict[str, Any]] = []
    manifest_reports: list[dict[str, Any]] = []
    for index, record in enumerate(saved_records, start=1):
        review_id = f"R{index:02d}"
        report_file = f"reports/{review_id}.md"
        report = record.get("report") if isinstance(record.get("report"), dict) else {}
        report_text = str(report.get("body_markdown") or "")
        review_text = "\n".join(
            [
                f"# RepoBench Report {review_id}",
                "",
                "## Task",
                f"- Report type: {record.get('report_type') or ''}",
                f"- Title: {record.get('title') or ''}",
                "",
                str(record.get("task_prompt") or "").strip(),
                "",
                "## Saved Report",
                "",
                report_text.strip(),
                "",
            ]
        )
        write_text_atomic(reports_root / f"{review_id}.md", review_text)
        report_digest = sha256_text(report_text)
        queue_rows.append(
            {
                "review_id": review_id,
                "task_id": record.get("task_id"),
                "report_type": record.get("report_type"),
                "report_file": report_file,
                "report_sha256": report_digest,
                "reviewer_id": "",
                "review_status": "not_started",
                "substantive_claims_reviewed": "",
                "claims_supported": "",
                "claims_unsupported_or_unclear": "",
                "overclaims": "",
                "citation_identity_problems": "",
                "overall_notes": "",
            }
        )
        citation_rows.extend(_human_audit_rows(record, review_id=review_id, report_file=report_file))
        manifest_reports.append(
            {
                "review_id": review_id,
                "task_id": record.get("task_id"),
                "report_type": record.get("report_type"),
                "report_file": report_file,
                "report_sha256": report_digest,
                "source_line_count": len(_sources_lines(report_text)),
            }
        )

    _write_csv(audit_root / "review_queue.csv", queue_rows)
    _write_csv(audit_root / "claim_citation_audit.csv", citation_rows)
    write_json_atomic(
        audit_root / "manifest.json",
        {
            "schema_version": HUMAN_AUDIT_SCHEMA_VERSION,
            "created_at": current_utc_iso(),
            "purpose": "posthoc_human_review_only",
            "report_count": len(manifest_reports),
            "reports": manifest_reports,
        },
    )
    write_text_atomic(
        audit_root / "README.md",
        "\n".join(
            [
                "# RepoBench Human Audit Package",
                "",
                "This package is a post-hoc human review aid. It is not available to the solving agent, does not change a report, and does not change any committee score.",
                "",
                "Review each neutral report file without looking up model or committee outputs. In `claim_citation_audit.csv`, assess whether each visible citation resolves to retrieved evidence, supports the nearby report content, and whether the nearby content overclaims. Use `yes`, `no`, or `unclear` for the three assessment columns. Record aggregate counts and notes in `review_queue.csv`.",
                "",
                "Reports are assigned neutral IDs only. The task prompt is included because a reviewer needs it to assess whether the saved report answers the requested real-world reporting task.",
                "",
            ]
        ),
    )
    return {
        "root": str(audit_root),
        "report_count": len(manifest_reports),
        "review_queue": str(audit_root / "review_queue.csv"),
        "claim_citation_audit": str(audit_root / "claim_citation_audit.csv"),
    }


def _specialist_category_level(assessment: dict[str, Any]) -> int | None:
    return judge_category_level((assessment or {}).get("category"))


def _weighted_level_mean(state: JudgeState) -> float:
    total = Decimal("0")
    total += Decimal(str(SPECIALIST_WEIGHTS["biomedical_content_specialist"])) * Decimal(_specialist_category_level(state.get("content_assessment") or {}) or 0)
    total += Decimal(str(SPECIALIST_WEIGHTS["evidence_support_specialist"])) * Decimal(_specialist_category_level(state.get("evidence_assessment") or {}) or 0)
    total += Decimal(str(SPECIALIST_WEIGHTS["template_compliance_specialist"])) * Decimal(_specialist_category_level(state.get("template_assessment") or {}) or 0)
    total += Decimal(str(SPECIALIST_WEIGHTS["provenance_quality_specialist"])) * Decimal(_specialist_category_level(state.get("provenance_assessment") or {}) or 0)
    return float(total)


def _category_rubric_text() -> str:
    lines = []
    for key in ("unsupported", "weak", "partial", "mostly_supported", "accept"):
        item = judge_category(key) or {}
        lines.append(f"- {item.get('key')} (level {item.get('level')}): {item.get('rubric')}")
    return "\n".join(lines)


def _normalize_assessment_category(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    raw_category = str(normalized.get("category") or "").strip().lower()
    if judge_category(raw_category) is None:
        raise JudgeStructuredOutputError(
            "Judge returned an invalid category. Expected one of: unsupported, weak, partial, mostly_supported, accept."
        )
    normalized["category"] = raw_category
    return normalized


def _has_material_evidence_failure_codes(evidence: dict[str, Any]) -> bool:
    codes = {
        str(item).strip().lower()
        for item in ((evidence or {}).get("failure_codes") or [])
        if str(item).strip()
    }
    if codes.intersection(MATERIAL_EVIDENCE_FAILURE_CODES):
        return True
    return bool((evidence or {}).get("unsupported_claims"))


def _compute_repobench_committee_result(state: JudgeState) -> dict[str, Any]:
    raw_level_mean = _weighted_level_mean(state)
    # The committee score is the continuous weighted mean of the specialist
    # categories. Specialist weaknesses remain audit signals; they must not
    # overwrite the quantitative result after weighting.
    final_level_mean = raw_level_mean
    quality_flags: list[str] = []

    evidence = state.get("evidence_assessment") or {}
    evidence_level = _specialist_category_level(evidence)
    if evidence_level is not None and evidence_level < final_level_mean:
        quality_flags.append(f"evidence_below_weighted_mean:{(evidence or {}).get('category')}")

    template = state.get("template_assessment") or {}
    template_level = _specialist_category_level(template)
    if template_level is not None and template_level <= 1 and final_level_mean > 2.0:
        quality_flags.append(f"template_quality_weak:{(template or {}).get('category')}")

    offline_audit = state.get("offline_provenance_audit") if isinstance(state.get("offline_provenance_audit"), dict) else {}
    offline_issue_codes = {
        str(item).strip()
        for item in (offline_audit.get("issue_codes") or [])
        if str(item).strip()
    }
    offline_audit_status = str(offline_audit.get("status") or "").strip().lower()
    major_audit_issue_count = int(offline_audit.get("major_issue_count") or 0)
    if major_audit_issue_count >= 2:
        quality_flags.append("offline_provenance_repeated_major_issues")
    elif offline_issue_codes.intersection(OFFLINE_PROVENANCE_MAJOR_CODES):
        quality_flags.append("offline_provenance_major_issue")

    committee_category = judge_mean_level_category(final_level_mean) or judge_category("unsupported") or {}
    disagreement_span = judge_category_disagreement_span(
        [
            (state.get("content_assessment") or {}).get("category"),
            (state.get("evidence_assessment") or {}).get("category"),
            (state.get("template_assessment") or {}).get("category"),
            (state.get("provenance_assessment") or {}).get("category"),
        ]
    )
    review_reasons: list[str] = []
    if quality_flags:
        review_reasons.append("quality_flag")
    if disagreement_span is not None and disagreement_span >= 3:
        review_reasons.append("specialist_disagreement_span")
    if _has_material_evidence_failure_codes(state.get("evidence_assessment") or {}):
        review_reasons.append("material_evidence_failure")
    if offline_issue_codes:
        review_reasons.append("offline_provenance_audit")
    if offline_audit_status == "error":
        review_reasons.append("offline_provenance_audit_error")
    return {
        "committee_category": committee_category.get("key"),
        "committee_category_label": committee_category.get("label"),
        "committee_level_mean_raw": raw_level_mean,
        "committee_level_mean_final": final_level_mean,
        "committee_level_mean_normalized": judge_level_mean_normalized(final_level_mean),
        "score_policy": "continuous_weighted_mean_exact_v1",
        "cap_applied": False,
        "cap_reasons": [],
        "quality_flags": quality_flags,
        "specialist_disagreement_span": disagreement_span,
        "review_required": bool(review_reasons),
        "review_reasons": review_reasons,
        "offline_provenance_issue_codes": sorted(offline_issue_codes),
    }


def _arbiter_unavailable_result(
    committee_result: dict[str, Any],
    error: Exception,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Preserve completed specialist scoring when arbiter synthesis is unavailable."""

    updated_result = dict(committee_result)
    updated_result["review_required"] = True
    updated_result["review_reasons"] = sorted(
        set([*(updated_result.get("review_reasons") or []), "arbiter_unavailable"])
    )
    error_text = str(error).strip() or type(error).__name__
    verdict = {
        "status": "unavailable",
        "short_review": "Arbiter synthesis unavailable; review the completed specialist assessments.",
        "rationale": "The numeric committee result was computed from completed specialist assessments. "
        f"The arbiter request did not complete: {error_text}",
        "major_strengths": [],
        "major_issues": ["arbiter_unavailable"],
        "disagreement_flags": [],
        "review_required": True,
        "review_reasons": ["arbiter_unavailable"],
        "error": error_text,
    }
    return verdict, updated_result


def _judge_score_band(score: float | None) -> dict[str, Any] | None:
    return judge_score_band(score)


def _role_prompt(role: str, context_text: str, guidance: str, *, offline_audit: dict[str, Any] | None = None) -> str:
    audit_block = ""
    if isinstance(offline_audit, dict) and offline_audit:
        audit_block = (
            "Deterministic offline provenance audit "
            "(computed locally from the saved report and saved session artifacts; no internet lookup):\n"
            f"{compact_json(_prompt_friendly_offline_audit(offline_audit), 3000)}\n\n"
        )
    return (
        f"You are the {role} in the RepoBench report-quality judge committee.\n\n"
        f"{guidance}\n\n"
        "Use the final saved report as the primary object of evaluation. "
        "Use the visible transcript, tool provenance, and deterministic workflow summary only as audit evidence.\n\n"
        "Use exactly one category from: unsupported, weak, partial, mostly_supported, accept.\n\n"
        "Shared category rubric:\n"
        f"{_category_rubric_text()}\n\n"
        "Use a small failure-code vocabulary such as: unsupported_claim, missing_core_answer, trace_mismatch, contradiction, overclaim, "
        "template_mismatch, provenance_gap, evidence_gap, citation_identity_mismatch, citation_title_mismatch, "
        "source_not_in_artifacts, artifact_cited_not_linked.\n\n"
        "Keep every returned list to at most six concise items.\n\n"
        f"{audit_block}"
        "Do not reveal hidden reasoning. Return structured output only.\n\n"
        "Context:\n"
        f"{context_text}\n"
    )


def _arbiter_task_context(record: dict[str, Any]) -> str:
    """Provide arbiter identity context without resending the full report.

    The arbiter synthesizes completed specialist assessments; it does not
    independently score or re-read the report. Keeping this payload compact
    avoids a redundant long structured-output request.
    """

    return compact_json(
        {
            "task_id": record.get("task_id"),
            "report_type": record.get("report_type"),
            "template_requested": record.get("template_id_requested"),
            "title": record.get("title"),
            "report_saved": bool(record.get("report_saved")),
        },
        800,
    )


class RepoBenchJudgeCommittee:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        model_map: dict[str, str],
        api_key: str,
        progress: JudgeProgressTracker | None = None,
    ) -> None:
        _require_dependencies()
        self.provider = _normalize_judge_provider(provider)
        self.base_url = str(base_url).strip()
        self.model_map = dict(model_map)
        self.api_key = api_key
        self.progress = progress
        graph: StateGraph[JudgeState] = StateGraph(JudgeState)
        graph.add_node("prepare", self._prepare)
        graph.add_node("biomedical_content_specialist", self._biomedical_content_specialist)
        graph.add_node("evidence_support_specialist", self._evidence_support_specialist)
        graph.add_node("template_compliance_specialist", self._template_compliance_specialist)
        graph.add_node("provenance_quality_specialist", self._provenance_quality_specialist)
        graph.add_node("arbiter", self._arbiter)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "biomedical_content_specialist")
        graph.add_edge("biomedical_content_specialist", "evidence_support_specialist")
        graph.add_edge("evidence_support_specialist", "template_compliance_specialist")
        graph.add_edge("template_compliance_specialist", "provenance_quality_specialist")
        graph.add_edge("provenance_quality_specialist", "arbiter")
        graph.add_edge("arbiter", END)
        self._graph = graph.compile()

    async def ainvoke(self, state: JudgeState) -> dict[str, Any]:
        return await self._graph.ainvoke(state)

    def _task_id(self, state: JudgeState) -> str:
        selection_row = state.get("selection_row") if isinstance(state.get("selection_row"), dict) else {}
        episode_record = state.get("episode_record") if isinstance(state.get("episode_record"), dict) else {}
        return str(selection_row.get("task_id") or episode_record.get("task_id") or Path(str(state.get("episode_root") or "")).name)

    def _start_stage(self, state: JudgeState, stage_name: str, model_role: str | None, detail: str | None = None) -> tuple[str, float, str | None]:
        task_id = self._task_id(state)
        model_name = state["model_map"].get(model_role, DEFAULT_MODEL) if model_role else None
        if self.progress is not None:
            self.progress.start_stage(task_id, stage_name, model=model_name, detail=detail)
        return task_id, time.perf_counter(), model_name

    def _complete_stage(self, task_id: str, stage_name: str, started_at: float, detail: str | None = None) -> None:
        if self.progress is not None:
            self.progress.complete_stage(task_id, stage_name, elapsed_seconds=max(0.0, time.perf_counter() - started_at), detail=detail)

    def _fail_stage(self, task_id: str, stage_name: str, started_at: float, error: Exception) -> None:
        if self.progress is not None:
            self.progress.fail_stage(
                task_id,
                stage_name,
                elapsed_seconds=max(0.0, time.perf_counter() - started_at),
                error=str(error).strip() or type(error).__name__,
            )

    def _retry_stage(self, task_id: str, stage_name: str):
        if self.progress is None:
            return None

        def _callback(attempt: int, max_retries: int, exc: Exception, delay_seconds: float) -> None:
            self.progress.retry_stage(
                task_id,
                stage_name,
                attempt=attempt,
                max_retries=max_retries,
                delay_seconds=delay_seconds,
                error=str(exc).strip() or type(exc).__name__,
            )

        return _callback

    async def _prepare(self, state: JudgeState) -> dict[str, Any]:
        task_id, started_at, _ = self._start_stage(state, "prepare", None, "load episode bundle")
        try:
            episode_root = Path(state["episode_root"])
            record = load_episode_record(episode_root)
            offline_audit = _offline_provenance_audit(Path(state["run_root"]), record)
            payload = {
                "episode_record": record,
                "prompt_context": _context_text(record),
                "offline_provenance_audit": offline_audit,
                "report_freeze_verification": _verify_report_freeze(episode_root, record),
                "model_map": dict(state.get("model_map") or self.model_map),
            }
        except Exception as exc:
            self._fail_stage(task_id, "prepare", started_at, exc)
            raise
        self._complete_stage(task_id, "prepare", started_at, "episode loaded")
        return payload

    async def _biomedical_content_specialist(self, state: JudgeState) -> dict[str, Any]:
        task_id, started_at, model_name = self._start_stage(state, "biomedical_content_specialist", "biomedical_content_specialist")
        prompt = _role_prompt(
            "biomedical content specialist",
            state["prompt_context"],
            "Judge whether the report captures the correct biological interpretation and useful synthesis for the task. "
            "Reward reports that answer the task directly, balance caveats, and stay scientifically useful. "
            "Do not penalize concise reports if they are correct and useful. "
            "Choose accept only when the saved report is biologically correct, useful, and well grounded. "
            "Choose mostly_supported when the report is broadly correct with non-fatal caveats. "
            "Choose partial when some useful synthesis is present but important biological interpretation is missing or distorted. "
            "Choose weak when the biological interpretation is substantially flawed. "
            "Choose unsupported when the core interpretation is materially wrong or unsupported. "
            "Use failure codes like: biological_misinterpretation, unsupported_claim, overclaim, missing_core_answer.",
        )
        try:
            result = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("biomedical_content_specialist", DEFAULT_MODEL),
                schema=ReportSpecialistAssessment,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(task_id, "biomedical_content_specialist"),
                max_tokens=SPECIALIST_MAX_OUTPUT_TOKENS,
            )
            result = _normalize_assessment_category(result)
        except Exception as exc:
            self._fail_stage(task_id, "biomedical_content_specialist", started_at, exc)
            raise
        self._complete_stage(task_id, "biomedical_content_specialist", started_at, f"category={result.get('category')}")
        return {"content_assessment": result}

    async def _evidence_support_specialist(self, state: JudgeState) -> dict[str, Any]:
        task_id, started_at, model_name = self._start_stage(state, "evidence_support_specialist", "evidence_support_specialist")
        prompt = _role_prompt(
            "evidence support specialist",
            state["prompt_context"],
            "Judge whether the saved report's claims are actually supported by the visible session evidence. "
            "This is the main grounding guard. Reward evidence-grounded reporting and penalize unsupported claims, "
            "overclaims, or references to facts that do not appear in the visible audit trace. "
            "Treat the deterministic offline provenance audit as hard local evidence about citation identity. "
            "If the audit reports citation_title_mismatch, citation_identity_mismatch, or source_not_in_artifacts for material claims, "
            "that is a grounding failure, not a cosmetic style issue. "
            "Choose accept only when the visible evidence directly supports the report's core claims. "
            "Choose mostly_supported when support is strong but not exhaustive. "
            "Choose partial when the report is only partly supported or leaves important grounding gaps. "
            "Choose weak when support is thin and the report overreaches. "
            "Choose unsupported when the report is contradicted, invented, or substantially unsupported. "
            "Use failure codes like: unsupported_claim, trace_mismatch, contradiction, overclaim, missing_trace_support, evidence_gap, "
            "citation_identity_mismatch, citation_title_mismatch, source_not_in_artifacts.",
            offline_audit=state.get("offline_provenance_audit"),
        )
        try:
            result = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("evidence_support_specialist", DEFAULT_MODEL),
                schema=EvidenceSupportAssessment,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(task_id, "evidence_support_specialist"),
                max_tokens=SPECIALIST_MAX_OUTPUT_TOKENS,
            )
            result = _normalize_assessment_category(result)
        except Exception as exc:
            self._fail_stage(task_id, "evidence_support_specialist", started_at, exc)
            raise
        self._complete_stage(task_id, "evidence_support_specialist", started_at, f"category={result.get('category')}")
        return {"evidence_assessment": result}

    async def _template_compliance_specialist(self, state: JudgeState) -> dict[str, Any]:
        task_id, started_at, model_name = self._start_stage(state, "template_compliance_specialist", "template_compliance_specialist")
        prompt = _role_prompt(
            "template compliance specialist",
            state["prompt_context"],
            "Judge whether the report satisfies the requested report type, structure, and task objective. "
            "Focus on whether the saved report behaves like the requested template family, not on raw length. "
            "Choose accept only when the report clearly satisfies the requested report type and structure. "
            "Choose mostly_supported when the template behavior is strong with only modest omissions. "
            "Choose partial when some structure is correct but important required sections or behaviors are missing. "
            "Choose weak when the requested template is poorly followed. "
            "Choose unsupported when the output does not function as the requested report type. "
            "Use failure codes like: template_mismatch, missing_required_section, missing_core_answer.",
        )
        try:
            result = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("template_compliance_specialist", DEFAULT_MODEL),
                schema=ReportSpecialistAssessment,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(task_id, "template_compliance_specialist"),
                max_tokens=SPECIALIST_MAX_OUTPUT_TOKENS,
            )
            result = _normalize_assessment_category(result)
        except Exception as exc:
            self._fail_stage(task_id, "template_compliance_specialist", started_at, exc)
            raise
        self._complete_stage(task_id, "template_compliance_specialist", started_at, f"category={result.get('category')}")
        return {"template_assessment": result}

    async def _provenance_quality_specialist(self, state: JudgeState) -> dict[str, Any]:
        task_id, started_at, model_name = self._start_stage(state, "provenance_quality_specialist", "provenance_quality_specialist")
        prompt = _role_prompt(
            "provenance quality specialist",
            state["prompt_context"],
            "Judge whether the report is reader-auditable with visible sources, clear attribution, and usable evidence presentation. "
            "Reward reports that make their grounding easy for a reviewer to inspect. "
            "Use the deterministic offline provenance audit as the authoritative local check for whether cited papers and artifact ids match the saved session artifacts. "
            "Do not assume internet access and do not excuse rewritten or invented paper titles when the saved artifact metadata disagrees. "
            "Choose accept only when provenance is clear, visible, and genuinely useful to a reviewer. "
            "Choose mostly_supported when attribution is strong with only modest gaps. "
            "Choose partial when provenance exists but remains uneven or incomplete. "
            "Choose weak when attribution is hard to audit. "
            "Choose unsupported when the report lacks meaningful reader-facing provenance. "
            "Use failure codes like: provenance_gap, missing_sources_line, unsupported_claim, evidence_gap, citation_identity_mismatch, "
            "citation_title_mismatch, source_not_in_artifacts, artifact_cited_not_linked.",
            offline_audit=state.get("offline_provenance_audit"),
        )
        try:
            result = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("provenance_quality_specialist", DEFAULT_MODEL),
                schema=ReportSpecialistAssessment,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(task_id, "provenance_quality_specialist"),
                max_tokens=SPECIALIST_MAX_OUTPUT_TOKENS,
            )
            result = _normalize_assessment_category(result)
        except Exception as exc:
            self._fail_stage(task_id, "provenance_quality_specialist", started_at, exc)
            raise
        self._complete_stage(task_id, "provenance_quality_specialist", started_at, f"category={result.get('category')}")
        return {"provenance_assessment": result}

    async def _arbiter(self, state: JudgeState) -> dict[str, Any]:
        task_id, started_at, model_name = self._start_stage(state, "arbiter", "arbiter")
        committee_result = _compute_repobench_committee_result(state)
        prompt = (
            "You are the final arbiter in the RepoBench report-quality judge committee.\n\n"
            "Do not output the official category or official numeric score. Those are computed separately in code. "
            "Your job is qualitative synthesis only.\n\n"
            f"Deterministic committee summary: {compact_json(committee_result, 400)}\n"
            f"Offline provenance audit: {compact_json(_prompt_friendly_offline_audit(state.get('offline_provenance_audit') or {}), 2500)}\n"
            f"Specialist weights: {compact_json(SPECIALIST_WEIGHTS, 200)}\n"
            f"Biomedical content assessment: {compact_json(state.get('content_assessment') or {}, 3000)}\n"
            f"Evidence support assessment: {compact_json(state.get('evidence_assessment') or {}, 3000)}\n"
            f"Template compliance assessment: {compact_json(state.get('template_assessment') or {}, 3000)}\n"
            f"Provenance quality assessment: {compact_json(state.get('provenance_assessment') or {}, 3000)}\n"
            f"Task identity: {_arbiter_task_context(state.get('episode_record') or {})}\n"
        )
        try:
            result = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("arbiter", DEFAULT_MODEL),
                schema=ArbiterVerdict,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(task_id, "arbiter"),
                max_tokens=ARBITER_MAX_OUTPUT_TOKENS,
            )
        except Exception as exc:
            self._fail_stage(task_id, "arbiter", started_at, exc)
            result, committee_result = _arbiter_unavailable_result(committee_result, exc)
            return {"arbiter_verdict": result, "committee_result": committee_result}
        if committee_result.get("review_required") and not result.get("review_required"):
            result["review_required"] = True
            result["review_reasons"] = sorted(
                set((result.get("review_reasons") or []) + list(committee_result.get("review_reasons") or []))
            )
        self._complete_stage(task_id, "arbiter", started_at, f"review_required={result.get('review_required')}")
        return {"arbiter_verdict": result, "committee_result": committee_result}


@dataclass(frozen=True)
class RepoBenchJudgeConfig:
    run_root: Path
    scope: str = "all"
    task_ids: tuple[str, ...] = ()
    report_type: str | None = None
    limit: int = 0
    resume: bool = False
    timeout_seconds: float = 180.0
    provider: str = DEFAULT_JUDGE_PROVIDER
    base_url: str | None = None
    model: str = DEFAULT_MODEL
    content_model: str | None = None
    evidence_model: str | None = None
    template_model: str | None = None
    provenance_model: str | None = None
    arbiter_model: str | None = None
    verbose: bool = False

    @property
    def model_map(self) -> dict[str, str]:
        default = self.model.strip() or DEFAULT_MODEL
        return {
            "biomedical_content_specialist": self.content_model.strip() if self.content_model else default,
            "evidence_support_specialist": self.evidence_model.strip() if self.evidence_model else default,
            "template_compliance_specialist": self.template_model.strip() if self.template_model else default,
            "provenance_quality_specialist": self.provenance_model.strip() if self.provenance_model else default,
            "arbiter": self.arbiter_model.strip() if self.arbiter_model else default,
        }

    @property
    def resolved_model_map(self) -> dict[str, str]:
        return {
            role: _resolve_model_name_for_provider(model_name, provider=self.provider)
            for role, model_name in self.model_map.items()
        }

    @property
    def resolved_provider(self) -> str:
        return _normalize_judge_provider(self.provider)

    @property
    def resolved_base_url(self) -> str:
        _api_key, resolved_base_url = _resolve_judge_provider_credentials(
            provider=self.provider,
            base_url=self.base_url,
            project_root=Path(self.run_root),
        )
        return resolved_base_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the RepoBench LangGraph judge committee over an existing run.")
    parser.add_argument("run_root", help="Existing RepoBench run root to judge.")
    parser.add_argument(
        "--recompute-existing-scores",
        action="store_true",
        help="Recompute saved scored verdicts from their specialist categories without calling a provider.",
    )
    parser.add_argument("--task-id", action="append", default=[], help="Judge one exact RepoBench task id.")
    parser.add_argument("--scope", default="all", help="RepoBench scope filter. Use 'all' for every selected task.")
    parser.add_argument("--report-type", default="", help="Optional report_type filter.")
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of selected tasks.")
    parser.add_argument("--resume", action="store_true", help="Skip tasks that already have judge.json.")
    parser.add_argument("--timeout-seconds", type=float, default=180.0, help="Timeout per judged task in seconds.")
    parser.add_argument("--provider", default=DEFAULT_JUDGE_PROVIDER, help="Judge provider (`openai` or `openrouter`).")
    parser.add_argument("--base-url", default="", help="Optional provider base URL override.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Default judge model (default: {DEFAULT_MODEL}).")
    parser.add_argument("--content-model", default="", help="Override model for biomedical_content_specialist.")
    parser.add_argument("--evidence-model", default="", help="Override model for evidence_support_specialist.")
    parser.add_argument("--template-model", default="", help="Override model for template_compliance_specialist.")
    parser.add_argument("--provenance-model", default="", help="Override model for provenance_quality_specialist.")
    parser.add_argument("--arbiter-model", default="", help="Override model for arbiter.")
    parser.add_argument("--verbose", action="store_true", help="Print progress information.")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _selected_rows_for_run(config: RepoBenchJudgeConfig) -> list[dict[str, Any]]:
    run_root = Path(config.run_root)
    manifest = read_json(run_root / "run_manifest.json")
    project_root = Path(str(manifest.get("project_root") or run_root.parents[3]))
    _dataset_path, rows = load_repobench_rows(project_root)
    selected = select_repobench_rows(
        rows,
        task_ids=config.task_ids,
        scope=config.scope,
        report_type=config.report_type,
    )
    if config.limit > 0:
        selected = selected[: config.limit]
    return selected


async def judge_repobench_run_async(config: RepoBenchJudgeConfig) -> Path:
    _require_dependencies()
    api_key, resolved_base_url = _resolve_judge_provider_credentials(
        provider=config.provider,
        base_url=config.base_url,
        project_root=Path(config.run_root),
    )
    run_root = Path(config.run_root)
    selected_rows = _selected_rows_for_run(config)
    summary_root = run_root / "summary"
    summary_root.mkdir(parents=True, exist_ok=True)
    judge_root = summary_root / "judge"
    judge_root.mkdir(parents=True, exist_ok=True)
    progress = JudgeProgressTracker(
        summary_root=judge_root,
        total_questions=len(selected_rows),
        run_label=f"RepoBench judge {run_root.name}",
        verbose=config.verbose,
    )
    committee = RepoBenchJudgeCommittee(
        provider=config.provider,
        base_url=resolved_base_url,
        model_map=config.resolved_model_map,
        api_key=api_key,
        progress=progress,
    )
    manifest_payload = {
        "created_at": current_utc_iso(),
        "run_root": str(run_root),
        "provider": config.resolved_provider,
        "base_url": resolved_base_url,
        "model_map": config.model_map,
        "resolved_model_map": config.resolved_model_map,
        "weights": SPECIALIST_WEIGHTS,
        "selection": {
            "scope": config.scope,
            "task_ids": list(config.task_ids),
            "report_type": config.report_type,
            "limit": config.limit,
            "resume": config.resume,
        },
        "outputs": {
            "progress_path": str(judge_root / "progress.json"),
            "human_audit_root": str(summary_root / "human_audit"),
        },
    }
    write_json_atomic(judge_root / "judge_manifest.json", manifest_payload)

    judged_task_ids: list[str] = []
    for index, row in enumerate(selected_rows, start=1):
        task_id = str(row.get("task_id") or "").strip()
        episode_root = run_root / "episodes" / task_id
        if not episode_root.exists():
            continue
        question_started_at = time.perf_counter()
        progress.start_question(task_id, episode_root=episode_root, index=index, total=len(selected_rows))
        record = load_episode_record(episode_root)
        if not bool(record.get("report_saved")):
            progress.skip_question(task_id, episode_root=episode_root, reason="no_saved_report")
            continue
        judge_path = episode_root / "judge.json"
        if config.resume and judge_path.exists():
            judged_task_ids.append(task_id)
            progress.skip_question(task_id, episode_root=episode_root, reason="resume_existing_judge")
            continue
        try:
            state = await asyncio.wait_for(
                committee.ainvoke(
                    {
                        "run_root": str(run_root),
                        "episode_root": str(episode_root),
                        "selection_row": row,
                        "model_map": config.resolved_model_map,
                    }
                ),
                timeout=config.timeout_seconds,
            )
        except Exception as exc:
            progress.finish_question(
                task_id,
                status="failed",
                started_at_monotonic=question_started_at,
                note=str(exc).strip() or type(exc).__name__,
            )
            raise
        committee_result = dict(state.get("committee_result") or _compute_repobench_committee_result(state))
        committee_level_mean_final = committee_result.get("committee_level_mean_final")
        payload = {
            "judge_schema_version": JUDGE_SCHEMA_VERSION,
            "created_at": current_utc_iso(),
            "task_id": task_id,
            "report_type": record.get("report_type"),
            "provider": config.resolved_provider,
            "base_url": resolved_base_url,
            "weights": SPECIALIST_WEIGHTS,
            "model_map": config.model_map,
            "resolved_model_map": config.resolved_model_map,
            "status": "scored",
            "committee_category": committee_result.get("committee_category"),
            "committee_category_label": committee_result.get("committee_category_label"),
            "committee_level_mean_raw": committee_result.get("committee_level_mean_raw"),
            "committee_level_mean_final": committee_level_mean_final,
            "committee_level_mean_normalized": committee_result.get("committee_level_mean_normalized"),
            "score_policy": committee_result.get("score_policy"),
            "cap_applied": committee_result.get("cap_applied"),
            "cap_reasons": committee_result.get("cap_reasons") or [],
            "quality_flags": committee_result.get("quality_flags") or [],
            "review_required": committee_result.get("review_required"),
            "review_reasons": committee_result.get("review_reasons") or [],
            "specialist_disagreement_span": committee_result.get("specialist_disagreement_span"),
            "offline_provenance_issue_codes": committee_result.get("offline_provenance_issue_codes") or [],
            "offline_provenance_audit": state.get("offline_provenance_audit") or {},
            "report_freeze_verification": state.get("report_freeze_verification") or {},
            "judge_score": judge_level_mean_normalized(None if committee_level_mean_final is None else float(committee_level_mean_final)),
            "judge": state.get("arbiter_verdict") or {},
            "specialists": {
                "biomedical_content_specialist": state.get("content_assessment") or {},
                "evidence_support_specialist": state.get("evidence_assessment") or {},
                "template_compliance_specialist": state.get("template_assessment") or {},
                "provenance_quality_specialist": state.get("provenance_assessment") or {},
            },
        }
        write_json_atomic(judge_path, payload)
        judged_task_ids.append(task_id)
        progress.finish_question(task_id, status="completed", started_at_monotonic=question_started_at)

    records: list[dict[str, Any]] = []
    for episode_path in sorted((run_root / "episodes").glob("*/episode.json")):
        record = read_json(episode_path)
        if not isinstance(record, dict):
            continue
        judge_path = episode_path.parent / "judge.json"
        if judge_path.exists():
            record["judge"] = read_json(judge_path)
        records.append(record)
    summary = write_summary_artifacts(run_root, records)
    run_manifest = read_json(run_root / "run_manifest.json")
    write_reports(run_root, run_manifest, records, summary)
    human_audit = write_human_audit_package(summary_root, records)

    judge_rows: list[dict[str, Any]] = []
    for record in records:
        judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
        judge_summary = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
        if not judge_payload:
            continue
        specialists = judge_payload.get("specialists") if isinstance(judge_payload.get("specialists"), dict) else {}
        judge_rows.append(
            {
                "task_id": record.get("task_id"),
                "report_type": record.get("report_type"),
                "scope": record.get("scope"),
                "committee_category": judge_payload.get("committee_category"),
                "committee_level_mean_final": judge_payload.get("committee_level_mean_final"),
                "review_required": judge_payload.get("review_required"),
                "content_category": (specialists.get("biomedical_content_specialist") or {}).get("category"),
                "evidence_category": (specialists.get("evidence_support_specialist") or {}).get("category"),
                "template_category": (specialists.get("template_compliance_specialist") or {}).get("category"),
                "provenance_category": (specialists.get("provenance_quality_specialist") or {}).get("category"),
            }
        )
    _write_csv(judge_root / "judge_metrics.csv", judge_rows)
    by_report_type = _group_mean(judge_rows, "report_type")
    by_scope = _group_mean(judge_rows, "scope")
    _write_csv(judge_root / "judge_by_report_type.csv", by_report_type)
    _write_csv(judge_root / "judge_by_scope.csv", by_scope)
    write_json_atomic(judge_root / "judge_metrics.json", judge_rows)
    write_json_atomic(judge_root / "judge_by_report_type.json", by_report_type)
    write_json_atomic(judge_root / "judge_by_scope.json", by_scope)
    write_text_atomic(
        judge_root / "report.md",
        "\n".join(
            [
                f"# RepoBench Judge Report",
                "",
                f"- judged tasks: {len(judge_rows)}",
                f"- mean committee level (0-4): {mean([float(row['committee_level_mean_final']) for row in judge_rows if row.get('committee_level_mean_final') is not None]) if judge_rows else 'n/a'}",
                f"- provider: {config.resolved_provider}",
                f"- model: {config.model}",
                f"- non-scoring human audit reports: {human_audit['report_count']}",
                "",
                "See `judge_metrics.csv`, `../human_audit/`, and the regenerated `summary/report.html` for the full view.",
            ]
        )
        + "\n",
    )
    progress.finalize(status="completed")
    return run_root


def _group_mean(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        token = str(row.get(key) or "").strip()
        groups.setdefault(token, []).append(row)
    output: list[dict[str, Any]] = []
    for token, bucket in sorted(groups.items()):
        scores = [float(row.get("committee_level_mean_final")) for row in bucket if row.get("committee_level_mean_final") is not None]
        output.append(
            {
                key: token,
                "task_count": len(bucket),
                "committee_level_mean_final": mean(scores) if scores else None,
            }
        )
    return output


def _state_from_saved_judge_payload(payload: dict[str, Any]) -> JudgeState | None:
    """Reconstruct score inputs from a completed judge artifact without an LLM call."""

    specialists = payload.get("specialists") if isinstance(payload.get("specialists"), dict) else {}
    role_map = {
        "content_assessment": "biomedical_content_specialist",
        "evidence_assessment": "evidence_support_specialist",
        "template_assessment": "template_compliance_specialist",
        "provenance_assessment": "provenance_quality_specialist",
    }
    state: JudgeState = {}
    for state_key, specialist_key in role_map.items():
        assessment = specialists.get(specialist_key)
        if not isinstance(assessment, dict) or judge_category(assessment.get("category")) is None:
            return None
        state[state_key] = dict(assessment)
    offline_audit = payload.get("offline_provenance_audit")
    if isinstance(offline_audit, dict):
        state["offline_provenance_audit"] = dict(offline_audit)
    return state


def _refresh_repobench_judge_outputs(run_root: Path) -> dict[str, int]:
    """Refresh run summaries after a local judge-artifact migration."""

    records: list[dict[str, Any]] = []
    for episode_path in sorted((run_root / "episodes").glob("*/episode.json")):
        record = read_json(episode_path)
        if not isinstance(record, dict):
            continue
        judge_path = episode_path.parent / "judge.json"
        if judge_path.exists():
            judge_payload = read_json(judge_path)
            if isinstance(judge_payload, dict):
                record["judge"] = judge_payload
        records.append(record)

    summary = write_summary_artifacts(run_root, records)
    run_manifest = read_json(run_root / "run_manifest.json")
    write_reports(run_root, run_manifest, records, summary)
    summary_root = run_root / "summary"
    judge_root = summary_root / "judge"
    judge_root.mkdir(parents=True, exist_ok=True)
    human_audit = write_human_audit_package(summary_root, records)

    judge_rows: list[dict[str, Any]] = []
    for record in records:
        judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
        if not judge_payload:
            continue
        specialists = judge_payload.get("specialists") if isinstance(judge_payload.get("specialists"), dict) else {}
        judge_rows.append(
            {
                "task_id": record.get("task_id"),
                "report_type": record.get("report_type"),
                "scope": record.get("scope"),
                "committee_category": judge_payload.get("committee_category"),
                "committee_level_mean_final": judge_payload.get("committee_level_mean_final"),
                "committee_level_mean_normalized": judge_payload.get("committee_level_mean_normalized"),
                "quality_flags": judge_payload.get("quality_flags") or [],
                "review_required": judge_payload.get("review_required"),
                "content_category": (specialists.get("biomedical_content_specialist") or {}).get("category"),
                "evidence_category": (specialists.get("evidence_support_specialist") or {}).get("category"),
                "template_category": (specialists.get("template_compliance_specialist") or {}).get("category"),
                "provenance_category": (specialists.get("provenance_quality_specialist") or {}).get("category"),
            }
        )
    _write_csv(judge_root / "judge_metrics.csv", judge_rows)
    by_report_type = _group_mean(judge_rows, "report_type")
    by_scope = _group_mean(judge_rows, "scope")
    _write_csv(judge_root / "judge_by_report_type.csv", by_report_type)
    _write_csv(judge_root / "judge_by_scope.csv", by_scope)
    write_json_atomic(judge_root / "judge_metrics.json", judge_rows)
    write_json_atomic(judge_root / "judge_by_report_type.json", by_report_type)
    write_json_atomic(judge_root / "judge_by_scope.json", by_scope)
    write_text_atomic(
        judge_root / "report.md",
        "\n".join(
            [
                "# RepoBench Judge Report",
                "",
                f"- judged tasks: {len(judge_rows)}",
                f"- mean committee level (0-4): {mean([float(row['committee_level_mean_final']) for row in judge_rows if row.get('committee_level_mean_final') is not None]) if judge_rows else 'n/a'}",
                "- score policy: continuous weighted mean of specialist category levels; quality flags do not modify the score",
                f"- non-scoring human audit reports: {human_audit['report_count']}",
                "",
                "See `judge_metrics.csv`, `../human_audit/`, and the regenerated `summary/report.html` for the full view.",
            ]
        )
        + "\n",
    )
    return {"episode_count": len(records), "judged_count": len(judge_rows)}


def recompute_repobench_scores(run_root: Path, *, task_ids: tuple[str, ...] = ()) -> dict[str, int]:
    """Migrate saved scored RepoBench verdicts to the continuous score policy.

    This is deliberately local-only: it reads existing specialist outputs and
    rewrites derived score fields without re-contacting a judge provider.
    """

    requested = {str(task_id).strip() for task_id in task_ids if str(task_id).strip()}
    updated_count = 0
    skipped_count = 0
    for judge_path in sorted((run_root / "episodes").glob("*/judge.json")):
        task_id = judge_path.parent.name
        if requested and task_id not in requested:
            continue
        payload = read_json(judge_path)
        if not isinstance(payload, dict) or payload.get("status") != "scored":
            skipped_count += 1
            continue
        state = _state_from_saved_judge_payload(payload)
        if state is None:
            skipped_count += 1
            continue
        committee_result = _compute_repobench_committee_result(state)
        prior_schema_version = payload.get("judge_schema_version")
        arbiter = payload.get("judge") if isinstance(payload.get("judge"), dict) else {}
        arbiter = dict(arbiter)
        legacy_reasons = [
            str(reason).strip()
            for reason in (arbiter.get("review_reasons") or [])
            if str(reason).strip() and str(reason).strip() != "cap_applied"
        ]
        arbiter["review_required"] = bool(committee_result.get("review_required"))
        arbiter["review_reasons"] = sorted(
            set(legacy_reasons + list(committee_result.get("review_reasons") or []))
        )
        payload.update(
            {
                "judge_schema_version": JUDGE_SCHEMA_VERSION,
                "committee_category": committee_result.get("committee_category"),
                "committee_category_label": committee_result.get("committee_category_label"),
                "committee_level_mean_raw": committee_result.get("committee_level_mean_raw"),
                "committee_level_mean_final": committee_result.get("committee_level_mean_final"),
                "committee_level_mean_normalized": committee_result.get("committee_level_mean_normalized"),
                "judge_score": committee_result.get("committee_level_mean_normalized"),
                "score_policy": committee_result.get("score_policy"),
                "cap_applied": False,
                "cap_reasons": [],
                "quality_flags": committee_result.get("quality_flags") or [],
                "review_required": committee_result.get("review_required"),
                "review_reasons": committee_result.get("review_reasons") or [],
                "specialist_disagreement_span": committee_result.get("specialist_disagreement_span"),
                "offline_provenance_issue_codes": committee_result.get("offline_provenance_issue_codes") or [],
                "judge": arbiter,
                "score_recomputation": {
                    "recomputed_at": current_utc_iso(),
                    "previous_judge_schema_version": prior_schema_version,
                    "method": "exact_continuous_weighted_mean_from_saved_specialists",
                    "provider_called": False,
                    "arbiter_text_note": "Arbiter prose was retained from the legacy score policy; only derived numeric score and review fields were recomputed.",
                },
            }
        )
        write_json_atomic(judge_path, payload)
        updated_count += 1

    output_counts = _refresh_repobench_judge_outputs(run_root)
    return {"updated_count": updated_count, "skipped_count": skipped_count, **output_counts}


def judge_repobench_run(config: RepoBenchJudgeConfig) -> Path:
    return asyncio.run(judge_repobench_run_async(config))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.recompute_existing_scores:
        result = recompute_repobench_scores(
            Path(args.run_root).resolve(),
            task_ids=tuple(str(item).strip() for item in args.task_id if str(item).strip()),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    config = RepoBenchJudgeConfig(
        run_root=Path(args.run_root).resolve(),
        scope=str(args.scope or "all"),
        task_ids=tuple(str(item).strip() for item in args.task_id if str(item).strip()),
        report_type=str(args.report_type or "").strip() or None,
        limit=max(0, int(args.limit or 0)),
        resume=bool(args.resume),
        timeout_seconds=max(1.0, float(args.timeout_seconds or 180.0)),
        provider=str(args.provider or DEFAULT_JUDGE_PROVIDER).strip() or DEFAULT_JUDGE_PROVIDER,
        base_url=str(args.base_url or "").strip() or None,
        model=str(args.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        content_model=str(args.content_model or "").strip() or None,
        evidence_model=str(args.evidence_model or "").strip() or None,
        template_model=str(args.template_model or "").strip() or None,
        provenance_model=str(args.provenance_model or "").strip() or None,
        arbiter_model=str(args.arbiter_model or "").strip() or None,
        verbose=bool(args.verbose),
    )
    run_root = judge_repobench_run(config)
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
