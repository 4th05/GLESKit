from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from gleskit_mcp.eval.config import EvalCase, load_report_type_specs
from gleskit_mcp.eval.runtime import compact_json, count_words
from gleskit_mcp.store import WorkspaceStore

try:
    from langchain_openai import ChatOpenAI
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover - optional dependency
    ChatOpenAI = None
    BaseModel = object
    Field = None

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - optional dependency
    fuzz = None


VISIBLE_SOURCES_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?\s*Sources\s*:\s*(?:\*\*|__)?\s*(?P<body>.*)$",
    re.IGNORECASE,
)


def mean_nugget_text_overlap(report_text: str, nuggets: list[str]) -> float:
    if not report_text or not nuggets or fuzz is None:
        return 0.0
    scores = [fuzz.partial_ratio(nugget, report_text) / 100 for nugget in nuggets]
    return round(sum(scores) / len(scores), 3)


def load_run_manifest(run_root: Path) -> dict[str, Any]:
    return json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))


def load_episode_payloads(run_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((run_root / "episodes").glob("*/episode.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    return rows


def build_artifact_details(workspace_root: Path, session_id: str, *, limit: int = 6) -> list[dict[str, Any]]:
    store = WorkspaceStore(workspace_root)
    artifact_details: list[dict[str, Any]] = []
    for artifact in store.list_artifacts(session_id)[:limit]:
        artifact_details.append(
            {
                "artifact_id": artifact.artifact_id,
                "kind": artifact.kind,
                "title": artifact.title,
                "summary": compact_json(artifact.summary or artifact.payload, 1600),
            }
        )
    return artifact_details


def normalize_visible_sources_line(line: str) -> str | None:
    """Normalize common Markdown emphasis around a reader-facing Sources line."""

    match = VISIBLE_SOURCES_LINE_RE.match(str(line or ""))
    if match is None:
        return None
    body = str(match.group("body") or "").strip()
    return f"Sources: {body}" if body else "Sources:"


def visible_sources_blocks(report_text: str) -> list[tuple[int, str]]:
    """Return normalized visible source blocks, including Markdown bullet lists."""

    raw_lines = str(report_text or "").splitlines()
    blocks: list[tuple[int, str]] = []
    for index, raw_line in enumerate(raw_lines):
        normalized = normalize_visible_sources_line(raw_line)
        if normalized is None:
            continue
        body = normalized[len("Sources:"):].strip()
        if not body:
            continuation: list[str] = []
            for following_line in raw_lines[index + 1:]:
                stripped = following_line.strip()
                if not stripped:
                    break
                if stripped.startswith("#") or normalize_visible_sources_line(stripped) is not None:
                    break
                bullet = re.match(r"^[-*+]\s+(?P<body>.+)$", stripped)
                if bullet is None:
                    break
                continuation.append(str(bullet.group("body") or "").strip())
            if continuation:
                normalized = f"Sources: {'; '.join(continuation)}"
        blocks.append((index + 1, normalized))
    return blocks


def visible_sources_lines(report_text: str) -> list[str]:
    return [line for _line_number, line in visible_sources_blocks(report_text)]


def visible_sources_summary(report_text: str) -> dict[str, Any]:
    lines = visible_sources_lines(report_text)
    human_friendly_source_tokens = (
        "reactome",
        "go biological process",
        "go:bp",
        "europe pmc",
        "open targets",
        "gtex",
        "expression atlas",
        "pubmed",
        "doi.org",
        "gleskit figure",
        "figure source",
    )
    provider_lines = [
        line
        for line in lines
        if "provider=" in line
        or "providers=" in line
        or any(token in line.lower() for token in human_friendly_source_tokens)
        or "http://" in line.lower()
        or "https://" in line.lower()
    ]
    artifact_lines = [line for line in lines if "artifact" in line.lower()]
    query_lines = [line for line in lines if "query=" in line.lower() or "effective_query=" in line.lower()]
    markdown_link_count = report_text.count("](")
    embedded_figure_count = sum(
        1 for line in str(report_text or "").splitlines() if line.strip().startswith("![") and "](" in line
    )
    opaque_id_lines = [
        line
        for line in lines
        if ("artifact_" in line.lower() or "session_" in line.lower() or "provider_" in line.lower())
        and not any(token in line.lower() for token in human_friendly_source_tokens)
        and "http://" not in line.lower()
        and "https://" not in line.lower()
    ]
    return {
        "source_line_count": len(lines),
        "provider_source_line_count": len(provider_lines),
        "artifact_source_line_count": len(artifact_lines),
        "query_source_line_count": len(query_lines),
        "markdown_link_count": markdown_link_count,
        "embedded_figure_count": embedded_figure_count,
        "opaque_id_source_line_count": len(opaque_id_lines),
        "source_line_examples": lines[:8],
    }


def build_evaluation_payload(run_root: Path, episode: dict[str, Any]) -> dict[str, Any]:
    specs = load_report_type_specs()
    case: EvalCase = specs[episode["report_type"]].case_map()[episode["case_id"]]
    manifest = load_run_manifest(run_root)
    workspace_root = Path(manifest["workspace_home"])
    return {
        "case_id": case.case_id,
        "report_type": case.template_id,
        "case_brief": case.brief,
        "kickoff_context": case.kickoff_context,
        "expected_nuggets": list(case.expected_nuggets),
        "forbidden_overclaims": list(case.forbidden_overclaims),
        "final_report": episode.get("final_report") or "",
        "final_report_payload": episode.get("final_report_payload"),
        "tool_trace_excerpt": [
            {
                "name": item["name"],
                "arguments": item["arguments"],
                "output_excerpt": str(item["llm_payload"])[:1000],
            }
            for item in episode.get("tool_calls", [])[:12]
        ],
        "artifact_details": build_artifact_details(workspace_root, episode["session_id"]),
        "visible_sources_summary": visible_sources_summary(episode.get("final_report") or ""),
        "transcript_excerpt": (episode.get("messages") or [])[-8:],
    }


def deterministic_metrics(episode: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    visible = payload.get("visible_sources_summary") or {}
    source_count = int(visible.get("source_line_count") or 0)
    provider_count = int(visible.get("provider_source_line_count") or 0)
    link_count = int(visible.get("markdown_link_count") or 0)
    figure_count = int(visible.get("embedded_figure_count") or 0)
    opaque_count = int(visible.get("opaque_id_source_line_count") or 0)
    raw_reader_facing = min(
        1.0,
        0.25 * min(source_count / 4.0, 1.0)
        + 0.35 * min(provider_count / 3.0, 1.0)
        + 0.25 * min(link_count / 4.0, 1.0)
        + 0.15 * min(figure_count / 1.0, 1.0),
    )
    opaque_penalty = min(0.25, 0.05 * opaque_count)
    reader_facing_provenance_score = round(max(0.0, raw_reader_facing - opaque_penalty), 3)
    return {
        "turn_count": int(episode.get("turn_count") or 0),
        "tool_call_count": int(episode.get("tool_call_count") or 0),
        "unique_tools_used": len({item["name"] for item in episode.get("tool_calls") or []}),
        "artifact_count": len(payload["artifact_details"]),
        "report_revision_count": int(episode.get("report_revision_count") or 0),
        "time_to_first_report": episode.get("time_to_first_report"),
        "final_report_word_count": count_words(payload["final_report"]),
        "linked_artifact_count": len((episode.get("final_report_payload") or {}).get("linked_artifact_ids") or []),
        "visible_sources_count": int((payload.get("visible_sources_summary") or {}).get("source_line_count") or 0),
        "visible_provider_sources_count": int(
            (payload.get("visible_sources_summary") or {}).get("provider_source_line_count") or 0
        ),
        "reader_facing_provenance_score": reader_facing_provenance_score,
        "stop_reason": episode.get("stop_reason"),
        "template_requested_vs_used": float(payload["report_type"] == episode["template_id"]),
        "mean_nugget_text_overlap": mean_nugget_text_overlap(payload["final_report"], payload["expected_nuggets"]),
        "report_saved": bool(episode.get("final_report_id")),
        "status": episode.get("status"),
    }


if Field is not None:
    class JudgeMetrics(BaseModel):  # type: ignore[misc]
        claim_support_rate: float = Field(ge=0, le=1)  # type: ignore[operator]
        nugget_recall: float = Field(ge=0, le=1)  # type: ignore[operator]
        unsupported_or_overstated_claim_rate: float = Field(ge=0, le=1)  # type: ignore[operator]
        template_compliance_score: float = Field(ge=0, le=1)  # type: ignore[operator]
        evidence_utilization_score: float = Field(ge=0, le=1)  # type: ignore[operator]
        reader_facing_provenance_score: float = Field(ge=0, le=1)  # type: ignore[operator]
        major_strengths: list[str] = Field(default_factory=list)  # type: ignore[operator]
        major_issues: list[str] = Field(default_factory=list)  # type: ignore[operator]
        rationale: str = ""
else:
    JudgeMetrics = None  # type: ignore[assignment]


@dataclass(frozen=True)
class JudgeConfig:
    enabled: bool
    model: str
    timeout_seconds: float = 180.0


async def judge_metrics(payload: dict[str, Any], *, judge_config: JudgeConfig) -> dict[str, Any]:
    if not judge_config.enabled or ChatOpenAI is None or Field is None or JudgeMetrics is None:
        return {
            "claim_support_rate": 0.0,
            "nugget_recall": 0.0,
            "unsupported_or_overstated_claim_rate": 0.0,
            "template_compliance_score": 0.0,
            "evidence_utilization_score": 0.0,
            "reader_facing_provenance_score": 0.0,
            "major_strengths": [],
            "major_issues": ["Judge disabled or optional dependencies missing."],
            "rationale": "Judge skipped.",
        }
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "claim_support_rate": 0.0,
            "nugget_recall": 0.0,
            "unsupported_or_overstated_claim_rate": 0.0,
            "template_compliance_score": 0.0,
            "evidence_utilization_score": 0.0,
            "reader_facing_provenance_score": 0.0,
            "major_strengths": [],
            "major_issues": ["Judge skipped because OPENAI_API_KEY is not configured."],
            "rationale": "Judge skipped.",
        }
    model = ChatOpenAI(model=judge_config.model, temperature=0.0, api_key=api_key)
    structured = model.with_structured_output(JudgeMetrics)
    prompt = f"""
You are grading a biomedical report produced during a controlled GLESKit evaluation episode.

Score each metric from 0.0 to 1.0.

Metric definitions:
- claim_support_rate: fraction of meaningful report claims that are supported by the evidence pack.
- nugget_recall: fraction of expected case nuggets that are covered in the report.
- unsupported_or_overstated_claim_rate: fraction of report claims that are unsupported, factually exaggerated, or clearly too strong for the evidence.
- template_compliance_score: how well the report fits the requested template and objective.
- evidence_utilization_score: how well the report uses the important evidence that was actually retrieved in the episode.
- reader_facing_provenance_score: how well the report presents human-readable, section-local, user-auditable provenance such as source names, links, and figure attribution.

Evaluation rules:
- Be conservative about support.
- Penalize causal or clinical overreach.
- Do not reward empty verbosity.
- Focus on the final saved report, but use the evidence pack to judge support and utilization.
- Visible provenance in the report body matters.
- Reward reports that include `Sources:` lines directly in the report sections or bullets they support.
- Treat provider names, human-readable source collections, paper titles, DOI/PubMed links, artifact links, and figure links inside `Sources:` lines as positive evidence-grounding signals.
- If major claims are not accompanied by visible source attribution in the report body, reduce claim_support_rate and evidence_utilization_score.
- Do not treat the tool trace alone as a substitute for visible source attribution in the saved report.
- Prefer reader-facing scientific reporting over internal debug-style reporting.
- Reward reports that name sources in a human-readable way such as `Reactome`, `GO Biological Process`, `Europe PMC`, `Open Targets`, `GTEx`, or `Expression Atlas`.
- Reward clickable links when papers, artifacts, or figures are referenced.
- Reward embedded figures when they are relevant and accompanied by a short figure-source description.
- Penalize reports whose provenance is dominated by opaque internal ids or tool-facing jargon instead of reader-facing sources.
- Do not penalize a report for being concise if it is well-grounded, readable, and complete for the task.
- Do not penalize a report merely for being more human-readable, more specific, or longer than a terse report.
- Do not count a claim as overstatement just because it uses natural scientific prose or stronger narrative flow.
- Count overstatement only when the report goes beyond the retrieved evidence, states a factual conclusion not supported in-session, or uses causal/clinical certainty without enough backing.
- If a report is careful, caveated, and visibly sourced, richer prose alone should not increase unsupported_or_overstated_claim_rate.

Case id: {payload['case_id']}
Requested report type: {payload['report_type']}
Case brief: {payload['case_brief']}
Kickoff context: {compact_json(payload['kickoff_context'], 1200)}
Expected nuggets: {compact_json(payload['expected_nuggets'], 1200)}
Forbidden overclaims: {compact_json(payload['forbidden_overclaims'], 1200)}

Artifact details:
{compact_json(payload['artifact_details'], 5000)}

Visible sources summary from final report:
{compact_json(payload['visible_sources_summary'], 2000)}

Tool trace excerpt:
{compact_json(payload['tool_trace_excerpt'], 5000)}

Transcript excerpt:
{compact_json(payload['transcript_excerpt'], 5000)}

Final report:
{payload['final_report']}
""".strip()
    try:
        judged = await asyncio.wait_for(structured.ainvoke(prompt), timeout=judge_config.timeout_seconds)
        return judged.model_dump()
    except asyncio.TimeoutError:
        return {
            "claim_support_rate": 0.0,
            "nugget_recall": 0.0,
            "unsupported_or_overstated_claim_rate": 1.0,
            "template_compliance_score": 0.0,
            "evidence_utilization_score": 0.0,
            "reader_facing_provenance_score": 0.0,
            "major_strengths": [],
            "major_issues": [f"Judge timed out after {judge_config.timeout_seconds:.0f}s."],
            "rationale": "Judge timed out.",
        }


async def score_run(run_root: Path, *, force: bool = False) -> list[dict[str, Any]]:
    manifest = load_run_manifest(run_root)
    judging_cfg = manifest["config"].get("judging") or {}
    judge_config = JudgeConfig(
        enabled=bool(judging_cfg.get("enabled", True)),
        model=str(judging_cfg.get("model") or "gpt-5-mini"),
        timeout_seconds=float(judging_cfg.get("timeout_seconds") or 180.0),
    )
    results: list[dict[str, Any]] = []
    for episode in load_episode_payloads(run_root):
        episode_dir = run_root / "episodes" / episode["episode_id"]
        metrics_path = episode_dir / "metrics.json"
        if metrics_path.exists() and not force:
            try:
                results.append(json.loads(metrics_path.read_text(encoding="utf-8")))
                continue
            except Exception:
                pass
        payload = build_evaluation_payload(run_root, episode)
        deterministic = deterministic_metrics(episode, payload)
        judged = await judge_metrics(payload, judge_config=judge_config)
        combined = {
            "episode_id": episode["episode_id"],
            "report_type": episode["report_type"],
            "case_id": episode["case_id"],
            "provider_id": episode["provider_id"],
            "provider_kind": episode.get("provider_kind"),
            "model_id": episode["model_id"],
            "skills_mode": episode["skills_mode"],
            "seed": episode["seed"],
            "session_id": episode["session_id"],
            **deterministic,
            **judged,
        }
        results.append(combined)
        metrics_path.write_text(json.dumps(combined, indent=2, sort_keys=True), encoding="utf-8")
        (episode_dir / "evaluation_payload.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    return results
