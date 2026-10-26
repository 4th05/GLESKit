from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict, get_args, get_origin

from gleskit_mcp.eval.bixbench import (
    current_utc_iso,
    numeric_for_row,
    read_json,
    select_bixbench_rows,
    sha256_text,
    write_json_atomic,
)
from gleskit_mcp.eval.bixbench_runner import (
    build_judge_agreement_rows,
    build_judge_summary_rows,
    build_overall_summary,
    render_html_summary_report,
    render_markdown_summary_report,
    write_html_summary_report,
    write_markdown_summary_report,
    write_portal,
    write_summary_artifacts,
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
from gleskit_mcp.eval.runtime import compact_json

try:  # pragma: no cover - optional eval dependency
    from langchain_openai import ChatOpenAI
    from langgraph.graph import END, START, StateGraph
    from pydantic import BaseModel, Field
except Exception:  # pragma: no cover - optional dependency fallback
    ChatOpenAI = None
    START = None
    END = None
    StateGraph = None
    BaseModel = object
    Field = None

try:  # pragma: no cover - optional dependency fallback
    from openai import APIConnectionError, APITimeoutError, APIStatusError, RateLimitError
except Exception:  # pragma: no cover - optional dependency fallback
    APIConnectionError = None
    APITimeoutError = None
    APIStatusError = None
    RateLimitError = None


DEFAULT_MODEL = "openai/gpt-5.4-mini"
DEFAULT_JUDGE_RETRIES = 3
DEFAULT_JUDGE_RETRY_DELAY_SECONDS = 3.0
DEFAULT_JUDGE_PROVIDER = "openai"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
JUDGE_SCORE_BANDS = STANDARD_JUDGE_SCORE_BANDS
JUDGE_SCHEMA_VERSION = 4
BIXBENCH_CATEGORY_KEYS = tuple(str(item["key"]) for item in STANDARD_JUDGE_SCORE_BANDS[::-1])
MATERIAL_EVIDENCE_FAILURE_CODES = {
    "contradiction",
    "missing_trace_support",
    "overclaim",
    "trace_mismatch",
    "unsupported_claim",
}


class JudgeStructuredOutputError(RuntimeError):
    """Raised when a judge model returns malformed or empty structured output."""


class JudgeProgressTracker:
    def __init__(
        self,
        *,
        summary_root: Path,
        total_questions: int,
        run_label: str,
        verbose: bool = False,
    ) -> None:
        self.summary_root = summary_root
        self.progress_path = summary_root / "progress.json"
        self.total_questions = max(0, int(total_questions))
        self.run_label = str(run_label or "judge").strip() or "judge"
        self.verbose = bool(verbose)
        self._state: dict[str, Any] = {
            "created_at": current_utc_iso(),
            "updated_at": current_utc_iso(),
            "status": "running",
            "run_label": self.run_label,
            "total_questions": self.total_questions,
            "completed_questions": 0,
            "skipped_questions": 0,
            "failed_questions": 0,
            "current_question_id": None,
            "current_stage": None,
            "questions": {},
        }
        self._write()

    def _question_state(self, question_id: str, *, episode_root: Path | None = None) -> dict[str, Any]:
        questions = self._state.setdefault("questions", {})
        question = questions.get(question_id)
        if not isinstance(question, dict):
            question = {
                "question_id": question_id,
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "elapsed_seconds": None,
                "current_stage": None,
                "stage_order": [],
                "stages": {},
                "episode_root": str(episode_root) if episode_root is not None else None,
            }
            questions[question_id] = question
        elif episode_root is not None:
            question["episode_root"] = str(episode_root)
        return question

    def _stage_state(self, question: dict[str, Any], stage: str) -> dict[str, Any]:
        stages = question.setdefault("stages", {})
        payload = stages.get(stage)
        if not isinstance(payload, dict):
            payload = {
                "stage": stage,
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "elapsed_seconds": None,
                "retries": 0,
                "model": None,
                "detail": None,
                "error": None,
            }
            stages[stage] = payload
            order = question.setdefault("stage_order", [])
            if stage not in order:
                order.append(stage)
        return payload

    def _write(self) -> None:
        self._state["updated_at"] = current_utc_iso()
        write_json_atomic(self.progress_path, self._state)
        questions = self._state.get("questions")
        if not isinstance(questions, dict):
            return
        for question in questions.values():
            if not isinstance(question, dict):
                continue
            episode_root_value = question.get("episode_root")
            if not episode_root_value:
                continue
            episode_root = Path(str(episode_root_value))
            write_json_atomic(episode_root / "judge_progress.json", question)

    def _log(self, message: str, *, force: bool = False) -> None:
        if not (self.verbose or force):
            return
        print(f"[{self.run_label}] {message}", flush=True)

    def start_question(self, question_id: str, *, episode_root: Path, index: int, total: int) -> None:
        question = self._question_state(question_id, episode_root=episode_root)
        question["status"] = "running"
        question["started_at"] = current_utc_iso()
        question["completed_at"] = None
        question["elapsed_seconds"] = None
        question["current_stage"] = None
        self._state["current_question_id"] = question_id
        self._state["current_stage"] = None
        self._write()
        self._log(f"question {index}/{total} start: {question_id}", force=True)

    def skip_question(self, question_id: str, *, episode_root: Path, reason: str) -> None:
        question = self._question_state(question_id, episode_root=episode_root)
        question["status"] = "skipped"
        question["completed_at"] = current_utc_iso()
        question["current_stage"] = None
        question["skip_reason"] = reason
        self._state["skipped_questions"] = int(self._state.get("skipped_questions") or 0) + 1
        self._state["current_question_id"] = None
        self._state["current_stage"] = None
        self._write()
        self._log(f"question skipped: {question_id} ({reason})", force=True)

    def start_stage(self, question_id: str, stage: str, *, model: str | None = None, detail: str | None = None) -> None:
        question = self._question_state(question_id)
        payload = self._stage_state(question, stage)
        payload["status"] = "running"
        payload["started_at"] = current_utc_iso()
        payload["completed_at"] = None
        payload["elapsed_seconds"] = None
        payload["model"] = model
        payload["detail"] = detail
        payload["error"] = None
        question["current_stage"] = stage
        self._state["current_question_id"] = question_id
        self._state["current_stage"] = stage
        self._write()
        model_suffix = f" model={model}" if model else ""
        detail_suffix = f" detail={detail}" if detail else ""
        self._log(f"{question_id} -> {stage} started{model_suffix}{detail_suffix}", force=True)

    def retry_stage(self, question_id: str, stage: str, *, attempt: int, max_retries: int, delay_seconds: float, error: str) -> None:
        question = self._question_state(question_id)
        payload = self._stage_state(question, stage)
        payload["status"] = "retrying"
        payload["retries"] = int(payload.get("retries") or 0) + 1
        payload["error"] = error
        self._write()
        self._log(
            f"{question_id} -> {stage} retry {attempt}/{max_retries} after {delay_seconds:.1f}s ({error})",
            force=True,
        )

    def complete_stage(self, question_id: str, stage: str, *, elapsed_seconds: float, detail: str | None = None) -> None:
        question = self._question_state(question_id)
        payload = self._stage_state(question, stage)
        payload["status"] = "completed"
        payload["completed_at"] = current_utc_iso()
        payload["elapsed_seconds"] = round(float(elapsed_seconds), 3)
        if detail:
            payload["detail"] = detail
        question["current_stage"] = None
        self._state["current_stage"] = None
        self._write()
        detail_suffix = f" ({detail})" if detail else ""
        self._log(f"{question_id} -> {stage} completed in {float(elapsed_seconds):.1f}s{detail_suffix}", force=True)

    def fail_stage(self, question_id: str, stage: str, *, elapsed_seconds: float, error: str) -> None:
        question = self._question_state(question_id)
        payload = self._stage_state(question, stage)
        payload["status"] = "failed"
        payload["completed_at"] = current_utc_iso()
        payload["elapsed_seconds"] = round(float(elapsed_seconds), 3)
        payload["error"] = error
        question["current_stage"] = None
        self._state["current_stage"] = None
        self._write()
        self._log(f"{question_id} -> {stage} failed in {float(elapsed_seconds):.1f}s ({error})", force=True)

    def finish_question(self, question_id: str, *, status: str, started_at_monotonic: float, note: str | None = None) -> None:
        question = self._question_state(question_id)
        question["status"] = status
        question["completed_at"] = current_utc_iso()
        question["elapsed_seconds"] = round(max(0.0, time.perf_counter() - float(started_at_monotonic)), 3)
        question["current_stage"] = None
        self._state["current_question_id"] = None
        self._state["current_stage"] = None
        if status == "completed":
            self._state["completed_questions"] = int(self._state.get("completed_questions") or 0) + 1
        elif status == "failed":
            self._state["failed_questions"] = int(self._state.get("failed_questions") or 0) + 1
        elif status == "skipped":
            self._state["skipped_questions"] = int(self._state.get("skipped_questions") or 0) + 1
        self._write()
        note_suffix = f" ({note})" if note else ""
        self._log(
            f"question finished: {question_id} status={status} elapsed={question['elapsed_seconds']:.1f}s{note_suffix}",
            force=True,
        )

    def finalize(self, *, status: str) -> None:
        self._state["status"] = status
        self._state["current_question_id"] = None
        self._state["current_stage"] = None
        self._write()
        self._log(
            f"run finished: status={status} completed={self._state.get('completed_questions', 0)} "
            f"skipped={self._state.get('skipped_questions', 0)} failed={self._state.get('failed_questions', 0)}",
            force=True,
        )


def _read_json_if_exists(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return read_json(path)


def _normalize_numeric(value: Any, *, fallback_row: dict[str, Any] | None = None) -> int:
    if value is None or value == "":
        if fallback_row is not None:
            return int(numeric_for_row(fallback_row))
        return 0
    try:
        return int(value)
    except Exception:
        if fallback_row is not None:
            return int(numeric_for_row(fallback_row))
        return 0

def _normalize_judge_provider(provider_name: str) -> str:
    cleaned = str(provider_name or "").strip().lower()
    if cleaned in {"", "openai"}:
        return "openai"
    if cleaned == "openrouter":
        return "openrouter"
    raise ValueError(f"Unsupported judge provider `{provider_name}`. Use `openai` or `openrouter`.")


def _locate_project_root(start: Path | None) -> Path | None:
    if start is None:
        return None
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return None


def _read_env_file_value(project_root: Path | None, key: str) -> str:
    if project_root is None:
        return ""
    env_path = project_root / ".env"
    if not env_path.exists():
        return ""
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        candidate_key, candidate_value = line.split("=", 1)
        if candidate_key.strip() != key:
            continue
        value = candidate_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip()
    return ""


def _env_value(name: str, *, project_root: Path | None = None) -> str:
    direct = os.environ.get(name, "").strip()
    if len(direct) >= 2 and direct[0] == direct[-1] and direct[0] in {"'", '"'}:
        direct = direct[1:-1].strip()
    if direct:
        return direct
    return _read_env_file_value(_locate_project_root(project_root), name).strip()


def _resolve_model_name_for_provider(model_name: str, *, provider: str) -> str:
    cleaned = str(model_name or "").strip()
    if not cleaned:
        cleaned = DEFAULT_MODEL
    normalized_provider = _normalize_judge_provider(provider)
    if normalized_provider == "openrouter":
        if cleaned.startswith("openrouter/"):
            stripped = cleaned.split("/", 1)[1].strip()
            return stripped or cleaned
        return cleaned
    if "/" not in cleaned:
        return cleaned
    _provider, bare_model = cleaned.split("/", 1)
    bare_model = bare_model.strip()
    return bare_model or cleaned


def _judge_score_band(score: float | None) -> dict[str, Any] | None:
    return judge_score_band(score)


def load_run_question_rows(run_root: Path) -> list[dict[str, Any]]:
    for candidate in (
        run_root / "planned_matrix.json",
        run_root / "summary" / "questions.json",
    ):
        if not candidate.exists():
            continue
        rows = read_json(candidate)
        if isinstance(rows, list):
            normalized: list[dict[str, Any]] = []
            for row in rows:
                if isinstance(row, dict) and row.get("question_id"):
                    copy = dict(row)
                    copy["numeric"] = _normalize_numeric(copy.get("numeric"), fallback_row=copy)
                    normalized.append(copy)
            if normalized:
                return normalized

    episode_rows: list[dict[str, Any]] = []
    episodes_root = run_root / "episodes"
    if not episodes_root.exists():
        return episode_rows
    for episode_dir in sorted(episodes_root.iterdir(), key=lambda item: item.name):
        episode_path = episode_dir / "episode.json"
        if not episode_path.exists():
            continue
        row = read_json(episode_path)
        if not isinstance(row, dict):
            continue
        row = dict(row)
        row["numeric"] = _normalize_numeric(row.get("numeric"), fallback_row=row)
        episode_rows.append(row)
    return episode_rows


def _ensure_question_metadata(row: dict[str, Any]) -> dict[str, Any]:
    copy = dict(row)
    copy["numeric"] = _normalize_numeric(copy.get("numeric"), fallback_row=copy)
    return copy


def _text_preview(value: Any, *, limit: int = 260) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _selected_input_summary(input_payload: Any) -> dict[str, Any]:
    if not isinstance(input_payload, dict):
        return {"text": _text_preview(input_payload)}
    summary: dict[str, Any] = {}
    for key in (
        "description",
        "command",
        "source_path",
        "label",
        "analysis",
        "analysis_axis",
        "scope",
        "method",
        "mode",
        "test",
        "workdir",
    ):
        value = input_payload.get(key)
        if value not in (None, ""):
            summary[key] = value
    if summary:
        return summary
    for key in sorted(input_payload.keys()):
        value = input_payload.get(key)
        if value not in (None, ""):
            summary[key] = value
        if len(summary) >= 6:
            break
    return summary


def _extract_artifact_path(text: str) -> str | None:
    match = re.search(r"Full output saved to:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def _summarize_tool_output(output: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if output is None:
        summary["text"] = "-"
        return summary

    parsed: Any = output
    if isinstance(output, str):
        stripped = output.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = output
        else:
            parsed = output

    if isinstance(parsed, dict):
        summary["text"] = _text_preview(parsed.get("summary") or parsed.get("message") or parsed)
        artifact_path = None
        workspace = parsed.get("workspace")
        if isinstance(workspace, dict):
            artifact_path = workspace.get("artifact_id") or workspace.get("saved_path") or workspace.get("output_path")
        if not artifact_path and isinstance(parsed.get("output"), str):
            artifact_path = _extract_artifact_path(parsed["output"])
        if not artifact_path and isinstance(output, str):
            artifact_path = _extract_artifact_path(output)
        if artifact_path:
            summary["artifact_path"] = artifact_path

        structured = parsed.get("structured_data")
        if isinstance(structured, dict):
            structured_summary = structured.get("summary")
            result = structured.get("result") if isinstance(structured.get("result"), dict) else {}
            interesting_keys = (
                "correlation",
                "p_value",
                "label_count",
                "value_count",
                "minimum",
                "maximum",
                "mean",
                "median",
                "standard_deviation",
                "skewness",
                "skewness_label",
            )
            result_bits = [f"{key}={result.get(key)}" for key in interesting_keys if result.get(key) is not None]
            if structured_summary:
                summary["structured_summary"] = structured_summary
            if result_bits:
                summary["structured_result"] = result_bits
            if structured_summary or result_bits:
                summary_text = structured_summary or summary["text"]
                if result_bits:
                    summary_text = f"{summary_text} | {', '.join(result_bits)}"
                summary["text"] = _text_preview(summary_text)
            top_terms = structured.get("top_terms")
            if isinstance(top_terms, list):
                top_names = [
                    str(item.get("term_name") or "").strip()
                    for item in top_terms
                    if isinstance(item, dict) and str(item.get("term_name") or "").strip()
                ]
                if top_names:
                    summary["top_terms"] = top_names[:3]
                    summary["text"] = _text_preview(f"{summary['text']} | top_terms={', '.join(top_names[:3])}")
        else:
            preview_keys = {}
            for key in ("summary", "provider", "analysis", "mode"):
                if parsed.get(key) not in (None, ""):
                    preview_keys[key] = parsed.get(key)
            if preview_keys:
                summary["details"] = preview_keys
        return summary

    if isinstance(parsed, list):
        summary["text"] = f"{len(parsed)} item(s)"
        summary["items"] = [_text_preview(item) for item in parsed[:4]]
        return summary

    text = _text_preview(parsed)
    summary["text"] = text
    artifact_path = _extract_artifact_path(str(output))
    if artifact_path:
        summary["artifact_path"] = artifact_path
    return summary


def build_tool_provenance(record: dict[str, Any]) -> list[dict[str, Any]]:
    final_answer = record.get("final_answer") if isinstance(record.get("final_answer"), dict) else {}
    evidence_refs = [str(item).strip() for item in (final_answer.get("evidence") or []) if str(item).strip()]
    provenance: list[dict[str, Any]] = []
    for item in record.get("tool_trace") or []:
        if not isinstance(item, dict):
            continue
        input_payload = item.get("input") if isinstance(item.get("input"), dict) else {}
        output_summary = _summarize_tool_output(item.get("output"))
        entry: dict[str, Any] = {
            "tool": item.get("tool"),
            "status": item.get("status"),
            "call_id": item.get("call_id"),
            "description": input_payload.get("description") or input_payload.get("label") or input_payload.get("command"),
            "input_summary": _selected_input_summary(input_payload),
            "output_summary": output_summary.get("text"),
        }
        if output_summary.get("structured_summary") is not None:
            entry["structured_summary"] = output_summary.get("structured_summary")
        if output_summary.get("structured_result") is not None:
            entry["structured_result"] = output_summary.get("structured_result")
        if output_summary.get("artifact_path") is not None:
            entry["artifact_path"] = output_summary.get("artifact_path")
        if output_summary.get("details") is not None:
            entry["details"] = output_summary.get("details")
        if output_summary.get("items") is not None:
            entry["items"] = output_summary.get("items")
        if output_summary.get("top_terms") is not None:
            entry["top_terms"] = output_summary.get("top_terms")
        if evidence_refs:
            entry["evidence_refs"] = evidence_refs
        provenance.append(entry)
    return provenance


def _merge_judge_fields(record: dict[str, Any], judge_payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record)
    merged["judge"] = judge_payload
    judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
    specialists = judge_payload.get("specialists") if isinstance(judge_payload.get("specialists"), dict) else {}
    stats = specialists.get("stats_specialist") if isinstance(specialists.get("stats_specialist"), dict) else {}
    biology = specialists.get("biology_specialist") if isinstance(specialists.get("biology_specialist"), dict) else {}
    evidence = specialists.get("evidence_checker") if isinstance(specialists.get("evidence_checker"), dict) else {}
    committee_category = judge_payload.get("committee_category")
    if committee_category is None:
        legacy_band = judge_payload.get("judge_score_band") if isinstance(judge_payload.get("judge_score_band"), dict) else None
        if isinstance(legacy_band, dict):
            committee_category = legacy_band.get("key")
        elif judge.get("verdict") in {"unsupported", "weak", "partial", "mostly_supported", "accept"}:
            committee_category = judge.get("verdict")
        else:
            legacy_score = judge_payload.get("judge_score", judge.get("judge_score"))
            legacy_band = judge_score_band(None if legacy_score is None else float(legacy_score))
            committee_category = (legacy_band or {}).get("key")
    merged["judge_schema_version"] = judge_payload.get("judge_schema_version", 1)
    merged["committee_category"] = committee_category
    merged["committee_level_mean_raw"] = judge_payload.get("committee_level_mean_raw")
    merged["committee_level_mean_final"] = judge_payload.get("committee_level_mean_final")
    merged["committee_level_mean_normalized"] = _first_non_none(
        judge_payload.get("committee_level_mean_normalized"),
        judge_payload.get("judge_score"),
        judge.get("judge_score"),
    )
    merged["cap_applied"] = judge_payload.get("cap_applied")
    merged["cap_reasons"] = judge_payload.get("cap_reasons") or []
    merged["quality_flags"] = judge_payload.get("quality_flags") or []
    merged["review_required"] = _first_non_none(judge_payload.get("review_required"), judge.get("review_required"))
    merged["review_reasons"] = _first_non_none(judge_payload.get("review_reasons"), judge.get("review_reasons"), [])
    merged["specialist_disagreement_span"] = judge_payload.get("specialist_disagreement_span")
    merged["judge_score"] = _first_non_none(judge_payload.get("judge_score"), judge.get("judge_score"))
    merged["judge_arbiter_raw_score"] = judge.get("arbiter_raw_score")
    merged["judge_verdict"] = committee_category
    merged["judge_agreement_with_benchmark"] = _first_non_none(judge_payload.get("benchmark_alignment"), judge.get("agreement_with_benchmark"))
    merged["judge_short_review"] = judge.get("short_review")
    merged["judge_rationale"] = judge.get("rationale")
    merged["judge_disagreement_flags"] = judge.get("disagreement_flags") or []
    merged["judge_committee_reference_score"] = judge_payload.get("committee_level_mean_raw")
    merged["judge_weight_hint"] = judge_payload.get("weight_hint")
    merged["judge_tool_provenance"] = judge_payload.get("tool_provenance") or record.get("tool_provenance") or []
    merged["judge_stats_category"] = stats.get("category")
    merged["judge_biology_category"] = biology.get("category")
    merged["judge_evidence_category"] = evidence.get("category")
    merged["judge_stats_score"] = judge_category_level(stats.get("category"))
    merged["judge_biology_score"] = judge_category_level(biology.get("category"))
    merged["judge_evidence_score"] = judge_category_level(evidence.get("category"))
    return merged


def load_episode_record(episode_root: Path, base_row: dict[str, Any] | None = None) -> dict[str, Any]:
    episode_path = episode_root / "episode.json"
    if not episode_path.exists():
        raise FileNotFoundError(episode_path)
    episode = read_json(episode_path)
    if not isinstance(episode, dict):
        raise ValueError(f"Malformed episode payload: {episode_path}")

    input_payload = _read_json_if_exists(episode_root / "input.json", episode.get("input") or {})
    transcript = _read_json_if_exists(episode_root / "transcript.json", episode.get("transcript") or [])
    tool_trace = _read_json_if_exists(episode_root / "tool_calls.json", episode.get("tool_trace") or [])
    agent_final_answer = _read_json_if_exists(episode_root / "agent_final_answer.json", episode.get("agent_final_answer") or {})
    final_answer = _read_json_if_exists(episode_root / "final_answer.json", episode.get("final_answer") or {})
    answer_resolution = _read_json_if_exists(episode_root / "answer_resolution.json", episode.get("answer_resolution") or {})
    score_details = _read_json_if_exists(episode_root / "score.json", episode.get("score_details") or {})

    merged = dict(episode)
    if base_row:
        for key in ("short_id", "scope", "numeric", "eval_mode", "capsule_uuid", "question", "ideal", "species"):
            if base_row.get(key) is not None:
                merged[key] = base_row.get(key)
    merged["input"] = input_payload
    merged["transcript"] = transcript
    merged["tool_trace"] = tool_trace
    merged["agent_final_answer"] = agent_final_answer
    merged["final_answer"] = final_answer
    merged["answer_resolution"] = answer_resolution
    merged["score_details"] = score_details
    merged["numeric"] = _normalize_numeric(merged.get("numeric"), fallback_row=merged)
    merged["tool_provenance"] = build_tool_provenance(merged)
    judge_path = episode_root / "judge.json"
    if judge_path.exists():
        judge_payload = read_json(judge_path)
        if isinstance(judge_payload, dict):
            merged = _merge_judge_fields(merged, judge_payload)
    return merged


class SpecialistAssessment(BaseModel):  # type: ignore[misc]
    category: str
    summary: str
    key_observations: list[str] = Field(default_factory=list)  # type: ignore[operator]
    satisfied_criteria: list[str] = Field(default_factory=list)  # type: ignore[operator]
    failed_criteria: list[str] = Field(default_factory=list)  # type: ignore[operator]
    failure_codes: list[str] = Field(default_factory=list)  # type: ignore[operator]


class EvidenceAssessment(BaseModel):  # type: ignore[misc]
    category: str
    summary: str
    key_observations: list[str] = Field(default_factory=list)  # type: ignore[operator]
    satisfied_criteria: list[str] = Field(default_factory=list)  # type: ignore[operator]
    failed_criteria: list[str] = Field(default_factory=list)  # type: ignore[operator]
    failure_codes: list[str] = Field(default_factory=list)  # type: ignore[operator]
    matching_evidence: list[str] = Field(default_factory=list)  # type: ignore[operator]
    unsupported_claims: list[str] = Field(default_factory=list)  # type: ignore[operator]
    evidence_gaps: list[str] = Field(default_factory=list)  # type: ignore[operator]


class JudgeVerdict(BaseModel):  # type: ignore[misc]
    short_review: str
    rationale: str
    major_strengths: list[str] = Field(default_factory=list)  # type: ignore[operator]
    major_issues: list[str] = Field(default_factory=list)  # type: ignore[operator]
    disagreement_flags: list[str] = Field(default_factory=list)  # type: ignore[operator]
    review_required: bool = False
    review_reasons: list[str] = Field(default_factory=list)  # type: ignore[operator]


class JudgeState(TypedDict, total=False):
    run_root: str
    episode_root: str
    selection_row: dict[str, Any]
    episode_record: dict[str, Any]
    prompt_context: str
    weight_hint: dict[str, float]
    committee_result: dict[str, Any]
    stats_assessment: dict[str, Any]
    biology_assessment: dict[str, Any]
    evidence_assessment: dict[str, Any]
    arbiter_verdict: dict[str, Any]
    model_map: dict[str, str]


def _require_dependencies() -> None:
    missing: list[str] = []
    if ChatOpenAI is None:
        missing.append("langchain-openai")
    if StateGraph is None:
        missing.append("langgraph")
    if Field is None or BaseModel is object:
        missing.append("pydantic")
    if missing:
        raise RuntimeError(
            "Missing optional evaluation dependencies: "
            + ", ".join(sorted(set(missing)))
            + ". Install them with `.venv/bin/python -m pip install -e \".[eval]\"`."
        )


def _resolve_judge_provider_credentials(
    *,
    provider: str,
    base_url: str | None = None,
    project_root: Path | None = None,
) -> tuple[str, str]:
    normalized_provider = _normalize_judge_provider(provider)
    if normalized_provider == "openrouter":
        api_key = _env_value("OPENROUTER_API_KEY", project_root=project_root)
        resolved_base_url = (
            str(base_url or "").strip()
            or _env_value("OPENROUTER_BASE_URL", project_root=project_root)
            or DEFAULT_OPENROUTER_BASE_URL
        )
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for judge_bixbench when provider=openrouter.")
        return api_key, resolved_base_url
    api_key = _env_value("OPENAI_API_KEY", project_root=project_root)
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for judge_bixbench.")
    resolved_base_url = (
        str(base_url or "").strip()
        or _env_value("OPENAI_BASE_URL", project_root=project_root)
        or DEFAULT_OPENAI_BASE_URL
    )
    return api_key, resolved_base_url


def _weight_hint(numeric: int) -> dict[str, float]:
    if int(numeric) == 1:
        return {"stats": 0.55, "biology": 0.20, "evidence": 0.25}
    return {"stats": 0.25, "biology": 0.40, "evidence": 0.35}


def _specialist_category_level(assessment: dict[str, Any]) -> int | None:
    return judge_category_level((assessment or {}).get("category"))


def _weighted_level_mean(weights: dict[str, float], stats: dict[str, Any], biology: dict[str, Any], evidence: dict[str, Any]) -> float:
    total = (
        Decimal(str(weights.get("stats", 0.0))) * Decimal(_specialist_category_level(stats) or 0)
        + Decimal(str(weights.get("biology", 0.0))) * Decimal(_specialist_category_level(biology) or 0)
        + Decimal(str(weights.get("evidence", 0.0))) * Decimal(_specialist_category_level(evidence) or 0)
    )
    return float(total)


def _category_rubric_text() -> str:
    lines = []
    for key in ("unsupported", "weak", "partial", "mostly_supported", "accept"):
        item = judge_category(key) or {}
        lines.append(f"- {item.get('key')} (level {item.get('level')}): {item.get('rubric')}")
    return "\n".join(lines)


def _has_material_evidence_failure_codes(evidence: dict[str, Any]) -> bool:
    codes = {
        str(item).strip().lower()
        for item in ((evidence or {}).get("failure_codes") or [])
        if str(item).strip()
    }
    if codes.intersection(MATERIAL_EVIDENCE_FAILURE_CODES):
        return True
    return bool((evidence or {}).get("unsupported_claims"))


def _has_sharp_benchmark_divergence(*, benchmark_correct: bool, committee_category_key: str) -> bool:
    if benchmark_correct and committee_category_key in {"unsupported", "weak"}:
        return True
    if (not benchmark_correct) and committee_category_key in {"mostly_supported", "accept"}:
        return True
    return False


def _compute_bixbench_committee_result(
    *,
    numeric: int,
    weights: dict[str, float],
    stats: dict[str, Any],
    biology: dict[str, Any],
    evidence: dict[str, Any],
    benchmark_correct: bool,
) -> dict[str, Any]:
    raw_level_mean = _weighted_level_mean(weights, stats, biology, evidence)
    # Preserve the continuous weighted mean as the score. Lower specialist
    # assessments remain explicit audit flags rather than post-hoc score caps.
    final_level_mean = raw_level_mean
    quality_flags: list[str] = []

    evidence_category = str((evidence or {}).get("category") or "").strip().lower()
    evidence_level = _specialist_category_level(evidence)
    if evidence_level is not None and evidence_level < final_level_mean:
        quality_flags.append(f"evidence_below_weighted_mean:{evidence_category}")

    stats_level = _specialist_category_level(stats)
    biology_level = _specialist_category_level(biology)
    if int(numeric) == 1 and stats_level is not None and stats_level <= 1 and final_level_mean > 2.0:
        quality_flags.append(f"numeric_stats_weak:{(stats or {}).get('category')}")
    if int(numeric) == 0 and biology_level is not None and biology_level <= 1 and final_level_mean > 2.0:
        quality_flags.append(f"semantic_biology_weak:{(biology or {}).get('category')}")

    committee_category = judge_mean_level_category(final_level_mean) or judge_category("unsupported") or {}
    disagreement_span = judge_category_disagreement_span(
        [
            (stats or {}).get("category"),
            (biology or {}).get("category"),
            (evidence or {}).get("category"),
        ]
    )

    review_reasons: list[str] = []
    if quality_flags:
        review_reasons.append("quality_flag")
    if disagreement_span is not None and disagreement_span >= 3:
        review_reasons.append("specialist_disagreement_span")
    if _has_material_evidence_failure_codes(evidence):
        review_reasons.append("material_evidence_failure")
    committee_category_key = str(committee_category.get("key") or "")
    if _has_sharp_benchmark_divergence(
        benchmark_correct=benchmark_correct,
        committee_category_key=committee_category_key,
    ):
        review_reasons.append("benchmark_divergence_audit")

    return {
        "committee_category": committee_category_key or None,
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
    }


def _episode_has_public_answer(record: dict[str, Any]) -> bool:
    if bool(record.get("abstain")):
        return False
    if bool(record.get("answer_present")):
        return True
    for value in (
        record.get("effective_answer"),
        record.get("answer"),
        ((record.get("final_answer") if isinstance(record.get("final_answer"), dict) else {}) or {}).get("answer"),
        ((record.get("final_answer") if isinstance(record.get("final_answer"), dict) else {}) or {}).get("raw_text"),
    ):
        if value is not None and str(value).strip() != "":
            return True
    return False


def _benchmark_alignment_from_record(record: dict[str, Any]) -> str:
    if bool(record.get("abstain")):
        return "abstained"
    if not _episode_has_public_answer(record):
        return "unanswered"
    return "agree" if bool(record.get("correct")) else "disagree"


def _json_prompt_context(record: dict[str, Any]) -> str:
    context = {
        "question_id": record.get("question_id"),
        "short_id": record.get("short_id"),
        "scope": record.get("scope"),
        "numeric": record.get("numeric"),
        "eval_mode": record.get("eval_mode"),
        "species": record.get("species"),
        "question": record.get("question"),
        "reference_answer": record.get("ideal"),
        "returned_answer": (record.get("final_answer") or {}).get("answer"),
        "raw_returned_text": (record.get("final_answer") or {}).get("raw_text"),
        "answer_evidence": (record.get("final_answer") or {}).get("evidence") or [],
        "visible_transcript": record.get("transcript") or [],
        "tool_provenance": record.get("tool_provenance") or build_tool_provenance(record),
    }
    return compact_json(context, 12000)


def _role_prompt(role: str, context_text: str, guidance: str) -> str:
    return (
        f"You are the {role} in the BixBench judge committee.\n\n"
        f"{guidance}\n\n"
        "The context includes a compact tool provenance snapshot for the question agent. Use it to judge whether the answer is actually supported by the recorded tools and outputs.\n\n"
        "Use exactly one category from: unsupported, weak, partial, mostly_supported, accept.\n\n"
        "Shared category rubric:\n"
        f"{_category_rubric_text()}\n\n"
        "Use a small failure-code vocabulary such as: unsupported_claim, missing_core_answer, trace_mismatch, contradiction, overclaim, "
        "numeric_mismatch, unit_error, range_error, biological_misinterpretation, evidence_gap.\n\n"
        "Do not reveal hidden reasoning. Return structured output only.\n\n"
        "Context:\n"
        f"{context_text}\n"
    )


def _annotation_contains_list(annotation: Any) -> bool:
    if annotation is None:
        return False
    origin = get_origin(annotation)
    if origin is list:
        return True
    if origin is None:
        return annotation is list
    return any(_annotation_contains_list(arg) for arg in get_args(annotation))


def _coerce_structured_text(value: Any) -> str:
    """Flatten provider-specific wrapper objects used for text list items."""
    if isinstance(value, str):
        return re.sub(r"</?item>", "", value).strip()
    if isinstance(value, dict):
        for key in ("text", "value", "item", "content", "summary", "message"):
            if key in value:
                return _coerce_structured_text(value[key])
        return compact_json(value, 1000)
    if isinstance(value, list):
        return "; ".join(item for item in (_coerce_structured_text(item) for item in value) if item)
    return str(value).strip()


def _normalize_schema_payload(schema: type[BaseModel], payload: dict[str, Any]) -> dict[str, Any]:
    model_fields = getattr(schema, "model_fields", None)
    if not isinstance(model_fields, dict):
        return dict(payload)
    normalized = dict(payload)
    for field_name, field_info in model_fields.items():
        annotation = getattr(field_info, "annotation", None)
        if _annotation_contains_list(annotation) and normalized.get(field_name) is not None:
            raw_items = normalized[field_name]
            if not isinstance(raw_items, list):
                raw_items = [raw_items]
            normalized[field_name] = [
                item for item in (_coerce_structured_text(item) for item in raw_items) if item
            ]
            continue
        if normalized.get(field_name) is not None:
            continue
        if _annotation_contains_list(annotation):
            normalized[field_name] = []
            continue
        default_factory = getattr(field_info, "default_factory", None)
        if callable(default_factory):
            normalized[field_name] = default_factory()
            continue
        default = getattr(field_info, "default", None)
        if default is not None and type(default).__name__ != "PydanticUndefinedType":
            normalized[field_name] = default
    return normalized


def _coerce_structured_result(schema: type[BaseModel], result: Any) -> dict[str, Any]:
    if result is None:
        raise JudgeStructuredOutputError("Judge model returned empty structured output.")
    if hasattr(result, "model_dump"):
        payload = result.model_dump()
    elif isinstance(result, dict):
        payload = dict(result)
    elif isinstance(result, str):
        stripped = result.strip()
        if not stripped:
            raise JudgeStructuredOutputError("Judge model returned an empty text payload.")
        try:
            payload = json.loads(stripped)
        except Exception as exc:
            raise JudgeStructuredOutputError("Judge model returned non-JSON text instead of structured output.") from exc
    else:
        raise JudgeStructuredOutputError(
            f"Judge model returned unsupported structured payload type `{type(result).__name__}`."
        )
    if not isinstance(payload, dict):
        raise JudgeStructuredOutputError(
            f"Judge model returned a `{type(payload).__name__}` payload; expected a JSON object."
        )
    normalized_payload = _normalize_schema_payload(schema, payload)
    try:
        validated = schema.model_validate(normalized_payload)
    except Exception as exc:
        raise JudgeStructuredOutputError(f"Judge structured output failed schema validation: {exc}") from exc
    if hasattr(validated, "model_dump"):
        return validated.model_dump()
    return dict(validated)


def _raw_structured_content(raw_response: Any) -> str:
    """Extract provider text from a raw LangChain response for JSON-mode diagnostics."""
    content = getattr(raw_response, "content", raw_response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _function_call_instruction(schema: type[BaseModel]) -> str:
    """Require the schema function so tool-capable providers do not answer in prose."""
    schema_name = getattr(schema, "__name__", "structured_result")
    return (
        f"\n\nCall the supplied `{schema_name}` function now to return your assessment. "
        "Do not reply in normal prose. Populate every required field and use empty arrays for unsupported list fields."
    )


def _normalize_assessment_category(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    raw_category = str(normalized.get("category") or "").strip().lower()
    if judge_category(raw_category) is None:
        raise JudgeStructuredOutputError(
            "Judge returned an invalid category. Expected one of: unsupported, weak, partial, mostly_supported, accept."
        )
    normalized["category"] = raw_category
    return normalized


def _is_retryable_judge_error(exc: Exception, *, provider: str | None = None) -> bool:
    if isinstance(exc, JudgeStructuredOutputError):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    if APIConnectionError is not None and isinstance(exc, APIConnectionError):
        return True
    if APITimeoutError is not None and isinstance(exc, APITimeoutError):
        return True
    if RateLimitError is not None and isinstance(exc, RateLimitError):
        return True
    if APIStatusError is not None and isinstance(exc, APIStatusError):
        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    # OpenRouter occasionally collapses an upstream transient failure to this
    # otherwise uninformative 400 response. Retry only for that exact provider
    # and message; ordinary client-side 400 errors must still fail immediately.
    if (
        str(provider or "").strip().lower() == "openrouter"
        and "provider returned error" in text
        and re.search(r"(?:code)[\"']?\s*[:=]\s*400", text) is not None
    ):
        return True
    embedded_status = re.search(r"(?:status(?:[_ ]?code)?|code)[\"']?\s*[:=]\s*(\d{3})", text)
    if embedded_status is not None and int(embedded_status.group(1)) in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    retryable_markers = (
        "connection error",
        "connectionerror",
        "api connection",
        "apiconnectionerror",
        "timeout",
        "timed out",
        "timeouterror",
        "ratelimit",
        "rate limit",
        "temporarily unavailable",
        "temporary unavailable",
        "service unavailable",
        "server error",
        "bad gateway",
        "gateway timeout",
        "operation was aborted",
    )
    return any(marker in text for marker in retryable_markers)


async def _invoke_structured_once(
    *,
    provider: str,
    base_url: str,
    model_name: str,
    schema: type[BaseModel],
    prompt: str,
    api_key: str,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    _require_dependencies()
    normalized_provider = _normalize_judge_provider(provider)
    resolved_model = _resolve_model_name_for_provider(model_name, provider=normalized_provider)
    model_kwargs: dict[str, Any] = {
        "model": resolved_model,
        "temperature": 0.0,
        "api_key": api_key,
        "base_url": base_url,
    }
    if max_tokens is not None and int(max_tokens) > 0:
        model_kwargs["max_tokens"] = int(max_tokens)
    if normalized_provider == "openrouter":
        model_kwargs["default_headers"] = {
            "X-OpenRouter-Title": "GLESKit BixBench Judge",
        }
        # Committee members must return a compact JSON verdict. Disable optional
        # thinking tokens so a reasoning-only response cannot consume the output.
        model_kwargs["extra_body"] = {"reasoning": {"effort": "none", "exclude": True}}
    model = ChatOpenAI(**model_kwargs)
    if normalized_provider == "openrouter":
        # OpenRouter models do not uniformly implement OpenAI's json_schema
        # response format. MiniMax reliably returns this compact contract as a
        # function call when explicitly instructed, which remains validated by
        # the same Pydantic schema below.
        runnable = model.with_structured_output(schema, method="function_calling", include_raw=True)
        prompt = prompt + _function_call_instruction(schema)
    else:
        runnable = model.with_structured_output(schema)
    result = await runnable.ainvoke(prompt)
    if normalized_provider == "openrouter":
        if not isinstance(result, dict):
            raise JudgeStructuredOutputError(
                f"OpenRouter JSON-mode response had unexpected type `{type(result).__name__}`."
            )
        parsed = result.get("parsed")
        if parsed is not None:
            return _coerce_structured_result(schema, parsed)
        raw_response = result.get("raw")
        raw_tool_calls = getattr(raw_response, "tool_calls", None)
        if isinstance(raw_tool_calls, list) and raw_tool_calls:
            tool_args = raw_tool_calls[0].get("args") if isinstance(raw_tool_calls[0], dict) else None
            if isinstance(tool_args, dict):
                return _coerce_structured_result(schema, tool_args)
        raw_content = _raw_structured_content(raw_response)
        if raw_content:
            return _coerce_structured_result(schema, raw_content)
        parse_error = result.get("parsing_error")
        raise JudgeStructuredOutputError(
            "OpenRouter structured response contained no parseable function call or content"
            + (f": {parse_error}" if parse_error else ".")
        )
    return _coerce_structured_result(schema, result)


async def _invoke_structured(
    *,
    provider: str,
    base_url: str,
    model_name: str,
    schema: type[BaseModel],
    prompt: str,
    api_key: str,
    max_retries: int = DEFAULT_JUDGE_RETRIES,
    retry_delay_seconds: float = DEFAULT_JUDGE_RETRY_DELAY_SECONDS,
    on_retry: Any | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    for attempt in range(0, max_retries + 1):
        try:
            return await _invoke_structured_once(
                provider=provider,
                base_url=base_url,
                model_name=model_name,
                schema=schema,
                prompt=prompt,
                api_key=api_key,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            if attempt >= max_retries or not _is_retryable_judge_error(exc, provider=provider):
                raise
            if callable(on_retry):
                on_retry(attempt + 1, max_retries, exc, retry_delay_seconds)
            await asyncio.sleep(retry_delay_seconds)


class BixBenchJudgeCommittee:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        model_map: dict[str, str],
        api_key: str,
        timeout_seconds: float,
        progress: JudgeProgressTracker | None = None,
    ) -> None:
        _require_dependencies()
        self.provider = _normalize_judge_provider(provider)
        self.base_url = str(base_url).strip()
        self.model_map = dict(model_map)
        self.api_key = api_key
        self.timeout_seconds = float(timeout_seconds)
        self.progress = progress
        graph: StateGraph[JudgeState] = StateGraph(JudgeState)
        graph.add_node("prepare", self._prepare)
        graph.add_node("stats_specialist", self._stats_specialist)
        graph.add_node("biology_specialist", self._biology_specialist)
        graph.add_node("evidence_checker", self._evidence_checker)
        graph.add_node("arbiter", self._arbiter)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "stats_specialist")
        graph.add_edge("stats_specialist", "biology_specialist")
        graph.add_edge("biology_specialist", "evidence_checker")
        graph.add_edge("evidence_checker", "arbiter")
        graph.add_edge("arbiter", END)
        self._graph = graph.compile()

    async def ainvoke(self, state: JudgeState) -> dict[str, Any]:
        return await self._graph.ainvoke(state)

    def _question_id(self, state: JudgeState) -> str:
        selection_row = state.get("selection_row") if isinstance(state.get("selection_row"), dict) else {}
        episode_record = state.get("episode_record") if isinstance(state.get("episode_record"), dict) else {}
        return str(
            _first_non_none(
                selection_row.get("question_id"),
                episode_record.get("question_id"),
                Path(str(state.get("episode_root") or "")).name,
                "unknown-question",
            )
        )

    def _run_stage(
        self,
        *,
        state: JudgeState,
        stage_name: str,
        model_role: str | None,
        schema: type[BaseModel] | None,
        prompt: str | None,
        deterministic_detail: str | None = None,
    ) -> tuple[str, float, str | None]:
        question_id = self._question_id(state)
        model_name = None
        if model_role:
            model_name = state["model_map"].get(model_role, DEFAULT_MODEL)
        if self.progress is not None:
            self.progress.start_stage(question_id, stage_name, model=model_name, detail=deterministic_detail)
        return question_id, time.perf_counter(), model_name

    def _complete_stage(
        self,
        *,
        question_id: str,
        stage_name: str,
        started_at: float,
        detail: str | None = None,
    ) -> None:
        if self.progress is not None:
            self.progress.complete_stage(
                question_id,
                stage_name,
                elapsed_seconds=max(0.0, time.perf_counter() - started_at),
                detail=detail,
            )

    def _fail_stage(
        self,
        *,
        question_id: str,
        stage_name: str,
        started_at: float,
        error: Exception,
    ) -> None:
        if self.progress is not None:
            self.progress.fail_stage(
                question_id,
                stage_name,
                elapsed_seconds=max(0.0, time.perf_counter() - started_at),
                error=str(error).strip() or type(error).__name__,
            )

    def _retry_stage(
        self,
        question_id: str,
        stage_name: str,
    ) -> Any:
        if self.progress is None:
            return None

        def _callback(attempt: int, max_retries: int, exc: Exception, delay_seconds: float) -> None:
            self.progress.retry_stage(
                question_id,
                stage_name,
                attempt=attempt,
                max_retries=max_retries,
                delay_seconds=delay_seconds,
                error=str(exc).strip() or type(exc).__name__,
            )

        return _callback

    async def _prepare(self, state: JudgeState) -> dict[str, Any]:
        question_id, started_at, _ = self._run_stage(
            state=state,
            stage_name="prepare",
            model_role=None,
            schema=None,
            prompt=None,
            deterministic_detail="load episode bundle",
        )
        try:
            episode_root = Path(state["episode_root"])
            selection_row = _ensure_question_metadata(state.get("selection_row") or {})
            record = load_episode_record(episode_root, selection_row)
            prompt_context = _json_prompt_context(record)
            numeric = int(record.get("numeric") or 0)
            payload = {
                "episode_record": record,
                "prompt_context": prompt_context,
                "weight_hint": _weight_hint(numeric),
                "model_map": dict(state.get("model_map") or self.model_map),
            }
        except Exception as exc:
            self._fail_stage(question_id=question_id, stage_name="prepare", started_at=started_at, error=exc)
            raise
        self._complete_stage(question_id=question_id, stage_name="prepare", started_at=started_at, detail="episode loaded")
        return payload

    async def _stats_specialist(self, state: JudgeState) -> dict[str, Any]:
        question_id, started_at, model_name = self._run_stage(
            state=state,
            stage_name="stats_specialist",
            model_role="stats_specialist",
            schema=SpecialistAssessment,
            prompt=None,
        )
        prompt = _role_prompt(
            "statistics specialist",
            state["prompt_context"],
            "Focus on the numeric/statistical content. Numeric questions can still be semantically phrased, "
            "so judge the meaning of the quantitative answer rather than the exact surface wording. "
            "Check ranges, units, directionality, magnitudes, and any statistical claim made by the answer. "
            "Use the reference answer only as a soft calibration signal, not as a strict pass/fail rule. "
            "Choose accept only when the numeric claim is accurate, evidence-grounded, and effectively aligned with the task. "
            "Choose mostly_supported when the numeric answer is useful and well supported but still has non-fatal caveats. "
            "Choose partial when the answer captures some correct quantitative meaning but misses or distorts an important detail. "
            "Choose weak when the numeric/statistical interpretation is substantially flawed. "
            "Choose unsupported when the visible trace does not support the numeric claim or the answer is materially wrong. "
            "Use failure codes like: numeric_mismatch, range_error, unit_error, unsupported_claim, trace_mismatch, missing_core_answer.",
        )
        try:
            assessment = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("stats_specialist", DEFAULT_MODEL),
                schema=SpecialistAssessment,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(question_id, "stats_specialist"),
            )
            assessment = _normalize_assessment_category(assessment)
        except Exception as exc:
            self._fail_stage(question_id=question_id, stage_name="stats_specialist", started_at=started_at, error=exc)
            raise
        self._complete_stage(
            question_id=question_id,
            stage_name="stats_specialist",
            started_at=started_at,
            detail=f"category={assessment.get('category')}",
        )
        return {"stats_assessment": assessment}

    async def _biology_specialist(self, state: JudgeState) -> dict[str, Any]:
        question_id, started_at, model_name = self._run_stage(
            state=state,
            stage_name="biology_specialist",
            model_role="biology_specialist",
            schema=SpecialistAssessment,
            prompt=None,
        )
        prompt = _role_prompt(
            "biology specialist",
            state["prompt_context"],
            "Focus on the biological meaning of the answer. Judge whether it captures the correct gene-level, "
            "pathway-level, disease-level, or precision-medicine interpretation, even when the wording differs "
            "from the benchmark. Numeric answers may still carry biological meaning, so do not ignore them. "
            "Use the reference answer only as a soft calibration signal, not as a strict pass/fail rule. "
            "Choose accept only when the biological interpretation is correct, useful, and well grounded. "
            "Choose mostly_supported when the biological conclusion is broadly right with only notable caveats. "
            "Choose partial when some biological meaning is right but important interpretation is missing or distorted. "
            "Choose weak when the biological interpretation is substantially flawed. "
            "Choose unsupported when the claimed interpretation is not supported by the visible evidence or is materially wrong. "
            "Use failure codes like: biological_misinterpretation, unsupported_claim, trace_mismatch, missing_core_answer, overclaim.",
        )
        try:
            assessment = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("biology_specialist", DEFAULT_MODEL),
                schema=SpecialistAssessment,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(question_id, "biology_specialist"),
            )
            assessment = _normalize_assessment_category(assessment)
        except Exception as exc:
            self._fail_stage(question_id=question_id, stage_name="biology_specialist", started_at=started_at, error=exc)
            raise
        self._complete_stage(
            question_id=question_id,
            stage_name="biology_specialist",
            started_at=started_at,
            detail=f"category={assessment.get('category')}",
        )
        return {"biology_assessment": assessment}

    async def _evidence_checker(self, state: JudgeState) -> dict[str, Any]:
        question_id, started_at, model_name = self._run_stage(
            state=state,
            stage_name="evidence_checker",
            model_role="evidence_checker",
            schema=EvidenceAssessment,
            prompt=None,
        )
        prompt = _role_prompt(
            "evidence checker",
            state["prompt_context"],
            "Focus on whether the returned answer is supported by the visible transcript and tool trace. "
            "Flag claims that look unsupported, overconfident, or inconsistent with the recorded tool activity. "
            "This is the main support gate for the committee. "
            "Choose accept only when the visible trace directly supports the core answer with no meaningful contradiction. "
            "Choose mostly_supported when support is strong but not perfectly exhaustive. "
            "Choose partial when the trace supports only part of the answer or leaves important gaps. "
            "Choose weak when support is thin and the answer overreaches. "
            "Choose unsupported when the answer is contradicted, invented, or not supported by the visible trace. "
            "Ignore the reference answer when assigning category; inspect the actual trace. "
            "Use failure codes like: unsupported_claim, trace_mismatch, contradiction, overclaim, missing_trace_support, evidence_gap.",
        )
        try:
            assessment = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("evidence_checker", DEFAULT_MODEL),
                schema=EvidenceAssessment,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(question_id, "evidence_checker"),
            )
            assessment = _normalize_assessment_category(assessment)
        except Exception as exc:
            self._fail_stage(question_id=question_id, stage_name="evidence_checker", started_at=started_at, error=exc)
            raise
        self._complete_stage(
            question_id=question_id,
            stage_name="evidence_checker",
            started_at=started_at,
            detail=f"category={assessment.get('category')}",
        )
        return {"evidence_assessment": assessment}

    async def _arbiter(self, state: JudgeState) -> dict[str, Any]:
        question_id, started_at, model_name = self._run_stage(
            state=state,
            stage_name="arbiter",
            model_role="arbiter",
            schema=JudgeVerdict,
            prompt=None,
        )
        stats = state.get("stats_assessment") or {}
        biology = state.get("biology_assessment") or {}
        evidence = state.get("evidence_assessment") or {}
        episode_record = state.get("episode_record") or {}
        committee_result = _compute_bixbench_committee_result(
            numeric=int((episode_record or {}).get("numeric") or 0),
            weights=state.get("weight_hint") or {},
            stats=stats,
            biology=biology,
            evidence=evidence,
            benchmark_correct=bool((episode_record or {}).get("correct")),
        )
        prompt = (
            "You are the final arbiter in a BixBench judge committee.\n\n"
            "Use the three specialist assessments and the deterministic committee summary to produce a qualitative synthesis only. "
            "Do not invent the official category or any official numeric score; those are computed separately in code. "
            "Your role is to summarize strengths, weaknesses, disagreement, and whether manual review is warranted.\n\n"
            "Return structured output only.\n\n"
            f"Deterministic committee summary: {compact_json(committee_result, 400)}\n"
            f"Weight hint: {compact_json(state.get('weight_hint') or {}, 200)}\n"
            f"Stats assessment: {compact_json(stats, 2000)}\n"
            f"Biology assessment: {compact_json(biology, 2000)}\n"
            f"Evidence assessment: {compact_json(evidence, 2000)}\n"
            f"Prompt context: {state.get('prompt_context')}\n"
        )
        try:
            verdict = await _invoke_structured(
                provider=self.provider,
                base_url=self.base_url,
                model_name=model_name or state["model_map"].get("arbiter", DEFAULT_MODEL),
                schema=JudgeVerdict,
                prompt=prompt,
                api_key=self.api_key,
                on_retry=self._retry_stage(question_id, "arbiter"),
            )
        except Exception as exc:
            self._fail_stage(question_id=question_id, stage_name="arbiter", started_at=started_at, error=exc)
            raise
        if committee_result.get("review_required") and not verdict.get("review_required"):
            verdict["review_required"] = True
            verdict["review_reasons"] = sorted(
                set((verdict.get("review_reasons") or []) + list(committee_result.get("review_reasons") or []))
            )
        self._complete_stage(
            question_id=question_id,
            stage_name="arbiter",
            started_at=started_at,
            detail=f"review_required={verdict.get('review_required')}",
        )
        return {"committee_result": committee_result, "arbiter_verdict": verdict}


@dataclass(frozen=True)
class BixBenchJudgeConfig:
    run_root: Path
    scope: str = "1"
    question_ids: tuple[str, ...] = ()
    limit: int = 0
    resume: bool = False
    timeout_seconds: float = 180.0
    provider: str = DEFAULT_JUDGE_PROVIDER
    base_url: str | None = None
    model: str = DEFAULT_MODEL
    stats_model: str | None = None
    biology_model: str | None = None
    evidence_model: str | None = None
    arbiter_model: str | None = None
    verbose: bool = False

    @property
    def model_map(self) -> dict[str, str]:
        default = self.model.strip() or DEFAULT_MODEL
        return {
            "stats_specialist": self.stats_model.strip() if self.stats_model else default,
            "biology_specialist": self.biology_model.strip() if self.biology_model else default,
            "evidence_checker": self.evidence_model.strip() if self.evidence_model else default,
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
            project_root=self.run_root,
        )
        return resolved_base_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the BixBench LangGraph judge committee over an existing run.")
    parser.add_argument("run_root", help="Existing BixBench run root to judge.")
    parser.add_argument("--config", default="", help="Optional JSON config file with judge overrides.")
    parser.add_argument(
        "--recompute-existing-scores",
        action="store_true",
        help="Recompute saved scored verdicts locally from specialist categories without provider calls.",
    )
    parser.add_argument("--question-id", action="append", default=[], help="Judge one exact BixBench question id.")
    parser.add_argument("--scope", default="1", help="BixBench scope filter. Use 'all' for every selected question.")
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of selected questions.")
    parser.add_argument("--resume", action="store_true", help="Skip questions that already have judge.json.")
    parser.add_argument("--timeout-seconds", type=float, default=180.0, help="Timeout per judged question in seconds.")
    parser.add_argument(
        "--provider",
        default=DEFAULT_JUDGE_PROVIDER,
        help="Judge provider to use (`openai` or `openrouter`).",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional provider base URL override. Defaults to the provider standard endpoint.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Default model for all committee nodes.")
    parser.add_argument("--stats-model", default="", help="Override the statistics specialist model.")
    parser.add_argument("--biology-model", default="", help="Override the biology specialist model.")
    parser.add_argument("--evidence-model", default="", help="Override the evidence checker model.")
    parser.add_argument("--arbiter-model", default="", help="Override the arbiter model.")
    parser.add_argument("--verbose", action="store_true", help="Print progress information.")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _load_config_overrides(path_text: str) -> dict[str, Any]:
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.exists():
        raise FileNotFoundError(path)
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Judge config must be a JSON object: {path}")
    return payload


def _resolved_config(args: argparse.Namespace) -> BixBenchJudgeConfig:
    overrides = _load_config_overrides(str(args.config or ""))
    run_root = Path(args.run_root)
    scope = str(args.scope or overrides.get("scope") or "1").strip()
    question_ids = tuple(
        str(item).strip()
        for item in (args.question_id or overrides.get("question_ids") or [])
        if str(item).strip()
    )
    limit = max(0, int(args.limit or overrides.get("limit") or 0))
    resume = bool(args.resume or overrides.get("resume", False))
    timeout_seconds = float(args.timeout_seconds or overrides.get("timeout_seconds") or 180.0)
    provider = str(args.provider or overrides.get("provider") or DEFAULT_JUDGE_PROVIDER).strip() or DEFAULT_JUDGE_PROVIDER
    base_url = str(args.base_url or overrides.get("base_url") or "").strip() or None
    model = str(args.model or overrides.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    stats_model = str(args.stats_model or overrides.get("stats_model") or "").strip() or None
    biology_model = str(args.biology_model or overrides.get("biology_model") or "").strip() or None
    evidence_model = str(args.evidence_model or overrides.get("evidence_model") or "").strip() or None
    arbiter_model = str(args.arbiter_model or overrides.get("arbiter_model") or "").strip() or None
    return BixBenchJudgeConfig(
        run_root=run_root,
        scope=scope,
        question_ids=question_ids,
        limit=limit,
        resume=resume,
        timeout_seconds=max(1.0, timeout_seconds),
        provider=provider,
        base_url=base_url,
        model=model,
        stats_model=stats_model,
        biology_model=biology_model,
        evidence_model=evidence_model,
        arbiter_model=arbiter_model,
        verbose=bool(args.verbose),
    )


def load_selected_question_rows(run_root: Path, config: BixBenchJudgeConfig) -> list[dict[str, Any]]:
    rows = load_run_question_rows(run_root)
    selected = select_bixbench_rows(rows, question_ids=config.question_ids, scope=config.scope)
    if config.limit > 0:
        selected = selected[: config.limit]
    return [_ensure_question_metadata(row) for row in selected]


def _judge_artifact_payload(
    *,
    run_root: Path,
    row: dict[str, Any],
    judge_state: dict[str, Any],
    config: BixBenchJudgeConfig,
) -> dict[str, Any]:
    episode_record = dict(judge_state.get("episode_record") or load_episode_record(run_root / "episodes" / str(row["question_id"]), row))
    committee_result = dict(judge_state.get("committee_result") or {})
    arbiter_verdict = dict(judge_state.get("arbiter_verdict") or {})
    specialists = {
        "stats_specialist": judge_state.get("stats_assessment") or {},
        "biology_specialist": judge_state.get("biology_assessment") or {},
        "evidence_checker": judge_state.get("evidence_assessment") or {},
    }
    benchmark_alignment = _benchmark_alignment_from_record(episode_record)
    arbiter_verdict["review_required"] = bool(
        _first_non_none(arbiter_verdict.get("review_required"), committee_result.get("review_required"), False)
    )
    arbiter_verdict["review_reasons"] = sorted(
        set((arbiter_verdict.get("review_reasons") or []) + list(committee_result.get("review_reasons") or []))
    )
    committee_level_mean_final = committee_result.get("committee_level_mean_final")
    return {
        "judge_schema_version": JUDGE_SCHEMA_VERSION,
        "created_at": current_utc_iso(),
        "run_root": str(run_root),
        "episode_root": str(run_root / "episodes" / str(row["question_id"])),
        "question_id": row.get("question_id"),
        "short_id": row.get("short_id"),
        "scope": row.get("scope"),
        "numeric": row.get("numeric"),
        "eval_mode": row.get("eval_mode"),
        "species": row.get("species"),
        "provider": config.resolved_provider,
        "base_url": config.resolved_base_url,
        "model_map": config.model_map,
        "resolved_model_map": config.resolved_model_map,
        "weight_hint": judge_state.get("weight_hint") or _weight_hint(int(row.get("numeric") or 0)),
        "tool_provenance": episode_record.get("tool_provenance") or build_tool_provenance(episode_record),
        "status": "scored",
        "benchmark_alignment": benchmark_alignment,
        "benchmark": {
            "score": episode_record.get("score"),
            "correct": episode_record.get("correct"),
            "match_reason": episode_record.get("match_reason"),
        },
        "specialists": specialists,
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
        "judge_score": judge_level_mean_normalized(None if committee_level_mean_final is None else float(committee_level_mean_final)),
        "judge": arbiter_verdict,
        "source_files": {
            "episode.json": str(run_root / "episodes" / str(row["question_id"]) / "episode.json"),
            "input.json": str(run_root / "episodes" / str(row["question_id"]) / "input.json"),
            "transcript.json": str(run_root / "episodes" / str(row["question_id"]) / "transcript.json"),
            "tool_calls.json": str(run_root / "episodes" / str(row["question_id"]) / "tool_calls.json"),
            "final_answer.json": str(run_root / "episodes" / str(row["question_id"]) / "final_answer.json"),
            "score.json": str(run_root / "episodes" / str(row["question_id"]) / "score.json"),
        },
    }


def _apply_judge_payload_to_record(record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return _merge_judge_fields(record, payload)


def _unanswered_judge_payload(
    *,
    run_root: Path,
    episode_root: Path,
    row: dict[str, Any],
    record: dict[str, Any],
    config: BixBenchJudgeConfig,
) -> dict[str, Any]:
    alignment = _benchmark_alignment_from_record(record)
    verdict = "abstained" if alignment == "abstained" else "unanswered"
    short_review = (
        "The agent abstained, so there is no final answer for the committee to score."
        if verdict == "abstained"
        else "No final answer was produced, so the committee did not score this episode."
    )
    return {
        "judge_schema_version": JUDGE_SCHEMA_VERSION,
        "created_at": current_utc_iso(),
        "run_root": str(run_root),
        "episode_root": str(episode_root),
        "question_id": row.get("question_id"),
        "short_id": row.get("short_id"),
        "scope": row.get("scope"),
        "numeric": row.get("numeric"),
        "eval_mode": row.get("eval_mode"),
        "species": row.get("species"),
        "provider": config.resolved_provider,
        "base_url": config.resolved_base_url,
        "model_map": config.model_map,
        "resolved_model_map": config.resolved_model_map,
        "weight_hint": _weight_hint(int(row.get("numeric") or 0)),
        "tool_provenance": record.get("tool_provenance") or build_tool_provenance(record),
        "status": "unscored",
        "benchmark_alignment": alignment,
        "benchmark": {
            "score": record.get("score"),
            "correct": record.get("correct"),
            "match_reason": record.get("match_reason"),
        },
        "specialists": {
            "stats_specialist": {},
            "biology_specialist": {},
            "evidence_checker": {},
        },
        "committee_category": None,
        "committee_category_label": None,
        "committee_level_mean_raw": None,
        "committee_level_mean_final": None,
        "cap_applied": False,
        "cap_reasons": [],
        "review_required": False,
        "review_reasons": [],
        "specialist_disagreement_span": None,
        "judge_score": None,
        "judge": {
            "short_review": short_review,
            "rationale": short_review,
            "major_strengths": [],
            "major_issues": [],
            "disagreement_flags": [],
            "review_required": False,
            "review_reasons": [],
        },
        "source_files": {
            "episode.json": str(episode_root / "episode.json"),
            "input.json": str(episode_root / "input.json"),
            "transcript.json": str(episode_root / "transcript.json"),
            "tool_calls.json": str(episode_root / "tool_calls.json"),
            "final_answer.json": str(episode_root / "final_answer.json"),
            "score.json": str(episode_root / "score.json"),
        },
    }


def _refresh_bixbench_judge_outputs(run_root: Path) -> dict[str, int]:
    """Refresh derived BixBench summaries after a local score migration."""

    run_manifest = read_json(run_root / "run_manifest.json")
    all_records: list[dict[str, Any]] = []
    for row in load_run_question_rows(run_root):
        question_id = str((row or {}).get("question_id") or "").strip()
        episode_root = run_root / "episodes" / question_id
        if question_id and episode_root.exists():
            all_records.append(load_episode_record(episode_root, row))

    judge_summary_root = run_root / "summary" / "judge"
    judge_portal_root = run_root / "portal" / "judge"
    canonical_summary_bundle = write_summary_artifacts(run_root, all_records)
    judge_summary_bundle = write_summary_artifacts(run_root, all_records, summary_root=judge_summary_root)
    write_html_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=canonical_summary_bundle,
        question_records=all_records,
        report_path=run_root / "summary" / "report.html",
        report_title="BixBench Run Report",
        run_manifest_href="../run_manifest.json",
        results_href="../results.json",
        questions_json_href="questions.json",
        portal_root_href="../portal",
    )
    write_portal(
        run_root=run_root,
        run_manifest=run_manifest,
        question_records=all_records,
        scope_summary=canonical_summary_bundle["scope_summary"],
        eval_mode_summary=canonical_summary_bundle["eval_mode_summary"],
        capsule_summary=canonical_summary_bundle["capsule_summary"],
        tool_usage_summary=canonical_summary_bundle["tool_usage_summary"],
        failure_summary=canonical_summary_bundle["failure_summary"],
        overall_summary=canonical_summary_bundle["overall_summary"],
        portal_root=run_root / "portal",
        episode_root_href_prefix="../../episodes",
    )
    write_json_atomic(
        run_root / "results.json",
        {
            "run_manifest": run_manifest,
            "summary": canonical_summary_bundle["overall_summary"],
            "question_count": len(all_records),
            "questions": build_judge_summary_rows(all_records),
            "summary_report_path": "summary/report.html",
            "summary_report_format": "html",
        },
    )
    write_html_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=judge_summary_bundle,
        question_records=all_records,
        report_path=judge_summary_root / "report.html",
        report_title="BixBench Judge Report",
        run_manifest_href="../../run_manifest.json",
        results_href="results.json",
        questions_json_href="questions.json",
        portal_root_href="../../portal/judge",
    )
    write_markdown_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=judge_summary_bundle,
        report_path=judge_summary_root / "report.md",
        report_title="BixBench Judge Report",
        run_manifest_href="../../run_manifest.json",
        results_href="results.json",
        questions_json_href="questions.json",
        portal_root_href="../../portal/judge",
    )
    write_portal(
        run_root=run_root,
        run_manifest=run_manifest,
        question_records=all_records,
        scope_summary=judge_summary_bundle["scope_summary"],
        eval_mode_summary=judge_summary_bundle["eval_mode_summary"],
        capsule_summary=judge_summary_bundle["capsule_summary"],
        tool_usage_summary=judge_summary_bundle["tool_usage_summary"],
        failure_summary=judge_summary_bundle["failure_summary"],
        overall_summary=judge_summary_bundle["overall_summary"],
        portal_root=judge_portal_root,
        episode_root_href_prefix="../../../episodes",
    )
    write_json_atomic(
        judge_summary_root / "results.json",
        {
            "run_manifest": run_manifest,
            "summary": judge_summary_bundle["overall_summary"],
            "question_count": len(all_records),
            "questions": build_judge_summary_rows(all_records),
            "summary_report_path": "summary/judge/report.html",
            "summary_report_format": "html",
            "judge_manifest_path": "summary/judge/judge_manifest.json",
            "portal_index_path": "portal/judge/index.html",
        },
    )
    return {"episode_count": len(all_records), "judged_count": sum(1 for record in all_records if record.get("judge"))}


def recompute_bixbench_scores(run_root: Path, *, question_ids: tuple[str, ...] = ()) -> dict[str, int]:
    """Recompute saved scored BixBench verdicts without provider calls.

    Only derived committee fields are rewritten. The saved specialist outputs,
    model metadata, and arbiter prose remain the original evidence of the
    completed judge run.
    """

    requested = {str(question_id).strip() for question_id in question_ids if str(question_id).strip()}
    rows_by_id = {str(row.get("question_id")): row for row in load_run_question_rows(run_root) if row.get("question_id")}
    updated_count = 0
    skipped_count = 0
    for judge_path in sorted((run_root / "episodes").glob("*/judge.json")):
        question_id = judge_path.parent.name
        if requested and question_id not in requested:
            continue
        payload = read_json(judge_path)
        if not isinstance(payload, dict) or payload.get("status") != "scored":
            skipped_count += 1
            continue
        specialists = payload.get("specialists") if isinstance(payload.get("specialists"), dict) else {}
        stats = specialists.get("stats_specialist") if isinstance(specialists.get("stats_specialist"), dict) else {}
        biology = specialists.get("biology_specialist") if isinstance(specialists.get("biology_specialist"), dict) else {}
        evidence = specialists.get("evidence_checker") if isinstance(specialists.get("evidence_checker"), dict) else {}
        if any(judge_category(item.get("category")) is None for item in (stats, biology, evidence)):
            skipped_count += 1
            continue
        row = rows_by_id.get(question_id, {})
        numeric = _normalize_numeric(payload.get("numeric"), fallback_row=row)
        weight_hint = payload.get("weight_hint") if isinstance(payload.get("weight_hint"), dict) else _weight_hint(numeric)
        benchmark = payload.get("benchmark") if isinstance(payload.get("benchmark"), dict) else {}
        committee_result = _compute_bixbench_committee_result(
            numeric=numeric,
            weights=weight_hint,
            stats=stats,
            biology=biology,
            evidence=evidence,
            benchmark_correct=bool(benchmark.get("correct")),
        )
        prior_schema_version = payload.get("judge_schema_version")
        arbiter = payload.get("judge") if isinstance(payload.get("judge"), dict) else {}
        arbiter = dict(arbiter)
        legacy_reasons = [
            str(reason).strip()
            for reason in (arbiter.get("review_reasons") or [])
            if str(reason).strip() and str(reason).strip() != "cap_applied"
        ]
        arbiter["review_required"] = bool(committee_result.get("review_required"))
        arbiter["review_reasons"] = sorted(set(legacy_reasons + list(committee_result.get("review_reasons") or [])))
        final_level_mean = committee_result.get("committee_level_mean_final")
        payload.update(
            {
                "judge_schema_version": JUDGE_SCHEMA_VERSION,
                "weight_hint": weight_hint,
                "committee_category": committee_result.get("committee_category"),
                "committee_category_label": committee_result.get("committee_category_label"),
                "committee_level_mean_raw": committee_result.get("committee_level_mean_raw"),
                "committee_level_mean_final": final_level_mean,
                "committee_level_mean_normalized": committee_result.get("committee_level_mean_normalized"),
                "judge_score": judge_level_mean_normalized(None if final_level_mean is None else float(final_level_mean)),
                "score_policy": committee_result.get("score_policy"),
                "cap_applied": False,
                "cap_reasons": [],
                "quality_flags": committee_result.get("quality_flags") or [],
                "review_required": committee_result.get("review_required"),
                "review_reasons": committee_result.get("review_reasons") or [],
                "specialist_disagreement_span": committee_result.get("specialist_disagreement_span"),
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

    output_counts = _refresh_bixbench_judge_outputs(run_root)
    return {"updated_count": updated_count, "skipped_count": skipped_count, **output_counts}


async def judge_bixbench_run_async(config: BixBenchJudgeConfig) -> Path:
    _require_dependencies()
    api_key, resolved_base_url = _resolve_judge_provider_credentials(
        provider=config.provider,
        base_url=config.base_url,
        project_root=config.run_root,
    )
    run_root = config.run_root
    judge_summary_root = run_root / "summary" / "judge"
    judge_portal_root = run_root / "portal" / "judge"
    run_manifest = read_json(run_root / "run_manifest.json")
    all_rows = load_run_question_rows(run_root)
    selected_rows = load_selected_question_rows(run_root, config)
    if not selected_rows:
        raise ValueError("No BixBench questions matched the requested judge selection.")

    progress = JudgeProgressTracker(
        summary_root=judge_summary_root,
        total_questions=len(selected_rows),
        run_label=f"BixBench judge {run_manifest.get('run_id') or run_root.name}",
        verbose=config.verbose,
    )
    committee = BixBenchJudgeCommittee(
        provider=config.resolved_provider,
        base_url=resolved_base_url,
        model_map=config.resolved_model_map,
        api_key=api_key,
        timeout_seconds=config.timeout_seconds,
        progress=progress,
    )
    judged_records: list[dict[str, Any]] = []
    judge_manifest = {
        "created_at": current_utc_iso(),
        "run_root": str(run_root),
        "run_id": run_manifest.get("run_id"),
        "selection": {
            "scope": config.scope,
            "question_ids": list(config.question_ids),
            "limit": config.limit,
            "resume": config.resume,
        },
        "timeout_seconds": config.timeout_seconds,
        "provider": config.resolved_provider,
        "base_url": resolved_base_url,
        "model_map": config.model_map,
        "resolved_model_map": config.resolved_model_map,
        "source_run_manifest": str(run_root / "run_manifest.json"),
        "selected_question_count": len(selected_rows),
        "outputs": {
            "summary_root": str(judge_summary_root),
            "portal_root": str(judge_portal_root),
            "results_path": str(judge_summary_root / "results.json"),
            "report_path": str(judge_summary_root / "report.html"),
            "markdown_report_path": str(judge_summary_root / "report.md"),
            "portal_index_path": str(judge_portal_root / "index.html"),
            "judge_manifest_path": str(judge_summary_root / "judge_manifest.json"),
            "progress_path": str(judge_summary_root / "progress.json"),
        },
    }
    write_json_atomic(judge_summary_root / "judge_manifest.json", judge_manifest)

    for index, row in enumerate(selected_rows, start=1):
        episode_root = run_root / "episodes" / str(row["question_id"])
        judge_path = episode_root / "judge.json"
        question_id = str(row.get("question_id") or episode_root.name)
        question_started_at = time.perf_counter()
        progress.start_question(question_id, episode_root=episode_root, index=index, total=len(selected_rows))
        existing_payload = None
        if judge_path.exists():
            loaded_payload = read_json(judge_path)
            if isinstance(loaded_payload, dict):
                existing_payload = loaded_payload
        if config.resume and judge_path.exists():
            loaded = load_episode_record(episode_root, row)
            judged_records.append(loaded)
            progress.skip_question(question_id, episode_root=episode_root, reason="resume_existing_judge")
            continue
        episode_record = load_episode_record(episode_root, row)
        if not _episode_has_public_answer(episode_record):
            payload = _unanswered_judge_payload(
                run_root=run_root,
                episode_root=episode_root,
                row=row,
                record=episode_record,
                config=config,
            )
            write_json_atomic(judge_path, payload)
            record = _apply_judge_payload_to_record(episode_record, payload)
            judged_records.append(record)
            progress.skip_question(question_id, episode_root=episode_root, reason="no_public_answer")
            continue
        try:
            judge_state = await asyncio.wait_for(
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
            payload = _judge_artifact_payload(run_root=run_root, row=row, judge_state=judge_state, config=config)
            progress.finish_question(question_id, status="completed", started_at_monotonic=question_started_at)
        except Exception as exc:
            error_text = str(exc).strip() or f"{type(exc).__name__} (no message)"
            error_payload = {
                "judge_schema_version": JUDGE_SCHEMA_VERSION,
                "created_at": current_utc_iso(),
                "run_root": str(run_root),
                "episode_root": str(episode_root),
                "question_id": row.get("question_id"),
                "short_id": row.get("short_id"),
                "scope": row.get("scope"),
                "numeric": row.get("numeric"),
                "eval_mode": row.get("eval_mode"),
                "species": row.get("species"),
                "provider": config.resolved_provider,
                "base_url": resolved_base_url,
                "model_map": config.model_map,
                "resolved_model_map": config.resolved_model_map,
                "weight_hint": _weight_hint(int(row.get("numeric") or 0)),
                "tool_provenance": episode_record.get("tool_provenance") or build_tool_provenance(episode_record),
                "error": error_text,
                "status": "error",
                "benchmark_alignment": _benchmark_alignment_from_record(episode_record),
                "committee_category": None,
                "committee_category_label": None,
                "committee_level_mean_raw": None,
                "committee_level_mean_final": None,
                "cap_applied": False,
                "cap_reasons": [],
                "review_required": True,
                "review_reasons": ["judge_error"],
                "specialist_disagreement_span": None,
                "judge_score": None,
                "judge": {
                    "short_review": "Judge failed.",
                    "rationale": error_text,
                    "major_strengths": [],
                    "major_issues": [error_text],
                    "disagreement_flags": ["judge_error"],
                    "review_required": True,
                    "review_reasons": ["judge_error"],
                },
                "specialists": {
                    "stats_specialist": {
                        "category": "unsupported",
                        "summary": "Judge failed.",
                        "key_observations": [],
                        "satisfied_criteria": [],
                        "failed_criteria": [error_text],
                        "failure_codes": ["judge_error"],
                    },
                    "biology_specialist": {
                        "category": "unsupported",
                        "summary": "Judge failed.",
                        "key_observations": [],
                        "satisfied_criteria": [],
                        "failed_criteria": [error_text],
                        "failure_codes": ["judge_error"],
                    },
                    "evidence_checker": {
                        "category": "unsupported",
                        "summary": "Judge failed.",
                        "key_observations": [],
                        "satisfied_criteria": [],
                        "failed_criteria": [error_text],
                        "failure_codes": ["judge_error"],
                        "matching_evidence": [],
                        "unsupported_claims": [error_text],
                        "evidence_gaps": [error_text],
                    },
                },
                "benchmark": {
                    "score": episode_record.get("score"),
                    "correct": episode_record.get("correct"),
                    "match_reason": episode_record.get("match_reason"),
                },
                "source_files": {
                    "episode.json": str(episode_root / "episode.json"),
                    "input.json": str(episode_root / "input.json"),
                    "transcript.json": str(episode_root / "transcript.json"),
                    "tool_calls.json": str(episode_root / "tool_calls.json"),
                    "final_answer.json": str(episode_root / "final_answer.json"),
                    "score.json": str(episode_root / "score.json"),
                },
            }
            write_json_atomic(episode_root / "judge_refresh_error.json", error_payload)
            payload = existing_payload if existing_payload is not None else error_payload
            progress.finish_question(question_id, status="failed", started_at_monotonic=question_started_at, note=error_text)
        write_json_atomic(judge_path, payload)
        record = load_episode_record(episode_root, row)
        record = _apply_judge_payload_to_record(record, payload)
        judged_records.append(record)

    all_records: list[dict[str, Any]] = []
    for row in all_rows:
        question_id = str((row or {}).get("question_id") or "").strip()
        if not question_id:
            continue
        episode_root = run_root / "episodes" / question_id
        if not episode_root.exists():
            continue
        all_records.append(load_episode_record(episode_root, row))

    canonical_summary_bundle = write_summary_artifacts(run_root, all_records)
    judge_summary_bundle = write_summary_artifacts(run_root, all_records, summary_root=judge_summary_root)
    # Refresh the canonical run-level dashboard with judge-enriched records.
    write_html_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=canonical_summary_bundle,
        question_records=all_records,
        report_path=run_root / "summary" / "report.html",
        report_title="BixBench Run Report",
        run_manifest_href="../run_manifest.json",
        results_href="../results.json",
        questions_json_href="questions.json",
        portal_root_href="../portal",
    )
    write_portal(
        run_root=run_root,
        run_manifest=run_manifest,
        question_records=all_records,
        scope_summary=canonical_summary_bundle["scope_summary"],
        eval_mode_summary=canonical_summary_bundle["eval_mode_summary"],
        capsule_summary=canonical_summary_bundle["capsule_summary"],
        tool_usage_summary=canonical_summary_bundle["tool_usage_summary"],
        failure_summary=canonical_summary_bundle["failure_summary"],
        overall_summary=canonical_summary_bundle["overall_summary"],
        portal_root=run_root / "portal",
        episode_root_href_prefix="../../episodes",
    )
    write_json_atomic(
        run_root / "results.json",
        {
            "run_manifest": run_manifest,
            "summary": canonical_summary_bundle["overall_summary"],
            "question_count": len(all_records),
            "questions": build_judge_summary_rows(all_records),
            "summary_report_path": "summary/report.html",
            "summary_report_format": "html",
        },
    )
    write_html_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=judge_summary_bundle,
        question_records=all_records,
        report_path=judge_summary_root / "report.html",
        report_title="BixBench Judge Report",
        run_manifest_href="../../run_manifest.json",
        results_href="results.json",
        questions_json_href="questions.json",
        portal_root_href="../../portal/judge",
    )
    write_markdown_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=judge_summary_bundle,
        report_path=judge_summary_root / "report.md",
        report_title="BixBench Judge Report",
        run_manifest_href="../../run_manifest.json",
        results_href="results.json",
        questions_json_href="questions.json",
        portal_root_href="../../portal/judge",
    )
    write_portal(
        run_root=run_root,
        run_manifest=run_manifest,
        question_records=all_records,
        scope_summary=judge_summary_bundle["scope_summary"],
        eval_mode_summary=judge_summary_bundle["eval_mode_summary"],
        capsule_summary=judge_summary_bundle["capsule_summary"],
        tool_usage_summary=judge_summary_bundle["tool_usage_summary"],
        failure_summary=judge_summary_bundle["failure_summary"],
        overall_summary=judge_summary_bundle["overall_summary"],
        portal_root=judge_portal_root,
        episode_root_href_prefix="../../../episodes",
    )
    write_json_atomic(
        judge_summary_root / "results.json",
        {
            "run_manifest": run_manifest,
            "summary": judge_summary_bundle["overall_summary"],
            "question_count": len(all_records),
            "questions": build_judge_summary_rows(all_records),
            "summary_report_path": "summary/judge/report.html",
            "summary_report_format": "html",
            "judge_manifest_path": "summary/judge/judge_manifest.json",
            "portal_index_path": "portal/judge/index.html",
        },
    )
    progress.finalize(status="completed")
    return run_root


def judge_bixbench_run(config: BixBenchJudgeConfig) -> Path:
    return asyncio.run(judge_bixbench_run_async(config))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.recompute_existing_scores:
        result = recompute_bixbench_scores(
            Path(args.run_root).resolve(),
            question_ids=tuple(str(item).strip() for item in args.question_id if str(item).strip()),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    config = _resolved_config(args)
    run_root = judge_bixbench_run(config)
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
