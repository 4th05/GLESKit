from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import csv
import html
import json
import os
import random
import re
from pathlib import Path
from statistics import mean, pstdev
import subprocess
import tempfile
import textwrap
import time
from dataclasses import dataclass
from typing import Any

from gleskit_mcp.config import load_settings
from gleskit_mcp.eval.bixbench import (
    build_tool_trace,
    build_visible_transcript,
    current_utc_iso,
    find_session_id_by_title,
    load_opencode_session_export,
    load_opencode_session_stats,
    opencode_activity_kind,
    read_json,
    render_html_page,
    run_id_from_time,
    sha256_file,
    sha256_text,
    write_json_atomic,
    write_text_atomic,
)
from gleskit_mcp.eval.bixbench_runner import (
    DEFAULT_BIXBENCH_MODEL,
    asset_has_forbidden_marker,
    configure_opencode_eval_agent,
    diagnose_opencode_runtime_failure,
    extend_opencode_tool_timeout,
    format_duration_human,
    harden_opencode_eval_runtime,
    make_retryable_error,
    normalize_skills_mode,
    opencode_subprocess_environment,
    probe_binary_version,
    probe_git_metadata,
    render_code_block,
    render_html_list_block,
    resolve_opencode_model,
    skills_mode_enabled,
    snapshot_single_file,
    snapshot_tool_schemas,
    snapshot_tree,
    sync_native_openai_oauth_from_codex,
    terminate_process,
)
from gleskit_mcp.eval.bixbench_judge import build_tool_provenance
from gleskit_mcp.eval.config import load_report_type_specs
from gleskit_mcp.eval.runtime import RuntimeHeartbeat
from gleskit_mcp.eval.repobench import (
    DEFAULT_REPOBENCH_RUNS_ROOT,
    agent_visible_task_payload,
    build_repobench_prompt,
    load_repobench_rows,
    repobench_dataset_path,
    select_repobench_rows,
    stage_repobench_input_artifacts,
)
from gleskit_mcp.eval.judge_bands import STANDARD_JUDGE_SCORE_BANDS, judge_band_kind, judge_category, judge_score_band
from gleskit_mcp.eval.scoring import build_evaluation_payload, deterministic_metrics, visible_sources_summary
from gleskit_mcp.report_templates import get_report_template
from gleskit_mcp.store import WorkspaceStore
from gleskit_mcp.workspace import WorkspaceService


REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPT_ASSET = REPO_ROOT / "docs" / "opencode" / "prompts" / "eval" / "repobench-eval-prompt.md"
PROMPT_ASSET_NO_SKILLS = REPO_ROOT / "docs" / "opencode" / "prompts" / "eval" / "repobench-eval-prompt-noskills.md"
OPENCODE_EVAL_AGENT_PROMPT = REPO_ROOT / "docs" / "opencode" / "prompts" / "agents" / "repobench-eval-agent-prompt.md"
OPENCODE_EVAL_AGENT_PROMPT_NO_SKILLS = (
    REPO_ROOT / "docs" / "opencode" / "prompts" / "agents" / "repobench-eval-agent-prompt-noskills.md"
)
OPENCODE_SKILL_ROOT = REPO_ROOT / "docs" / "opencode" / "skills" / "gleskit-repobench-eval"
OPENCODE_CONFIG_PATH = REPO_ROOT / "docs" / "opencode" / "opencode.json"
REPORT_TYPE_ASSET_ROOT = REPO_ROOT / "src" / "gleskit_mcp" / "eval" / "assets" / "report_types"
DEFAULT_REPOBENCH_MODEL = DEFAULT_BIXBENCH_MODEL
EVAL_PERSONA_FORBIDDEN_MARKERS = (
    "workspace_status",
    "gleskit-research-assistant",
    "references/10_workspace_mode.md",
    "references/11_uploads_mode.md",
)
REPOBENCH_JUDGE_SCORE_BANDS = STANDARD_JUDGE_SCORE_BANDS


@dataclass(frozen=True)
class RepoBenchRunConfig:
    run_id: str
    scope: str = "1"
    task_ids: tuple[str, ...] = ()
    report_type: str | None = None
    limit: int = 0
    random_count: int = 0
    random_seed: int | None = None
    resume: bool = False
    continue_mode: str = "fresh"
    dry_run: bool = False
    agent: str = "repobench-eval"
    skills_mode: str = "on"
    model: str | None = None
    opencode_bin: str = "opencode"
    timeout_seconds: int = 900
    max_turns: int = 14
    max_tool_calls: int = 36
    no_progress_cycles: int = 3
    poll_interval_seconds: int = 60
    opencode_tool_timeout_ms: int = 300000
    max_retries: int = 1
    verbose: bool = False


def _next_watchdog_state(
    *,
    activity_kind: str,
    current_signature: tuple[Any, ...],
    last_signature: tuple[Any, ...] | None,
    no_progress: int,
) -> tuple[int, bool, bool]:
    """Keep the idle watchdog separate from active MCP and model work."""
    if activity_kind == "mcp_tool":
        return 0, True, False
    if activity_kind == "provider":
        return 0, False, True
    if last_signature is not None and current_signature == last_signature:
        return no_progress + 1, False, False
    return 0, False, False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the RepoBench evaluation pipeline with OpenCode.")
    parser.add_argument("--run-id", default="", help="Optional explicit run id. Defaults to a timestamped id.")
    parser.add_argument("--task-id", action="append", default=[], help="Run one exact RepoBench task id.")
    parser.add_argument("--scope", default="1", help="RepoBench scope filter. Use 'all' for every selected task.")
    parser.add_argument("--report-type", default="", help="Optional report_type filter.")
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of selected tasks.")
    parser.add_argument(
        "--random-count",
        type=int,
        default=0,
        help="Randomly sample this many selected tasks after filters are applied.",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=None,
        help="Optional seed for random task sampling.",
    )
    parser.add_argument("--resume", action="store_true", help="Skip tasks whose episode bundle is already complete.")
    parser.add_argument(
        "--continue-mode",
        choices=("fresh", "resume", "append", "retry_failed"),
        default="fresh",
        help="How to handle an existing run id: fresh, resume planned tasks, append new tasks, or retry failed tasks.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan the run and write manifests without launching OpenCode.")
    parser.add_argument("--agent", default="repobench-eval", help="OpenCode agent to use.")
    parser.add_argument(
        "--skills-mode",
        choices=("on", "off"),
        default="on",
        help="Enable or disable the evaluation skill pack for this run (default: on).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_REPOBENCH_MODEL,
        help=f"OpenCode model to use (default: {DEFAULT_REPOBENCH_MODEL}).",
    )
    parser.add_argument("--opencode-bin", default="opencode", help="OpenCode binary to invoke.")
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Wall-clock timeout per task in seconds.")
    parser.add_argument("--max-turns", type=int, default=14, help="Maximum assistant turns before stopping an episode.")
    parser.add_argument("--max-tool-calls", type=int, default=36, help="Maximum tool calls before stopping an episode.")
    parser.add_argument("--no-progress-cycles", type=int, default=3, help="Maximum consecutive no-progress polls.")
    parser.add_argument("--poll-interval-seconds", type=int, default=60, help="Polling interval for the stall watchdog.")
    parser.add_argument(
        "--opencode-tool-timeout-ms",
        type=int,
        default=300000,
        help="Timeout for MCP tool calls inside OpenCode, in milliseconds (default: 300000).",
    )
    parser.add_argument("--max-retries", type=int, default=1, help="Retry count for transient OpenCode launch failures.")
    parser.add_argument("--verbose", action="store_true", help="Print progress information.")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def resolve_project_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return current.parents[3]


def normalize_continue_mode(value: Any, *, resume_flag: bool = False) -> str:
    token = str(value or "fresh").strip().lower() or "fresh"
    if token not in {"fresh", "resume", "append", "retry_failed"}:
        raise ValueError(
            f"Unsupported continue mode: {value!r}. Use fresh, resume, append, or retry_failed."
        )
    if resume_flag and token not in {"fresh", "resume"}:
        raise ValueError("--resume cannot be combined with --continue-mode append or retry_failed.")
    if resume_flag and token == "fresh":
        return "resume"
    return token


def build_selection_payload(config: RepoBenchRunConfig) -> dict[str, Any]:
    return {
        "scope": config.scope,
        "task_ids": list(config.task_ids),
        "report_type": config.report_type,
        "limit": config.limit,
        "random_count": config.random_count,
        "random_seed": config.random_seed,
        "resume": config.resume,
        "continue_mode": normalize_continue_mode(config.continue_mode, resume_flag=config.resume),
        "dry_run": config.dry_run,
        "skills_mode": normalize_skills_mode(config.skills_mode),
    }


def task_is_completed(record: dict[str, Any]) -> bool:
    return bool(record.get("report_saved"))


def task_needs_retry(record: dict[str, Any]) -> bool:
    return not task_is_completed(record)


def _sample_or_limit_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    random_count: int,
    random_seed: int | None,
) -> list[dict[str, Any]]:
    if random_count > 0 and rows:
        count = min(random_count, len(rows))
        generator = random.Random(random_seed)
        return generator.sample(rows, count)
    if limit > 0:
        return rows[:limit]
    return list(rows)


def prompt_asset_for_skills_mode(skills_mode: str) -> Path:
    return PROMPT_ASSET if skills_mode_enabled(skills_mode) else PROMPT_ASSET_NO_SKILLS


def agent_prompt_asset_for_skills_mode(skills_mode: str) -> Path:
    return OPENCODE_EVAL_AGENT_PROMPT if skills_mode_enabled(skills_mode) else OPENCODE_EVAL_AGENT_PROMPT_NO_SKILLS


def validate_eval_persona_assets(skills_mode: str = "on") -> None:
    normalized_mode = normalize_skills_mode(skills_mode)
    required_paths = [
        OPENCODE_CONFIG_PATH,
        agent_prompt_asset_for_skills_mode(normalized_mode),
        prompt_asset_for_skills_mode(normalized_mode),
    ]
    if skills_mode_enabled(normalized_mode):
        required_paths.extend(
            [
                OPENCODE_SKILL_ROOT / "SKILL.md",
                OPENCODE_SKILL_ROOT / "references" / "00_core_rules.md",
                OPENCODE_SKILL_ROOT / "references" / "12_tool_call_contract.md",
                OPENCODE_SKILL_ROOT / "references" / "20_gene_sets_and_signatures.md",
                OPENCODE_SKILL_ROOT / "references" / "40_literature_and_provenance.md",
                OPENCODE_SKILL_ROOT / "references" / "60_reports_and_notes.md",
            ]
        )
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "RepoBench evaluation persona assets are incomplete. Missing: " + ", ".join(missing)
        )

    config_payload = json.loads(OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
    agent_payload = (((config_payload.get("agent") or {}) if isinstance(config_payload, dict) else {}) or {}).get("repobench-eval")
    if not isinstance(agent_payload, dict):
        raise ValueError("OpenCode config is missing the repobench-eval agent definition.")
    permission_payload = agent_payload.get("permission") if isinstance(agent_payload.get("permission"), dict) else {}
    allowed_skills = (permission_payload.get("skill") or {}) if isinstance(permission_payload, dict) else {}
    if skills_mode_enabled(normalized_mode) and (
        not isinstance(allowed_skills, dict) or allowed_skills.get("gleskit-repobench-eval") != "allow"
    ):
        raise ValueError("repobench-eval agent must allow the gleskit-repobench-eval skill.")

    for path in required_paths[1:]:
        text = path.read_text(encoding="utf-8")
        for marker in EVAL_PERSONA_FORBIDDEN_MARKERS:
            if asset_has_forbidden_marker(text, marker):
                raise ValueError(f"RepoBench persona asset {path} contains forbidden marker {marker!r}.")


def load_prompt_template(skills_mode: str = "on") -> str:
    prompt_asset = prompt_asset_for_skills_mode(skills_mode)
    if not prompt_asset.exists():
        raise FileNotFoundError(
            f"Missing RepoBench OpenCode prompt template: {prompt_asset}. Run `make opencode_setup`."
        )
    validate_eval_persona_assets(skills_mode)
    return prompt_asset.read_text(encoding="utf-8").strip()


def make_session_title(run_id: str, task_id: str, attempt: int, *, nonce: str | None = None) -> str:
    suffix = f" {nonce}" if nonce else ""
    return f"GLESKit RepoBench {run_id} {task_id} a{attempt}{suffix}"


def build_opencode_command(
    *,
    config: RepoBenchRunConfig,
    project_dir: Path,
    session_title: str,
    prompt_text: str,
) -> list[str]:
    command = [
        config.opencode_bin,
        "run",
        prompt_text,
        "--format",
        "json",
        "--agent",
        config.agent,
        "--title",
        session_title,
        "--dir",
        str(project_dir),
    ]
    if config.model:
        command.extend(["--model", config.model])
    return command


def _report_type_snapshots(selected_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    report_types = sorted({str(row.get("report_type") or "").strip() for row in selected_rows if str(row.get("report_type") or "").strip()})
    snapshots: list[dict[str, Any]] = []
    for report_type in report_types:
        path = REPORT_TYPE_ASSET_ROOT / f"{report_type}.json"
        snapshots.append(snapshot_single_file(path))
    return snapshots


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_manifest_payload(
    *,
    project_root: Path,
    run_root: Path,
    settings: Any,
    config: RepoBenchRunConfig,
    dataset_path: Path,
    dataset_rows: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]],
    staged_inputs: list[dict[str, Any]],
    tool_schema_snapshot: dict[str, Any],
    prompt_template: str,
) -> dict[str, Any]:
    prompt_asset = prompt_asset_for_skills_mode(config.skills_mode)
    agent_prompt_asset = agent_prompt_asset_for_skills_mode(config.skills_mode)
    return {
        "run_id": config.run_id,
        "created_at": current_utc_iso(),
        "project_root": str(project_root),
        "run_root": str(run_root),
        "workspace_home": str(settings.workspace_default_root),
        "runtime_home": str(settings.runtime_root),
        "dataset": {
            "path": str(dataset_path),
            "sha256": sha256_file(dataset_path),
            "row_count": len(dataset_rows),
            "selected_rows": selected_rows,
        },
        "selection": {
            **build_selection_payload(config),
            "selected_task_count": len(selected_rows),
        },
        "opencode": {
            "binary": config.opencode_bin,
            "binary_version": probe_binary_version(config.opencode_bin),
            "agent": config.agent,
            "skills_mode": normalize_skills_mode(config.skills_mode),
            "skills_enabled": skills_mode_enabled(config.skills_mode),
            "model": config.model,
            "timeout_seconds": config.timeout_seconds,
            "tool_timeout_ms": config.opencode_tool_timeout_ms,
            "max_turns": config.max_turns,
            "max_tool_calls": config.max_tool_calls,
            "no_progress_cycles": config.no_progress_cycles,
            "poll_interval_seconds": config.poll_interval_seconds,
            "max_retries": config.max_retries,
            "config_path": str(OPENCODE_CONFIG_PATH),
            "config_snapshot": snapshot_single_file(OPENCODE_CONFIG_PATH),
            "agent_prompt_path": str(agent_prompt_asset),
            "agent_prompt_snapshot": snapshot_single_file(agent_prompt_asset),
            "skill_root": str(OPENCODE_SKILL_ROOT),
            "skill_snapshot": snapshot_tree(OPENCODE_SKILL_ROOT),
        },
        "git": probe_git_metadata(project_root),
        "prompt_asset": snapshot_single_file(prompt_asset),
        "report_type_snapshots": _report_type_snapshots(selected_rows),
        "report_type_registry_snapshot": snapshot_tree(REPORT_TYPE_ASSET_ROOT),
        "tool_schema_snapshot": tool_schema_snapshot,
        "staged_inputs": staged_inputs,
        "planned_episode_count": len(selected_rows),
        "prompt_template_sha256": sha256_text(prompt_template),
    }


def load_existing_manifest(run_root: Path) -> dict[str, Any] | None:
    manifest_path = run_root / "run_manifest.json"
    if not manifest_path.exists():
        return None
    return read_json(manifest_path)


def load_existing_episode_records(run_root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    episodes_root = run_root / "episodes"
    if not episodes_root.exists():
        return records
    for episode_root in sorted(path for path in episodes_root.iterdir() if path.is_dir()):
        episode_path = episode_root / "episode.json"
        if not episode_path.exists():
            continue
        try:
            payload = read_json(episode_path)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        task_id = str(payload.get("task_id") or episode_root.name).strip()
        if task_id:
            records[task_id] = payload
    return records


def load_existing_planned_matrix(run_root: Path) -> list[dict[str, Any]]:
    planned_path = run_root / "planned_matrix.json"
    if not planned_path.exists():
        return []
    payload = read_json(planned_path)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def load_existing_planned_task_order(
    run_root: Path,
    existing_manifest: dict[str, Any] | None,
    existing_records: dict[str, dict[str, Any]],
) -> list[str]:
    planned_entries = load_existing_planned_matrix(run_root)
    if planned_entries:
        ordered = [
            str(item.get("task_id") or item.get("episode_id") or "").strip()
            for item in planned_entries
            if str(item.get("task_id") or item.get("episode_id") or "").strip()
        ]
        if ordered:
            seen: set[str] = set()
            result: list[str] = []
            for task_id in ordered:
                if task_id not in seen:
                    seen.add(task_id)
                    result.append(task_id)
            for task_id in existing_records.keys():
                if task_id not in seen:
                    seen.add(task_id)
                    result.append(task_id)
            return result
    selected_rows = (((existing_manifest or {}).get("dataset") or {}).get("selected_rows") or [])
    if isinstance(selected_rows, list):
        ordered = [
            str(item.get("task_id") or "").strip()
            for item in selected_rows
            if isinstance(item, dict) and str(item.get("task_id") or "").strip()
        ]
        if ordered:
            seen = set(ordered)
            for task_id in existing_records.keys():
                if task_id not in seen:
                    ordered.append(task_id)
            return ordered
    return list(existing_records.keys())


def merge_report_type_snapshots(
    existing_manifest: dict[str, Any] | None,
    current_snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in ((existing_manifest or {}).get("report_type_snapshots") or []):
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            if path:
                merged[path] = item
    for item in current_snapshots:
        path = str(item.get("path") or "").strip()
        if path:
            merged[path] = item
    return [merged[key] for key in sorted(merged)]


def merge_staged_inputs(
    existing_manifest: dict[str, Any] | None,
    current_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in ((existing_manifest or {}).get("staged_inputs") or []):
        if isinstance(item, dict):
            task_id = str(item.get("task_id") or "").strip()
            if task_id:
                merged[task_id] = item
    for item in current_rows:
        task_id = str(item.get("task_id") or "").strip()
        if task_id:
            merged[task_id] = item
    return [merged[key] for key in sorted(merged)]


def build_selection_history_event(
    config: RepoBenchRunConfig,
    *,
    continue_mode: str,
    selected_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "created_at": current_utc_iso(),
        "continue_mode": continue_mode,
        "selection": build_selection_payload(config),
        "selected_task_ids": [str(row.get("task_id") or "").strip() for row in selected_rows],
        "selected_task_count": len(selected_rows),
    }


def merge_episode_attempt_history(
    existing_record: dict[str, Any] | None,
    new_record: dict[str, Any],
) -> dict[str, Any]:
    if not existing_record:
        return new_record
    prior_attempts = existing_record.get("attempts") if isinstance(existing_record.get("attempts"), list) else []
    current_attempts = new_record.get("attempts") if isinstance(new_record.get("attempts"), list) else []
    combined_attempts = [*prior_attempts, *current_attempts]
    new_record["attempts"] = combined_attempts
    new_record["attempt_count"] = len(combined_attempts)
    new_record["retry_count"] = max(0, len(combined_attempts) - 1)
    new_record["total_duration_seconds"] = round(
        sum(float(item.get("duration_seconds") or 0.0) for item in combined_attempts),
        3,
    )
    return new_record


def next_attempt_number(episode_root: Path) -> int:
    attempts_root = episode_root / "private" / "attempts"
    highest = 0
    if attempts_root.exists():
        for path in attempts_root.iterdir():
            if not path.is_dir():
                continue
            match = re.fullmatch(r"attempt_(\d+)", path.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def merge_records_by_order(
    ordered_task_ids: list[str],
    records_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task_id in ordered_task_ids:
        payload = records_by_id.get(task_id)
        if payload is None:
            continue
        merged.append(payload)
        seen.add(task_id)
    for task_id, payload in records_by_id.items():
        if task_id not in seen:
            merged.append(payload)
    return merged


def select_rows_for_continue_mode(
    *,
    dataset_rows: list[dict[str, Any]],
    config: RepoBenchRunConfig,
    continue_mode: str,
    run_root: Path,
    existing_manifest: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    filtered_rows = select_repobench_rows(
        dataset_rows,
        task_ids=config.task_ids,
        scope=config.scope,
        report_type=config.report_type,
    )
    existing_records = load_existing_episode_records(run_root)
    existing_order = load_existing_planned_task_order(run_root, existing_manifest, existing_records)
    existing_ids = set(existing_order) | set(existing_records.keys())
    filtered_by_id = {
        str(row.get("task_id") or "").strip(): row
        for row in filtered_rows
        if str(row.get("task_id") or "").strip()
    }

    if continue_mode == "fresh":
        selected_rows = _sample_or_limit_rows(
            filtered_rows,
            limit=config.limit,
            random_count=config.random_count,
            random_seed=config.random_seed,
        )
        return selected_rows, existing_records, [str(row["task_id"]) for row in selected_rows]

    if not existing_manifest and not existing_records:
        raise FileNotFoundError(
            f"Run root does not contain an existing RepoBench run to continue: {run_root}"
        )

    if continue_mode == "resume":
        resume_candidates = [
            filtered_by_id[task_id]
            for task_id in existing_order
            if task_id in filtered_by_id and task_needs_retry(existing_records.get(task_id, {}))
        ]
        selected_rows = _sample_or_limit_rows(
            resume_candidates,
            limit=config.limit,
            random_count=config.random_count,
            random_seed=config.random_seed,
        )
        return selected_rows, existing_records, list(existing_order)

    if continue_mode == "append":
        append_candidates = [
            row
            for row in filtered_rows
            if str(row.get("task_id") or "").strip() not in existing_ids
        ]
        selected_rows = _sample_or_limit_rows(
            append_candidates,
            limit=config.limit,
            random_count=config.random_count,
            random_seed=config.random_seed,
        )
        appended_ids = [str(row["task_id"]) for row in selected_rows]
        combined_order = list(existing_order)
        for task_id in appended_ids:
            if task_id not in combined_order:
                combined_order.append(task_id)
        return selected_rows, existing_records, combined_order

    retry_candidates = [
        filtered_by_id[task_id]
        for task_id in existing_order
        if task_id in filtered_by_id
        and task_id in existing_records
        and task_needs_retry(existing_records.get(task_id, {}))
    ]
    selected_rows = _sample_or_limit_rows(
        retry_candidates,
        limit=config.limit,
        random_count=config.random_count,
        random_seed=config.random_seed,
    )
    return selected_rows, existing_records, list(existing_order)


def _assert_isolated_eval_roots(*, settings: Any, workspace_root: Path, runtime_root: Path) -> None:
    expected_workspace_root = Path(workspace_root).resolve()
    expected_runtime_root = Path(runtime_root).resolve()
    resolved_workspace_root = Path(settings.workspace_default_root).resolve()
    resolved_runtime_root = Path(settings.runtime_root).resolve()
    if resolved_workspace_root != expected_workspace_root:
        raise RuntimeError(
            "RepoBench evaluation refused to start because the resolved workspace root does not match the run-local "
            f"workspace. expected={expected_workspace_root} resolved={resolved_workspace_root}"
        )
    if resolved_runtime_root != expected_runtime_root:
        raise RuntimeError(
            "RepoBench evaluation refused to start because the resolved runtime root does not match the run-local "
            f"runtime. expected={expected_runtime_root} resolved={resolved_runtime_root}"
        )


def _workspace_service_for_root(*, workspace_root: Path, settings: Any) -> WorkspaceService:
    return WorkspaceService(WorkspaceStore(Path(workspace_root).resolve()), settings=settings)


@contextmanager
def _temporary_eval_paths(*, workspace_root: Path, runtime_root: Path):
    previous_env = {
        "GLESKIT_WORKSPACE_HOME": os.environ.get("GLESKIT_WORKSPACE_HOME"),
        "GLESKIT_RUNTIME_HOME": os.environ.get("GLESKIT_RUNTIME_HOME"),
    }
    os.environ["GLESKIT_WORKSPACE_HOME"] = str(workspace_root)
    os.environ["GLESKIT_RUNTIME_HOME"] = str(runtime_root)
    try:
        yield
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _create_gleskit_session(
    *,
    workspace_root: Path,
    settings: Any,
    row: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    service = _workspace_service_for_root(workspace_root=workspace_root, settings=settings)
    title = str(row.get("title") or row.get("task_id") or "RepoBench task").strip()
    # Session metadata is visible through regular GLESKit tools, so it must
    # contain the user task rather than evaluator-only success criteria.
    description = str(row.get("task_prompt") or "").strip()
    species = str(row.get("species") or "human").strip() or "human"
    tags = [
        "gleskit-repobench",
        f"run:{run_id}",
        f"task:{row.get('task_id')}",
        f"report_type:{row.get('report_type')}",
    ]
    return service.create_session(title=title, description=description, species=species, tags=tags)


def _fetch_saved_report(*, workspace_root: Path, settings: Any, session_id: str) -> dict[str, Any]:
    service = _workspace_service_for_root(workspace_root=workspace_root, settings=settings)
    return service.get_session_report(session_id)


def _allow_eval_read_paths(opencode_config_path: Path, paths: list[Path]) -> None:
    if not opencode_config_path.exists():
        return
    try:
        config = json.loads(opencode_config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    agent_payload = (((config.get("agent") or {}) if isinstance(config, dict) else {}) or {}).get("repobench-eval")
    if not isinstance(agent_payload, dict):
        return
    permission_payload = agent_payload.setdefault("permission", {})
    if not isinstance(permission_payload, dict):
        return
    read_rules = permission_payload.setdefault("read", {})
    external_rules = permission_payload.setdefault("external_directory", {})
    if not isinstance(read_rules, dict) or not isinstance(external_rules, dict):
        return
    for raw_path in paths:
        resolved = Path(raw_path).resolve()
        patterns = [str(resolved)]
        if resolved.is_dir():
            patterns.append(f"{resolved}/**")
        for pattern in patterns:
            read_rules[pattern] = "allow"
            external_rules[pattern] = "allow"
    write_json_atomic(opencode_config_path, config)


def _tool_trace_for_metrics(tool_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in tool_trace:
        rows.append(
            {
                "name": item.get("tool"),
                "arguments": item.get("input") or {},
                "payload": item.get("output"),
                "llm_payload": json.dumps(item.get("output"), indent=2, sort_keys=True, default=str),
            }
        )
    return rows


def _timestamp_to_seconds(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            return numeric / 1000.0
        return numeric
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
        if numeric > 10_000_000_000:
            return numeric / 1000.0
        return numeric
    except Exception:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _time_to_first_report(session_export: dict[str, Any], tool_trace: list[dict[str, Any]]) -> float | None:
    if not session_export:
        return None
    timestamps = [
        _timestamp_to_seconds(message.get("time_created"))
        for message in session_export.get("messages") or []
    ]
    start_ts = min((ts for ts in timestamps if ts is not None), default=None)
    if start_ts is None:
        return None
    report_calls = [
        item
        for item in tool_trace
        if str(item.get("tool") or "").strip() in {"create_session_report", "update_session_report", "get_session_report"}
    ]
    report_ts = min(
        (_timestamp_to_seconds(item.get("time_created")) for item in report_calls),
        default=None,
    )
    if report_ts is None:
        return None
    delta = report_ts - start_ts
    if delta < 0:
        return None
    return round(delta, 3)


def _report_revision_count(tool_trace: list[dict[str, Any]]) -> int:
    return sum(
        1
        for item in tool_trace
        if str(item.get("tool") or "").strip() in {"create_session_report", "update_session_report"}
        and str(item.get("status") or "").strip().lower() == "completed"
    )


def _report_exists(report_payload: dict[str, Any]) -> bool:
    return bool(report_payload.get("exists")) and bool(str(report_payload.get("body_markdown") or "").strip())


def build_input_payload(
    *,
    row: dict[str, Any],
    run_id: str,
    episode_root: Path,
    session_title: str,
    prompt_text: str,
    opencode_command: list[str],
    staged_inputs: dict[str, Any],
    gleskit_session: dict[str, Any],
    config: RepoBenchRunConfig,
) -> dict[str, Any]:
    prompt_asset = prompt_asset_for_skills_mode(config.skills_mode)
    return {
        "run_id": run_id,
        "episode_root": str(episode_root),
        "session_title": session_title,
        "task": agent_visible_task_payload(row),
        "staged_inputs": staged_inputs,
        "gleskit_session": gleskit_session,
        "prompt": {
            "path": str(prompt_asset),
            "sha256": sha256_file(prompt_asset),
            "text": prompt_text,
            "text_sha256": sha256_text(prompt_text),
        },
        "opencode": {
            "binary": config.opencode_bin,
            "agent": config.agent,
            "skills_mode": normalize_skills_mode(config.skills_mode),
            "model": config.model,
            "command": opencode_command,
        },
    }


def run_opencode_attempt(
    *,
    project_root: Path,
    run_root: Path,
    workspace_root: Path,
    runtime_root: Path,
    settings: Any,
    row: dict[str, Any],
    spec: Any,
    staged_inputs: dict[str, Any],
    config: RepoBenchRunConfig,
    attempt: int,
    prompt_text: str,
    gleskit_session: dict[str, Any],
) -> dict[str, Any]:
    episode_root = run_root / "episodes" / str(row["task_id"])
    attempt_root = episode_root / "private" / "attempts" / f"attempt_{attempt:02d}"
    attempt_root.mkdir(parents=True, exist_ok=True)
    opencode_home = attempt_root / "opencode-home"
    if opencode_home.exists():
        subprocess.run(["rm", "-rf", str(opencode_home)], check=False)
    opencode_home.mkdir(parents=True, exist_ok=True)

    source_opencode_config = REPO_ROOT / "docs" / "opencode"
    source_opencode_auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    target_opencode_config = opencode_home / ".config" / "opencode"
    if source_opencode_config.exists():
        target_opencode_config.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["cp", "-R", str(source_opencode_config), str(target_opencode_config)],
            check=False,
        )
        opencode_config_copy = target_opencode_config / "opencode.json"
        harden_opencode_eval_runtime(opencode_config_copy)
        extend_opencode_tool_timeout(opencode_config_copy, timeout_ms=config.opencode_tool_timeout_ms)
        stage_root_raw = str(staged_inputs.get("stage_root") or "").strip()
        _allow_eval_read_paths(
            opencode_config_copy,
            [Path(stage_root_raw)] if stage_root_raw else [],
        )
        configure_opencode_eval_agent(
            opencode_config_copy,
            agent_name=config.agent,
            agent_prompt_path=agent_prompt_asset_for_skills_mode(config.skills_mode),
            skill_name="gleskit-repobench-eval",
            skill_root_name=OPENCODE_SKILL_ROOT.name,
            skills_mode=config.skills_mode,
        )
    if source_opencode_auth.exists():
        target_auth = opencode_home / ".local" / "share" / "opencode" / "auth.json"
        target_auth.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", str(source_opencode_auth), str(target_auth)], check=False)
    if str(config.model or "").split("/", 1)[0].lower() == "openai":
        target_auth = opencode_home / ".local" / "share" / "opencode" / "auth.json"
        target_auth.parent.mkdir(parents=True, exist_ok=True)
        sync_native_openai_oauth_from_codex(target_auth)

    session_title = make_session_title(config.run_id, str(row["task_id"]), attempt)
    live_session_title = make_session_title(config.run_id, str(row["task_id"]), attempt, nonce=str(time.time_ns()))
    opencode_command = build_opencode_command(
        config=config,
        project_dir=episode_root,
        session_title=live_session_title,
        prompt_text=prompt_text,
    )
    input_payload = build_input_payload(
        row=row,
        run_id=config.run_id,
        episode_root=episode_root,
        session_title=session_title,
        prompt_text=prompt_text,
        opencode_command=opencode_command,
        staged_inputs=staged_inputs,
        gleskit_session=gleskit_session,
        config=config,
    )
    input_payload["live_session_title"] = live_session_title
    write_json_atomic(attempt_root / "input.json", input_payload)

    stdout_path = attempt_root / "opencode.stdout.log"
    stderr_path = attempt_root / "opencode.stderr.log"
    started_at = time.monotonic()
    opencode_session_id: str | None = None
    last_signature: tuple[Any, ...] | None = None
    no_progress = 0
    active_tool_started_at: float | None = None
    watchdog_triggered = False
    stop_reason = "completed"
    returncode: int | None = None

    previous_env = {
        "HOME": os.environ.get("HOME"),
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME"),
        "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
    }
    os.environ["HOME"] = str(opencode_home)
    os.environ["XDG_CONFIG_HOME"] = str(opencode_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(opencode_home / ".local" / "share")
    os.environ["XDG_STATE_HOME"] = str(opencode_home / ".local" / "state")
    os.environ["XDG_CACHE_HOME"] = str(opencode_home / ".cache")
    db_path = opencode_home / ".local" / "share" / "opencode" / "opencode.db"
    runtime_log_dir = opencode_home / ".local" / "share" / "opencode" / "log"

    try:
        env = opencode_subprocess_environment(config.model)
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
        env.setdefault("CI", "1")
        env["GLESKIT_WORKSPACE_HOME"] = str(Path(workspace_root).resolve())
        env["GLESKIT_RUNTIME_HOME"] = str(Path(runtime_root).resolve())
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            process = subprocess.Popen(
                opencode_command,
                cwd=episode_root,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
            try:
                while True:
                    returncode = process.poll()
                    if returncode is not None:
                        break
                    elapsed = time.monotonic() - started_at
                    runtime_failure = diagnose_opencode_runtime_failure(runtime_log_dir)
                    if runtime_failure is not None:
                        stop_reason = str(runtime_failure.get("stop_reason") or "provider_failure")
                        watchdog_triggered = True
                        break
                    if opencode_session_id is None:
                        opencode_session_id = find_session_id_by_title(db_path, live_session_title)
                    if opencode_session_id is not None:
                        stats = load_opencode_session_stats(db_path, opencode_session_id)
                        current_signature = (
                            stats["message_count"],
                            stats["assistant_message_count"],
                            stats["part_count"],
                            stats["tool_part_count"],
                            stats.get("active_tool_count", 0),
                            stats.get("pending_provider_count", 0),
                            stats["last_message_updated"],
                            stats["last_part_updated"],
                        )
                        activity_kind = opencode_activity_kind(stats)
                        no_progress, active_tool, pending_provider = _next_watchdog_state(
                            activity_kind=activity_kind,
                            current_signature=current_signature,
                            last_signature=last_signature,
                            no_progress=no_progress,
                        )
                        if active_tool:
                            if active_tool_started_at is None:
                                active_tool_started_at = time.monotonic()
                            if time.monotonic() - active_tool_started_at >= max(
                                1.0,
                                float(config.opencode_tool_timeout_ms) / 1000.0,
                            ):
                                stop_reason = "tool_timeout"
                                watchdog_triggered = True
                                break
                        else:
                            active_tool_started_at = None
                        last_signature = current_signature
                        if stats["assistant_message_count"] >= config.max_turns:
                            stop_reason = "turn_limit"
                            watchdog_triggered = True
                            break
                        if stats["tool_part_count"] >= config.max_tool_calls:
                            stop_reason = "tool_call_limit"
                            watchdog_triggered = True
                            break
                        if no_progress >= config.no_progress_cycles:
                            stop_reason = "no_progress"
                            watchdog_triggered = True
                            break
                    else:
                        pending_provider = False
                    if elapsed >= config.timeout_seconds:
                        stop_reason = "provider_timeout" if pending_provider else "wall_timeout"
                        watchdog_triggered = True
                        break
                    time.sleep(config.poll_interval_seconds)
            finally:
                if watchdog_triggered:
                    terminate_process(process)
                returncode = process.wait()
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if opencode_session_id is None:
        opencode_session_id = find_session_id_by_title(db_path, live_session_title)
    if stop_reason == "completed" and returncode not in (0, None):
        stop_reason = "opencode_exit_nonzero"
    if opencode_session_id is None and stop_reason == "completed":
        stop_reason = "session_missing"

    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    runtime_log_dir = opencode_home / ".local" / "share" / "opencode" / "log"
    runtime_failure = diagnose_opencode_runtime_failure(runtime_log_dir)

    stats = load_opencode_session_stats(db_path, opencode_session_id) if opencode_session_id else {
        "session_exists": False,
        "message_count": 0,
        "assistant_message_count": 0,
        "part_count": 0,
        "tool_part_count": 0,
        "pending_provider_count": 0,
        "pending_provider_started_at": None,
        "last_message_updated": None,
        "last_part_updated": None,
    }

    session_export: dict[str, Any] | None = None
    transcript: list[dict[str, Any]] = []
    tool_trace: list[dict[str, Any]] = []
    if opencode_session_id is not None:
        try:
            session_export = load_opencode_session_export(db_path, opencode_session_id)
            transcript = build_visible_transcript(session_export)
            tool_trace = build_tool_trace(session_export)
        except Exception as exc:
            session_export = {"session_id": opencode_session_id, "error": str(exc)}

    gleskit_session_id = str(gleskit_session.get("session_id") or "").strip()
    report_payload = _fetch_saved_report(
        workspace_root=workspace_root,
        settings=settings,
        session_id=gleskit_session_id,
    ) if gleskit_session_id else {
        "exists": False,
        "body_markdown": "",
        "report_id": None,
    }
    report_saved = _report_exists(report_payload)
    report_markdown = str(report_payload.get("body_markdown") or report_payload.get("markdown") or "").strip()
    report_json = {
        "report_id": report_payload.get("report_id") or report_payload.get("latest_report_id"),
        "session_id": gleskit_session_id,
        "template_id": report_payload.get("template_id") or spec.template_id,
        "report_type": spec.report_type,
        "title": report_payload.get("title") or row.get("title"),
        "body_markdown": report_markdown,
        "linked_artifact_ids": report_payload.get("linked_artifact_ids") or [],
        "linked_artifacts": report_payload.get("linked_artifacts") or [],
        "visible_sources_summary": visible_sources_summary(report_markdown),
        "report_source_metadata": {
            "path": report_payload.get("path"),
            "created_at": report_payload.get("created_at"),
            "updated_at": report_payload.get("updated_at"),
            "report_count": report_payload.get("report_count"),
            "latest_report_id": report_payload.get("latest_report_id"),
            "exists": bool(report_payload.get("exists")),
        },
    }

    if report_saved and stop_reason in {"no_progress", "turn_limit", "tool_call_limit", "completed", "opencode_exit_nonzero"}:
        stop_reason = "report_saved"
    elif runtime_failure and stop_reason in {"completed", "no_progress", "stopped", "opencode_exit_nonzero", "session_missing"}:
        stop_reason = str(runtime_failure.get("stop_reason") or stop_reason)

    attempt_record = {
        "attempt": attempt,
        "session_title": live_session_title,
        "opencode_session_id": opencode_session_id,
        "gleskit_session_id": gleskit_session_id,
        "started_at": started_at,
        "ended_at": time.monotonic(),
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "returncode": returncode,
        "stop_reason": stop_reason,
        "watchdog_triggered": watchdog_triggered,
        "session_stats": stats,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "stdout_excerpt": stdout_text[:4000],
        "stderr_excerpt": stderr_text[:4000],
        "runtime_failure": runtime_failure,
        "opencode_command": opencode_command,
        "input_payload": input_payload,
        "session_export_path": str(attempt_root / "session_export.json") if session_export is not None else None,
        "transcript": transcript,
        "tool_trace": tool_trace,
        "report_saved": report_saved,
        "report_json": report_json,
        "report_markdown": report_markdown,
        "report_revision_count": _report_revision_count(tool_trace),
        "time_to_first_report": _time_to_first_report(session_export or {}, tool_trace),
        "session_export": session_export,
        "retryable": (
            bool(runtime_failure.get("retryable")) if runtime_failure else (
                not report_saved
                and (returncode not in (0, None) or make_retryable_error(stdout_text) or make_retryable_error(stderr_text))
                and (opencode_session_id is None or make_retryable_error(stdout_text) or make_retryable_error(stderr_text))
            )
        ),
    }
    if session_export is not None:
        write_json_atomic(attempt_root / "session_export.json", session_export)
    write_json_atomic(attempt_root / "attempt.json", attempt_record)
    return attempt_record


def should_retry_attempt(
    attempt_record: dict[str, Any],
    *,
    attempt_index: int,
    max_retries: int,
) -> bool:
    if bool(attempt_record.get("report_saved")):
        return False
    if attempt_index >= max_retries:
        return False
    if attempt_record.get("watchdog_triggered"):
        return False
    return bool(attempt_record.get("retryable"))


def build_episode_record(
    *,
    row: dict[str, Any],
    run_id: str,
    staged_inputs: dict[str, Any],
    attempt_records: list[dict[str, Any]],
    config: RepoBenchRunConfig,
) -> dict[str, Any]:
    final_attempt = attempt_records[-1]
    transcript = final_attempt.get("transcript") or []
    tool_trace = final_attempt.get("tool_trace") or []
    converted_tool_calls = _tool_trace_for_metrics(tool_trace)
    report_json = final_attempt.get("report_json") or {}
    report_saved = bool(final_attempt.get("report_saved"))
    final_stop_reason = str(final_attempt.get("stop_reason") or "")
    if report_saved:
        status = "completed"
        displayed_stop_reason = "report_saved"
    elif final_stop_reason in {"wall_timeout", "turn_limit", "tool_call_limit", "no_progress"}:
        status = "stopped"
        displayed_stop_reason = final_stop_reason
    else:
        status = "failed"
        displayed_stop_reason = final_stop_reason or "failed"

    total_duration = round(sum(float(item.get("duration_seconds") or 0.0) for item in attempt_records), 3)
    report_markdown = str(report_json.get("body_markdown") or "").strip()

    record = {
        "episode_id": row["task_id"],
        "task_id": row["task_id"],
        "short_id": row.get("short_id"),
        "question_id": row.get("question_id"),
        "case_id": row["case_id"],
        "scope": row.get("scope"),
        "report_type": row.get("report_type"),
        "template_id_requested": row.get("template_id"),
        "template_id": report_json.get("template_id") or row.get("template_id"),
        "title": row.get("title"),
        "task_prompt": row.get("task_prompt"),
        "kickoff_context": row.get("kickoff_context") or {},
        "expected_content": row.get("expected_content"),
        "expected_nuggets": row.get("expected_nuggets") or [],
        "forbidden_overclaims": row.get("forbidden_overclaims") or [],
        "input_artifacts": row.get("input_artifacts") or [],
        "staged_inputs": staged_inputs,
        "species": row.get("species"),
        "domain": row.get("domain"),
        "difficulty": row.get("difficulty"),
        "notes": row.get("notes"),
        "run_id": run_id,
        "skills_mode": normalize_skills_mode(config.skills_mode),
        "session_id": final_attempt.get("gleskit_session_id"),
        "opencode_session_id": final_attempt.get("opencode_session_id"),
        "session_title": final_attempt.get("session_title"),
        "status": status,
        "stop_reason": displayed_stop_reason,
        "report_saved": report_saved,
        "final_report_id": report_json.get("report_id"),
        "final_report": report_markdown,
        "report_freeze": {
            "schema_version": 1,
            "status": "frozen" if report_saved else "not_created",
            "algorithm": "sha256",
            "content_sha256": sha256_text(report_markdown) if report_saved else None,
            "report_path": "report.md",
            "captured_at": current_utc_iso(),
        },
        "final_report_payload": report_json,
        "messages": transcript,
        "tool_calls": converted_tool_calls,
        "tool_trace": tool_trace,
        "turn_count": int((final_attempt.get("session_stats") or {}).get("assistant_message_count") or 0),
        "tool_call_count": int((final_attempt.get("session_stats") or {}).get("tool_part_count") or 0),
        "report_revision_count": int(final_attempt.get("report_revision_count") or 0),
        "time_to_first_report": final_attempt.get("time_to_first_report"),
        "duration_seconds": float(final_attempt.get("duration_seconds") or 0.0),
        "total_duration_seconds": total_duration,
        "attempt_count": len(attempt_records),
        "retry_count": max(0, len(attempt_records) - 1),
        "runtime_failure": final_attempt.get("runtime_failure"),
        "attempts": [
            {
                "attempt": item.get("attempt"),
                "session_title": item.get("session_title"),
                "opencode_session_id": item.get("opencode_session_id"),
                "gleskit_session_id": item.get("gleskit_session_id"),
                "returncode": item.get("returncode"),
                "stop_reason": item.get("stop_reason"),
                "duration_seconds": item.get("duration_seconds"),
                "report_saved": item.get("report_saved"),
                "stdout_log": item.get("stdout_log"),
                "stderr_log": item.get("stderr_log"),
                "session_export_path": item.get("session_export_path"),
            }
            for item in attempt_records
        ],
        "input": final_attempt.get("input_payload") or {},
        "transcript": transcript,
        "report": report_json,
    }
    record["tool_provenance"] = build_tool_provenance(record)
    return record


def _summary_row(record: dict[str, Any]) -> dict[str, Any]:
    metrics = record.get("deterministic_metrics") if isinstance(record.get("deterministic_metrics"), dict) else {}
    judge = record.get("judge") if isinstance(record.get("judge"), dict) else {}
    judge_summary = judge.get("judge") if isinstance(judge.get("judge"), dict) else {}
    committee_category = _committee_category(record) or {}
    return {
        "task_id": record.get("task_id"),
        "report_type": record.get("report_type"),
        "scope": record.get("scope"),
        "skills_mode": record.get("skills_mode"),
        "status": record.get("status"),
        "stop_reason": record.get("stop_reason"),
        "report_saved": bool(record.get("report_saved")),
        "template_requested": record.get("template_id_requested"),
        "template_used": record.get("template_id"),
        "turn_count": record.get("turn_count"),
        "tool_call_count": record.get("tool_call_count"),
        "duration_seconds": record.get("duration_seconds"),
        "total_duration_seconds": record.get("total_duration_seconds"),
        "report_revision_count": metrics.get("report_revision_count"),
        "time_to_first_report": metrics.get("time_to_first_report"),
        "final_report_word_count": metrics.get("final_report_word_count"),
        "linked_artifact_count": metrics.get("linked_artifact_count"),
        "visible_sources_count": metrics.get("visible_sources_count"),
        "visible_provider_sources_count": metrics.get("visible_provider_sources_count"),
        "reader_facing_provenance_score": metrics.get("reader_facing_provenance_score"),
        "mean_nugget_text_overlap": metrics.get("mean_nugget_text_overlap"),
        "template_requested_vs_used": metrics.get("template_requested_vs_used"),
        "committee_category": committee_category.get("key"),
        "committee_category_label": committee_category.get("label"),
        "committee_level_mean_final": _committee_level_mean(record),
        "committee_level_mean_normalized": _committee_level_mean_normalized(record),
        "review_required": _review_required(record),
        "judge_short_review": judge_summary.get("short_review"),
    }


def _completed_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if bool(record.get("report_saved"))]


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return mean(values), pstdev(values) if len(values) > 1 else 0.0


def build_overall_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = _completed_records(records)
    committee_levels = [
        float(_committee_level_mean(record))
        for record in completed
        if _committee_level_mean(record) is not None
    ]
    provenance_scores = [
        float((record.get("deterministic_metrics") or {}).get("reader_facing_provenance_score"))
        for record in completed
        if isinstance(record.get("deterministic_metrics"), dict)
        and (record.get("deterministic_metrics") or {}).get("reader_facing_provenance_score") is not None
    ]
    nugget_scores = [
        float((record.get("deterministic_metrics") or {}).get("mean_nugget_text_overlap"))
        for record in completed
        if isinstance(record.get("deterministic_metrics"), dict)
        and (record.get("deterministic_metrics") or {}).get("mean_nugget_text_overlap") is not None
    ]
    committee_mean, committee_std = _mean_std(committee_levels)
    provenance_mean, provenance_std = _mean_std(provenance_scores)
    nugget_mean, nugget_std = _mean_std(nugget_scores)
    return {
        "task_count": len(records),
        "completed_count": len(completed),
        "incomplete_count": len(records) - len(completed),
        "stopped_count": sum(1 for record in records if record.get("status") == "stopped"),
        "failed_count": sum(1 for record in records if record.get("status") == "failed"),
        "report_saved_rate": round(len(completed) / len(records), 3) if records else 0.0,
        "mean_turns_completed": round(mean(float(record.get("turn_count") or 0.0) for record in completed), 3) if completed else 0.0,
        "mean_tool_calls_completed": round(mean(float(record.get("tool_call_count") or 0.0) for record in completed), 3) if completed else 0.0,
        "mean_duration_seconds_completed": round(mean(float(record.get("duration_seconds") or 0.0) for record in completed), 3) if completed else 0.0,
        "committee_level_mean_mean": committee_mean,
        "committee_level_mean_std": committee_std,
        "committee_level_mean_count": len(committee_levels),
        "committee_score_mean": (committee_mean / 4.0) if committee_mean is not None else None,
        "committee_score_std": (committee_std / 4.0) if committee_std is not None else None,
        "committee_score_count": len(committee_levels),
        "reader_facing_provenance_score_mean": provenance_mean,
        "reader_facing_provenance_score_std": provenance_std,
        "reader_facing_provenance_score_count": len(provenance_scores),
        "mean_nugget_text_overlap_mean": nugget_mean,
        "mean_nugget_text_overlap_std": nugget_std,
        "mean_nugget_text_overlap_count": len(nugget_scores),
    }


def _group_summary(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        token = str(record.get(key) or "").strip()
        groups.setdefault(token, []).append(record)
    rows: list[dict[str, Any]] = []
    for token, bucket in sorted(groups.items()):
        completed = _completed_records(bucket)
        committee_levels = [
            float(_committee_level_mean(record))
            for record in completed
            if _committee_level_mean(record) is not None
        ]
        judge_mean, judge_std = _mean_std(committee_levels)
        nugget_scores = [
            float((record.get("deterministic_metrics") or {}).get("mean_nugget_text_overlap"))
            for record in completed
            if isinstance(record.get("deterministic_metrics"), dict)
            and (record.get("deterministic_metrics") or {}).get("mean_nugget_text_overlap") is not None
        ]
        nugget_mean, nugget_std = _mean_std(nugget_scores)
        rows.append(
            {
                key: token,
                "task_count": len(bucket),
                "completed_count": len(completed),
                "incomplete_count": len(bucket) - len(completed),
                "report_saved_rate": round(len(completed) / len(bucket), 3) if bucket else 0.0,
                "mean_turns_completed": round(mean(float(record.get("turn_count") or 0.0) for record in completed), 3) if completed else 0.0,
                "mean_tool_calls_completed": round(mean(float(record.get("tool_call_count") or 0.0) for record in completed), 3) if completed else 0.0,
                "mean_duration_seconds_completed": round(mean(float(record.get("duration_seconds") or 0.0) for record in completed), 3) if completed else 0.0,
                "committee_level_mean_mean": judge_mean,
                "committee_level_mean_std": judge_std,
                "committee_level_mean_count": len(committee_levels),
                "committee_score_mean": (judge_mean / 4.0) if judge_mean is not None else None,
                "committee_score_std": (judge_std / 4.0) if judge_std is not None else None,
                "committee_score_count": len(committee_levels),
                "mean_nugget_text_overlap_mean": nugget_mean,
                "mean_nugget_text_overlap_std": nugget_std,
                "mean_nugget_text_overlap_count": len(nugget_scores),
            }
        )
    return rows


def build_failure_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for record in records:
        reason = str(record.get("stop_reason") or "unknown").strip() or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    total = len(records) or 1
    return [
        {"stop_reason": key, "count": value, "share": round(value / total, 3)}
        for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def render_metric_card(label: str, value: Any) -> str:
    value_text = "n/a" if value is None else str(value)
    return f'<div class="kpi"><span class="label">{html.escape(label)}</span><span class="value">{html.escape(value_text)}</span></div>'


def render_html_table(headers: list[str], rows: list[list[str]], *, empty_message: str = "No data.") -> str:
    if not rows:
        return f"<p>{html.escape(empty_message)}</p>"
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body_html = []
    for row in rows:
        body_html.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return (
        "<table><thead><tr>"
        + header_html
        + "</tr></thead><tbody>"
        + "".join(body_html)
        + "</tbody></table>"
    )


def _compact_preview(value: Any, *, width: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "-"
    return textwrap.shorten(text, width=width, placeholder="…")


def _text_list(value: Any) -> list[str]:
    """Normalize scalar-or-list metadata before rendering it in the portal."""

    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [text for item in values if (text := str(item).strip())]


def _fallback_judge_score_band(score: Any) -> dict[str, Any] | None:
    return judge_score_band(None if score is None else float(score))


def _judge_band_kind(band_key: Any) -> str:
    return judge_band_kind(band_key)


def _committee_category(record: dict[str, Any]) -> dict[str, Any] | None:
    judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
    if judge_category(record.get("committee_category")) is not None:
        return judge_category(record.get("committee_category"))
    if isinstance(judge_payload, dict) and judge_category(judge_payload.get("committee_category")) is not None:
        return judge_category(judge_payload.get("committee_category"))
    score_band = judge_payload.get("judge_score_band") if isinstance(judge_payload.get("judge_score_band"), dict) else _fallback_judge_score_band(judge_payload.get("judge_score"))
    if isinstance(score_band, dict):
        return judge_category(score_band.get("key"))
    return None


def _committee_level_mean(record: dict[str, Any]) -> float | None:
    if record.get("committee_level_mean_final") is not None:
        return float(record.get("committee_level_mean_final"))
    judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
    if isinstance(judge_payload, dict) and judge_payload.get("committee_level_mean_final") is not None:
        return float(judge_payload.get("committee_level_mean_final"))
    legacy_score = judge_payload.get("judge_score") if isinstance(judge_payload, dict) else None
    if legacy_score is None:
        return None
    return float(legacy_score) * 4.0


def _committee_level_mean_normalized(record: dict[str, Any]) -> float | None:
    if record.get("committee_level_mean_normalized") is not None:
        return float(record.get("committee_level_mean_normalized"))
    if record.get("committee_level_mean_final") is not None:
        return float(record.get("committee_level_mean_final")) / 4.0
    judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
    if isinstance(judge_payload, dict):
        if judge_payload.get("committee_level_mean_normalized") is not None:
            return float(judge_payload.get("committee_level_mean_normalized"))
        if judge_payload.get("committee_level_mean_final") is not None:
            return float(judge_payload.get("committee_level_mean_final")) / 4.0
    legacy_score = judge_payload.get("judge_score") if isinstance(judge_payload, dict) else None
    if legacy_score is None:
        return None
    return float(legacy_score)


def _review_required(record: dict[str, Any]) -> bool:
    if record.get("review_required") is not None:
        return bool(record.get("review_required"))
    judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
    if isinstance(judge_payload, dict):
        if judge_payload.get("review_required") is not None:
            return bool(judge_payload.get("review_required"))
        judge = judge_payload.get("judge")
        if isinstance(judge, dict) and judge.get("review_required") is not None:
            return bool(judge.get("review_required"))
    return False


def _review_reasons(record: dict[str, Any]) -> list[str]:
    if isinstance(record.get("review_reasons"), list):
        return [str(item).strip() for item in record.get("review_reasons") or [] if str(item).strip()]
    judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
    if isinstance(judge_payload.get("review_reasons"), list):
        return [str(item).strip() for item in judge_payload.get("review_reasons") or [] if str(item).strip()]
    judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
    if isinstance(judge.get("review_reasons"), list):
        return [str(item).strip() for item in judge.get("review_reasons") or [] if str(item).strip()]
    return []


def render_judge_summary_html(record: dict[str, Any]) -> str:
    judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
    if not judge_payload:
        return ""
    judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
    specialists = judge_payload.get("specialists") if isinstance(judge_payload.get("specialists"), dict) else {}
    category_item = _committee_category(record) or {}
    committee_level = _committee_level_mean(record)
    sections = []
    for key, title in (
        ("biomedical_content_specialist", "Biomedical content"),
        ("evidence_support_specialist", "Evidence support"),
        ("template_compliance_specialist", "Template compliance"),
        ("provenance_quality_specialist", "Provenance quality"),
    ):
        specialist = specialists.get(key) if isinstance(specialists.get(key), dict) else {}
        specialist_category = judge_category(specialist.get("category")) or {}
        extra_blocks = ""
        if key == "evidence_support_specialist":
            extra_blocks = (
                f'{render_html_list_block("Supported claims", specialist.get("supported_claims"), wide=True)}'
                f'{render_html_list_block("Unsupported claims", specialist.get("unsupported_claims"), wide=True)}'
                f'{render_html_list_block("Evidence gaps", specialist.get("evidence_gaps"), wide=True)}'
            )
        sections.append(
            '<div class="judge-section">'
            f"<h4>{html.escape(title)}</h4>"
            f'<div class="kpi-row">{render_metric_card("Category", specialist_category.get("label") or specialist.get("category"))}</div>'
            f'{render_code_block("Summary", specialist.get("summary"), wide=True)}'
            f'{render_html_list_block("Key observations", specialist.get("key_observations"), wide=True)}'
            f'{render_html_list_block("Satisfied criteria", specialist.get("satisfied_criteria"), wide=True)}'
            f'{render_html_list_block("Failed criteria", specialist.get("failed_criteria"), wide=True)}'
            f'{render_html_list_block("Failure codes", specialist.get("failure_codes"), wide=True)}'
            f"{extra_blocks}"
            "</div>"
        )
    return (
        '<div class="judge-panel">'
        "<h3>Judge Committee</h3>"
        f'<div class="kpi-row">{render_metric_card("Committee category", category_item.get("label") or category_item.get("key") or "n/a")}{render_metric_card("Committee level mean (0-4)", committee_level)}{render_metric_card("Review required", "yes" if _review_required(record) else "no")}{render_metric_card("Specialist disagreement span", judge_payload.get("specialist_disagreement_span"))}</div>'
        f'{render_code_block("Quality flags", ", ".join(_text_list(judge_payload.get("quality_flags"))) or "-", wide=True)}'
        f'{render_code_block("Review reasons", ", ".join(_review_reasons(record)) or "-", wide=True)}'
        f'{render_code_block("Short review", judge.get("short_review"), wide=True)}'
        f'{render_code_block("Rationale", judge.get("rationale"), wide=True)}'
        f'{render_html_list_block("Major strengths", judge.get("major_strengths"), wide=True)}'
        f'{render_html_list_block("Major issues", judge.get("major_issues"), wide=True)}'
        f'{render_html_list_block("Disagreement flags", judge.get("disagreement_flags"), wide=True)}'
        f"<div class=\"judge-grid\">{''.join(sections)}</div>"
        "</div>"
    )


def render_linked_artifacts_html(record: dict[str, Any]) -> str:
    report_payload = record.get("report") if isinstance(record.get("report"), dict) else {}
    linked_artifacts = report_payload.get("linked_artifacts") if isinstance(report_payload.get("linked_artifacts"), list) else []
    if not linked_artifacts:
        return "<p>No linked evidence artifacts captured.</p>"
    episode_root = Path(str(record.get("episode_root") or ""))
    question_dir = episode_root.parents[1] / "portal" / "questions" if len(episode_root.parents) >= 2 else None
    items: list[str] = []
    for item in linked_artifacts:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        title = str(item.get("title") or artifact_id or "artifact").strip()
        provider = str(item.get("provider") or "").strip()
        summary = str(item.get("summary") or "").strip()
        artifact_path = str(item.get("path") or "").strip()
        href = ""
        if artifact_path and question_dir is not None:
            try:
                href = os.path.relpath(Path(artifact_path), question_dir)
            except Exception:
                href = ""
        link_html = f'<a href="{html.escape(href)}">{html.escape(title)}</a>' if href else html.escape(title)
        meta_bits = []
        if provider:
            meta_bits.append(html.escape(provider))
        if artifact_id:
            meta_bits.append(f'<span class="mono">{html.escape(artifact_id)}</span>')
        if summary:
            meta_bits.append(html.escape(summary))
        paper_links = []
        for paper in item.get("paper_links") or []:
            if not isinstance(paper, dict):
                continue
            paper_title = str(paper.get("title") or "").strip()
            paper_url = str(paper.get("url") or "").strip()
            if paper_title and paper_url:
                paper_links.append(f'<a href="{html.escape(paper_url)}">{html.escape(paper_title)}</a>')
        if paper_links:
            meta_bits.append("Representative papers: " + "; ".join(paper_links))
        items.append(f"<li><strong>{link_html}</strong><br>{' | '.join(meta_bits)}</li>")
    return f"<ul>{''.join(items)}</ul>"


def render_question_page(record: dict[str, Any], *, episode_root_href_prefix: str = "../../episodes") -> str:
    report_payload = record.get("report") if isinstance(record.get("report"), dict) else {}
    metrics = record.get("deterministic_metrics") if isinstance(record.get("deterministic_metrics"), dict) else {}
    transcript = record.get("transcript") or []
    tool_trace = record.get("tool_trace") or []
    transcript_html = []
    for item in transcript:
        transcript_html.append(
            "<details>"
            f"<summary>{html.escape(str(item.get('role') or ''))}</summary>"
            f"<pre>{html.escape(str(item.get('text') or ''))}</pre>"
            "</details>"
        )
    tool_html = []
    for item in tool_trace:
        tool_html.append(
            "<details>"
            f"<summary>{html.escape(str(item.get('tool') or 'tool'))} ({html.escape(str(item.get('status') or 'unknown'))})</summary>"
            f"<h3>Input</h3><pre>{html.escape(json.dumps(item.get('input') or {}, indent=2, sort_keys=True, default=str))}</pre>"
            f"<h3>Output</h3><pre>{html.escape(json.dumps(item.get('output'), indent=2, sort_keys=True, default=str))}</pre>"
            "</details>"
        )
    artifact_links = [
        ("episode.json", f"{episode_root_href_prefix}/{record['task_id']}/episode.json"),
        ("input.json", f"{episode_root_href_prefix}/{record['task_id']}/input.json"),
        ("transcript.json", f"{episode_root_href_prefix}/{record['task_id']}/transcript.json"),
        ("tool_calls.json", f"{episode_root_href_prefix}/{record['task_id']}/tool_calls.json"),
        ("report.json", f"{episode_root_href_prefix}/{record['task_id']}/report.json"),
        ("report.md", f"{episode_root_href_prefix}/{record['task_id']}/report.md"),
        ("deterministic_metrics.json", f"{episode_root_href_prefix}/{record['task_id']}/deterministic_metrics.json"),
    ]
    if record.get("judge") or ((Path(record.get("episode_root", "")) / "judge.json").exists() if record.get("episode_root") else False):
        artifact_links.append(("judge.json", f"{episode_root_href_prefix}/{record['task_id']}/judge.json"))
    artifact_html = " ".join(f'<a href="{html.escape(link)}">{html.escape(label)}</a>' for label, link in artifact_links)
    body = f"""
    <header class="hero">
      <div class="meta">RepoBench reviewer page</div>
      <h1>{html.escape(str(record.get("task_id") or ""))}</h1>
      <div class="meta">Report type: <span class="mono">{html.escape(str(record.get("report_type") or ""))}</span></div>
      <div class="meta">Artifacts: {artifact_html}</div>
    </header>

    <section>
      <h2>Task</h2>
      <p><strong>Title:</strong> {html.escape(str(record.get("title") or ""))}</p>
      <p><strong>Expected content:</strong> {html.escape(str(record.get("expected_content") or ""))}</p>
      <pre>{html.escape(str(record.get("task_prompt") or ""))}</pre>
      <h3>Kickoff context</h3>
      <pre>{html.escape(json.dumps(record.get("kickoff_context") or {}, indent=2, sort_keys=True, default=str))}</pre>
      <h3>Expected nuggets</h3>
      <pre>{html.escape(json.dumps(record.get("expected_nuggets") or [], indent=2, sort_keys=True, default=str))}</pre>
      <h3>Forbidden overclaims</h3>
      <pre>{html.escape(json.dumps(record.get("forbidden_overclaims") or [], indent=2, sort_keys=True, default=str))}</pre>
    </section>

    <section>
      <h2>Outcome</h2>
      <p><strong>Status:</strong> {html.escape(str(record.get("status") or ""))}</p>
      <p><strong>Stop reason:</strong> {html.escape(str(record.get("stop_reason") or ""))}</p>
      <p><strong>Saved report:</strong> {html.escape(str(bool(record.get("report_saved"))))}</p>
      <p><strong>Template requested:</strong> {html.escape(str(record.get("template_id_requested") or ""))}</p>
      <p><strong>Template used:</strong> {html.escape(str(record.get("template_id") or ""))}</p>
      <p><strong>Turns:</strong> {html.escape(str(record.get("turn_count") or 0))}</p>
      <p><strong>Tool calls:</strong> {html.escape(str(record.get("tool_call_count") or 0))}</p>
      <p><strong>Duration:</strong> {html.escape(format_duration_human(record.get("duration_seconds")))}</p>
      <h3>Final saved report</h3>
      <pre class="code-block">{html.escape(str(report_payload.get("body_markdown") or ""))}</pre>
    </section>

    <section>
      <h2>Linked evidence artifacts</h2>
      {render_linked_artifacts_html(record)}
    </section>

    <section>
      <h2>Deterministic metrics</h2>
      <pre class="code-block">{html.escape(json.dumps(metrics, indent=2, sort_keys=True, default=str))}</pre>
    </section>

    {f'<section><h2>Judge</h2>{render_judge_summary_html(record)}</section>' if record.get("judge") else ""}

    <section>
      <h2>Visible transcript</h2>
      {''.join(transcript_html) or '<p>No visible transcript captured.</p>'}
    </section>

    <section>
      <h2>Tool provenance</h2>
      {''.join(tool_html) or '<p>No tool calls captured.</p>'}
    </section>
    """
    return render_html_page(f"RepoBench {record.get('task_id')}", body)


def render_index_page(*, run_manifest: dict[str, Any], records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    by_report_type = summary.get("by_report_type") or []
    failure_summary = summary.get("failure_summary") or []
    task_cards = []
    for record in records:
        judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
        judge_summary = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
        committee_category = _committee_category(record) or {}
        committee_label = str(committee_category.get("label") or committee_category.get("key") or "").strip()
        committee_key = str(committee_category.get("key") or "").strip()
        committee_level = _committee_level_mean(record)
        task_cards.append(
            f"""
            <details class="question-accordion">
              <summary>
                <div class="question-summary">
                  <div class="question-summary-main">
                    <div class="question-summary-top">
                      <a class="mono question-link" href="questions/{html.escape(str(record.get("task_id") or ""))}.html">{html.escape(str(record.get("task_id") or ""))}</a>
                      <div class="question-meta">
                        <span class="pill neutral">{html.escape(str(record.get("report_type") or ""))}</span>
                        <span class="pill {'good' if record.get('report_saved') else 'warn'}">{'saved' if record.get('report_saved') else 'not saved'}</span>
                        <span class="pill neutral">{html.escape(format_duration_human(record.get("duration_seconds")))}</span>
                        {f'<span class="pill neutral">level {html.escape(str(committee_level))}/4</span>' if committee_level is not None else ''}
                        {f'<span class="pill {judge_band_kind(committee_key)}">{html.escape(committee_label)}</span>' if committee_label else ''}
                        {f'<span class="pill warn">review</span>' if _review_required(record) else ''}
                      </div>
                    </div>
                    <div class="question-preview">{html.escape(_compact_preview(record.get("task_prompt"), width=220))}</div>
                    <div class="question-preview question-response-preview"><span class="block-label">Saved report preview</span>{html.escape(_compact_preview((record.get("report") or {}).get("body_markdown"), width=220))}</div>
                  </div>
                  <div class="kpi-row">
                    {render_metric_card("Turns", record.get("turn_count"))}
                    {render_metric_card("Tools", record.get("tool_call_count"))}
                    {render_metric_card("Words", (record.get("deterministic_metrics") or {}).get("final_report_word_count"))}
                  </div>
                </div>
              </summary>
              <div class="question-accordion-body">
                {render_code_block("Task prompt", record.get("task_prompt"), wide=True)}
                {render_code_block("Saved report", (record.get("report") or {}).get("body_markdown"), wide=True)}
                {render_code_block("Judge short review", judge_summary.get("short_review"), wide=True)}
                {render_judge_summary_html(record)}
              </div>
            </details>
            """
        )
    run_info_rows = [
        ["Run ID", html.escape(str(run_manifest.get("run_id") or ""))],
        ["Created at", html.escape(str(run_manifest.get("created_at") or ""))],
        ["Dataset", html.escape(str((run_manifest.get("dataset") or {}).get("path") or ""))],
        ["Model", html.escape(str((run_manifest.get("opencode") or {}).get("model") or ""))],
        ["Agent", html.escape(str((run_manifest.get("opencode") or {}).get("agent") or ""))],
        ["Skills mode", html.escape(str((run_manifest.get("selection") or {}).get("skills_mode") or (run_manifest.get("opencode") or {}).get("skills_mode") or ""))],
        ["Scope", html.escape(str((run_manifest.get("selection") or {}).get("scope") or ""))],
        ["Report type filter", html.escape(str((run_manifest.get("selection") or {}).get("report_type") or "all"))],
    ]
    by_report_rows = [
        [
            html.escape(str(row.get("report_type") or "")),
            html.escape(str(row.get("task_count") or "")),
            html.escape(str(row.get("completed_count") or "")),
            html.escape(str(row.get("incomplete_count") or "")),
            html.escape(str(row.get("report_saved_rate") or "")),
            html.escape(str(row.get("committee_level_mean_mean") or "")),
            html.escape(str(row.get("mean_nugget_text_overlap_mean") or "")),
        ]
        for row in by_report_type
    ]
    failure_rows = [
        [html.escape(str(row.get("stop_reason") or "")), html.escape(str(row.get("count") or "")), html.escape(str(row.get("share") or ""))]
        for row in failure_summary
    ]
    body = f"""
    <header class="hero">
      <div class="meta">RepoBench evaluation run</div>
      <h1>{html.escape(str(run_manifest.get("run_id") or ""))}</h1>
      <div class="meta">Completed-only aggregates are computed only over episodes with a real saved report artifact.</div>
    </header>

    <section class="grid cards">
      {render_metric_card("Tasks", summary['overall']['task_count'])}
      {render_metric_card("Completed", summary['overall']['completed_count'])}
      {render_metric_card("Incomplete", summary['overall']['incomplete_count'])}
      {render_metric_card("Saved report rate", summary['overall']['report_saved_rate'])}
      {render_metric_card("Committee level mean (0-4)", summary['overall']['committee_level_mean_mean'])}
      {render_metric_card("Provenance", summary['overall']['reader_facing_provenance_score_mean'])}
      {render_metric_card("Nugget overlap", summary['overall']['mean_nugget_text_overlap_mean'])}
      {render_metric_card("Mean turns", summary['overall']['mean_turns_completed'])}
      {render_metric_card("Mean tools", summary['overall']['mean_tool_calls_completed'])}
      {render_metric_card("Mean time", format_duration_human(summary['overall']['mean_duration_seconds_completed']))}
    </section>

    <section>
      <h2>Run info</h2>
      {render_html_table(["Field", "Value"], run_info_rows)}
    </section>

    <section>
      <h2>By report type</h2>
      {render_html_table(["Report type", "Tasks", "Completed", "Incomplete", "Saved rate", "Committee level mean", "Nugget overlap mean"], by_report_rows)}
    </section>

    <section>
      <h2>Failure modes</h2>
      {render_html_table(["Stop reason", "Count", "Share"], failure_rows)}
    </section>

    <section>
      <h2>Tasks</h2>
      <div class="stack">
        {''.join(task_cards) or '<p>No tasks were run.</p>'}
      </div>
    </section>
    """
    return render_html_page(f"RepoBench {run_manifest.get('run_id')}", body)


def write_episode_files(episode_root: Path, record: dict[str, Any]) -> None:
    episode_root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(episode_root / "input.json", record.get("input") or {})
    write_json_atomic(episode_root / "transcript.json", record.get("transcript") or [])
    write_json_atomic(episode_root / "tool_calls.json", record.get("tool_trace") or [])
    write_json_atomic(episode_root / "report.json", record.get("report") or {})
    write_text_atomic(episode_root / "report.md", str((record.get("report") or {}).get("body_markdown") or ""))
    write_json_atomic(episode_root / "deterministic_metrics.json", record.get("deterministic_metrics") or {})
    write_json_atomic(episode_root / "episode.json", record)


def load_episode_record(episode_root: Path) -> dict[str, Any]:
    return read_json(episode_root / "episode.json")


def write_summary_artifacts(run_root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    summary_root = run_root / "summary"
    summary_root.mkdir(parents=True, exist_ok=True)
    rows = [_summary_row(record) for record in records]
    overall = build_overall_summary(records)
    by_report_type = _group_summary(records, "report_type")
    by_scope = _group_summary(records, "scope")
    failure_summary = build_failure_summary(records)
    judge_rows = [row for row in rows if row.get("committee_level_mean_final") is not None]

    write_json_atomic(summary_root / "overall.json", overall)
    write_json_atomic(summary_root / "tasks.json", rows)
    _write_csv(summary_root / "tasks.csv", rows)
    write_json_atomic(summary_root / "by_report_type.json", by_report_type)
    _write_csv(summary_root / "by_report_type.csv", by_report_type)
    write_json_atomic(summary_root / "by_scope.json", by_scope)
    _write_csv(summary_root / "by_scope.csv", by_scope)
    write_json_atomic(summary_root / "failure_summary.json", failure_summary)
    _write_csv(summary_root / "failure_summary.csv", failure_summary)
    if judge_rows:
        write_json_atomic(summary_root / "judge_metrics.json", judge_rows)
        _write_csv(summary_root / "judge_metrics.csv", judge_rows)
    return {
        "overall": overall,
        "task_rows": rows,
        "by_report_type": by_report_type,
        "by_scope": by_scope,
        "failure_summary": failure_summary,
        "judge_rows": judge_rows,
    }


def write_reports(run_root: Path, run_manifest: dict[str, Any], records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    summary_root = run_root / "summary"
    portal_root = run_root / "portal"
    portal_questions = portal_root / "questions"
    portal_questions.mkdir(parents=True, exist_ok=True)
    report_html = render_index_page(run_manifest=run_manifest, records=records, summary=summary)
    write_text_atomic(summary_root / "report.html", report_html)
    write_text_atomic(summary_root / "report.md", "RepoBench report is available in report.html.\n")
    write_text_atomic(portal_root / "index.html", report_html)
    for record in records:
        record = dict(record)
        record["episode_root"] = str(run_root / "episodes" / str(record.get("task_id") or ""))
        page = render_question_page(record)
        write_text_atomic(portal_questions / f"{record['task_id']}.html", page)


def _preflight_validate(
    *,
    selected_rows: list[dict[str, Any]],
    staged_inputs: dict[str, dict[str, Any]],
    tool_schema_snapshot: dict[str, Any],
) -> dict[str, Any]:
    specs = load_report_type_specs()
    tool_names = {
        str((tool.get("function") or {}).get("name") or "").strip()
        for tool in tool_schema_snapshot.get("tools") or []
        if isinstance(tool, dict)
    }
    checks: list[dict[str, Any]] = []
    for row in selected_rows:
        task_id = str(row.get("task_id") or "").strip()
        report_type = str(row.get("report_type") or "").strip()
        checks.append(
            {
                "name": f"report_type_exists__{task_id}",
                "passed": report_type in specs,
                "details": {"report_type": report_type},
            }
        )
        if report_type not in specs:
            continue
        spec = specs[report_type]
        try:
            get_report_template(spec.template_id)
            template_exists = True
        except Exception:
            template_exists = False
        checks.append(
            {
                "name": f"template_exists__{task_id}",
                "passed": template_exists,
                "details": {"template_id": spec.template_id},
            }
        )
        staged = staged_inputs[task_id]
        for artifact in staged.get("artifacts") or []:
            checks.append(
                {
                    "name": f"artifact_exists__{task_id}__{artifact.get('artifact_id')}",
                    "passed": Path(str(artifact.get("staged_path") or "")).exists(),
                    "details": artifact,
                }
            )
        for step in spec.workflow_steps:
            missing_tools = [tool for tool in step.required_tools if tool not in tool_names]
            checks.append(
                {
                    "name": f"required_tools_present__{task_id}__{step.step_id}",
                    "passed": not missing_tools,
                    "details": {
                        "required_tools": list(step.required_tools),
                        "missing_tools": missing_tools,
                    },
                }
            )
            missing_skill_files = [
                rel_path
                for rel_path in step.required_skill_files
                if not (OPENCODE_SKILL_ROOT / rel_path).exists()
            ]
            checks.append(
                {
                    "name": f"required_skill_files_present__{task_id}__{step.step_id}",
                    "passed": not missing_skill_files,
                    "details": {
                        "required_skill_files": list(step.required_skill_files),
                        "missing_skill_files": missing_skill_files,
                    },
                }
            )
    return {
        "created_at": current_utc_iso(),
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def run_repobench_evaluation(project_root: Path, config: RepoBenchRunConfig) -> Path:
    dataset_path, dataset_rows = load_repobench_rows(project_root)
    continue_mode = normalize_continue_mode(config.continue_mode, resume_flag=config.resume)
    run_root = project_root / DEFAULT_REPOBENCH_RUNS_ROOT / config.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    existing_manifest = load_existing_manifest(run_root)
    if continue_mode == "fresh" and existing_manifest is not None:
        raise FileExistsError(
            f"Run root already exists: {run_root}. Use --continue-mode resume|append|retry_failed or choose a new run id."
        )
    selected_rows, existing_records, cumulative_task_order = select_rows_for_continue_mode(
        dataset_rows=dataset_rows,
        config=config,
        continue_mode=continue_mode,
        run_root=run_root,
        existing_manifest=existing_manifest,
    )
    workspace_root = run_root / "workspace"
    runtime_root = run_root / "runtime"
    workspace_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)

    with _temporary_eval_paths(workspace_root=workspace_root, runtime_root=runtime_root):
        settings = load_settings(project_root)
        _assert_isolated_eval_roots(
            settings=settings,
            workspace_root=workspace_root,
            runtime_root=runtime_root,
        )
        prompt_template = load_prompt_template(config.skills_mode)

        staged_inputs: dict[str, dict[str, Any]] = {}
        staged_input_rows: list[dict[str, Any]] = []
        for row in selected_rows:
            staged = stage_repobench_input_artifacts(project_root=project_root, run_root=run_root, row=row)
            staged_inputs[str(row["task_id"])] = staged
            staged_input_rows.append(staged)

        tool_schema_snapshot = asyncio.run(snapshot_tool_schemas(project_root, run_root))
        run_manifest = build_manifest_payload(
            project_root=project_root,
            run_root=run_root,
            settings=settings,
            config=config,
            dataset_path=dataset_path,
            dataset_rows=dataset_rows,
            selected_rows=selected_rows,
            staged_inputs=staged_input_rows,
            tool_schema_snapshot=tool_schema_snapshot,
            prompt_template=prompt_template,
        )
        run_manifest["created_at"] = str((existing_manifest or {}).get("created_at") or run_manifest.get("created_at"))
        run_manifest["updated_at"] = current_utc_iso()
        run_manifest["planned_episode_count"] = len(cumulative_task_order)
        run_manifest["report_type_snapshots"] = merge_report_type_snapshots(
            existing_manifest,
            run_manifest.get("report_type_snapshots") or [],
        )
        run_manifest["staged_inputs"] = merge_staged_inputs(existing_manifest, staged_input_rows)
        run_manifest["selection"] = {
            **build_selection_payload(config),
            "selected_task_count": len(selected_rows),
        }
        existing_history = (existing_manifest or {}).get("selection_history") or []
        history_rows = [item for item in existing_history if isinstance(item, dict)]
        history_rows.append(
            build_selection_history_event(
                config,
                continue_mode=continue_mode,
                selected_rows=selected_rows,
            )
        )
        run_manifest["selection_history"] = history_rows
        run_manifest["run_inventory"] = {
            "planned_task_count": len(cumulative_task_order),
            "planned_task_ids": list(cumulative_task_order),
            "completed_task_count": sum(1 for payload in existing_records.values() if task_is_completed(payload)),
        }
        write_json_atomic(run_root / "run_manifest.json", run_manifest)

        preflight = _preflight_validate(
            selected_rows=selected_rows,
            staged_inputs=staged_inputs,
            tool_schema_snapshot=tool_schema_snapshot,
        )
        write_json_atomic(run_root / "preflight.json", preflight)
        if not preflight.get("passed"):
            raise RuntimeError("RepoBench preflight failed. Inspect run_root/preflight.json.")

        specs = load_report_type_specs()
        existing_planned_entries = {
            str(item.get("task_id") or item.get("episode_id") or "").strip(): item
            for item in load_existing_planned_matrix(run_root)
            if str(item.get("task_id") or item.get("episode_id") or "").strip()
        }
        current_planned_entries: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(selected_rows, start=1):
            spec = specs[str(row["report_type"])]
            current_planned_entries[str(row["task_id"])] = {
                "run_id": config.run_id,
                "selected_index": index,
                "episode_id": row["task_id"],
                "task_id": row["task_id"],
                "report_type": row["report_type"],
                "template_id": row.get("template_id") or spec.template_id,
                "scope": row.get("scope"),
                "title": row.get("title"),
                "task_prompt": row.get("task_prompt"),
                "expected_content": row.get("expected_content"),
                "staged_inputs": staged_inputs[str(row["task_id"])],
                "selection": build_selection_payload(config),
            }
        planned_matrix = [
            current_planned_entries.get(task_id)
            or existing_planned_entries.get(task_id)
            for task_id in cumulative_task_order
            if current_planned_entries.get(task_id) or existing_planned_entries.get(task_id)
        ]
        write_json_atomic(run_root / "planned_matrix.json", planned_matrix)

        if config.dry_run:
            write_json_atomic(
                run_root / "dry_run.json",
                {
                    "run_id": config.run_id,
                    "selected_task_count": len(selected_rows),
                    "planned_task_count": len(cumulative_task_order),
                    "run_root": str(run_root),
                    "continue_mode": continue_mode,
                },
            )
            return run_root

        heartbeat = RuntimeHeartbeat(project_root, workspace_root, runtime_root)
        heartbeat.start()
        try:
            records_by_id = dict(existing_records)
            for row in selected_rows:
                task_id = str(row["task_id"])
                episode_root = run_root / "episodes" / task_id
                episode_root.mkdir(parents=True, exist_ok=True)
                existing_episode = episode_root / "episode.json"
                if config.resume and existing_episode.exists():
                    payload = read_json(existing_episode)
                    if isinstance(payload, dict) and payload.get("report_saved"):
                        records_by_id[task_id] = payload
                        continue
                spec = specs[str(row["report_type"])]
                attempt_records: list[dict[str, Any]] = []
                prior_record = records_by_id.get(task_id)
                first_attempt_number = next_attempt_number(episode_root)
                for retry_index in range(config.max_retries + 1):
                    attempt = first_attempt_number + retry_index
                    gleskit_session = _create_gleskit_session(
                        workspace_root=workspace_root,
                        settings=settings,
                        row=row,
                        run_id=config.run_id,
                    )
                    heartbeat.set_active_session(str(gleskit_session.get("session_id") or ""))
                    prompt_text = build_repobench_prompt(
                        template_text=prompt_template,
                        row=row,
                        spec=spec,
                        staged_inputs=staged_inputs[task_id],
                        run_id=config.run_id,
                        task_root=episode_root,
                        session_id=str(gleskit_session.get("session_id") or ""),
                    )
                    attempt_record = run_opencode_attempt(
                        project_root=project_root,
                        run_root=run_root,
                        workspace_root=workspace_root,
                        runtime_root=runtime_root,
                        settings=settings,
                        row=row,
                        spec=spec,
                        staged_inputs=staged_inputs[task_id],
                        config=config,
                        attempt=attempt,
                        prompt_text=prompt_text,
                        gleskit_session=gleskit_session,
                    )
                    attempt_records.append(attempt_record)
                    if not should_retry_attempt(
                        attempt_record,
                        attempt_index=retry_index,
                        max_retries=config.max_retries,
                    ):
                        break

                record = build_episode_record(
                    row=row,
                    run_id=config.run_id,
                    staged_inputs=staged_inputs[task_id],
                    attempt_records=attempt_records,
                    config=config,
                )
                record = merge_episode_attempt_history(prior_record, record)
                evaluation_payload = build_evaluation_payload(run_root, record)
                metrics = deterministic_metrics(record, evaluation_payload)
                record["report"] = {
                    **(record.get("report") or {}),
                    "visible_source_summary": evaluation_payload.get("visible_sources_summary") or visible_sources_summary(
                        str((record.get("report") or {}).get("body_markdown") or "")
                    ),
                }
                record["deterministic_metrics"] = metrics
                write_episode_files(episode_root, record)
                records_by_id[task_id] = record
                if _env_flag("REPOBENCH_AUTO_JUDGE"):
                    _run_auto_repobench_judge(
                        run_root,
                        task_ids=_completed_task_ids(merge_records_by_order(cumulative_task_order, records_by_id)),
                    )

            run_manifest["run_inventory"]["completed_task_count"] = sum(
                1 for payload in records_by_id.values() if task_is_completed(payload)
            )
            write_json_atomic(run_root / "run_manifest.json", run_manifest)
            records = merge_records_by_order(cumulative_task_order, records_by_id)
            summary = write_summary_artifacts(run_root, records)
            write_reports(run_root, run_manifest, records, summary)
            write_json_atomic(
                run_root / "results.json",
                {
                    "run_manifest": run_manifest,
                    "summary": summary["overall"],
                    "task_count": len(records),
                    "tasks": summary["task_rows"],
                    "summary_report_path": "summary/report.html",
                },
            )
            if _env_flag("REPOBENCH_AUTO_JUDGE"):
                _run_auto_repobench_judge(run_root, task_ids=_completed_task_ids(records))
            return run_root
        finally:
            heartbeat.stop()


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


def _completed_task_ids(records: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(str(record.get("task_id") or "").strip() for record in records if bool(record.get("report_saved")))


def _run_auto_repobench_judge(run_root: Path, *, task_ids: tuple[str, ...] | None = None) -> None:
    from gleskit_mcp.eval.repobench_judge import RepoBenchJudgeConfig, judge_repobench_run

    raw_scope = _env_text("REPOBENCH_JUDGE_SCOPE", "all") or "all"
    raw_report_type = _env_text("REPOBENCH_JUDGE_REPORT_TYPE", "") or None
    raw_task_ids = _env_text("REPOBENCH_JUDGE_TASK_IDS", "")
    raw_limit = _env_text("REPOBENCH_JUDGE_LIMIT", "0") or "0"
    raw_timeout = _env_text("REPOBENCH_JUDGE_TIMEOUT_SECONDS", "180") or "180"
    raw_provider = _env_text("REPOBENCH_JUDGE_PROVIDER", "openai") or "openai"
    raw_base_url = _env_text("REPOBENCH_JUDGE_BASE_URL", "") or None
    raw_model = _env_text("REPOBENCH_JUDGE_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini"

    live_task_ids = tuple(task_ids or ())
    if live_task_ids:
        judge_scope = "all"
        judge_task_ids = live_task_ids
        judge_limit = 0
    else:
        judge_scope = raw_scope
        judge_task_ids = tuple(item.strip() for item in raw_task_ids.split(",") if item.strip())
        judge_limit = max(0, int(raw_limit))
    config = RepoBenchJudgeConfig(
        run_root=run_root,
        scope=judge_scope,
        task_ids=judge_task_ids,
        report_type=raw_report_type,
        limit=judge_limit,
        resume=True,
        timeout_seconds=max(1.0, float(raw_timeout)),
        provider=raw_provider,
        base_url=raw_base_url,
        model=raw_model,
        content_model=_env_text("REPOBENCH_JUDGE_CONTENT_MODEL", "") or None,
        evidence_model=_env_text("REPOBENCH_JUDGE_EVIDENCE_MODEL", "") or None,
        template_model=_env_text("REPOBENCH_JUDGE_TEMPLATE_MODEL", "") or None,
        provenance_model=_env_text("REPOBENCH_JUDGE_PROVENANCE_MODEL", "") or None,
        arbiter_model=_env_text("REPOBENCH_JUDGE_ARBITER_MODEL", "") or None,
        verbose=_env_flag("REPOBENCH_JUDGE_VERBOSE"),
    )
    judge_repobench_run(config)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = resolve_project_root()
    resolved_model, message = resolve_opencode_model(str(args.model or DEFAULT_REPOBENCH_MODEL), OPENCODE_CONFIG_PATH)
    if message:
        print(message)
    config = RepoBenchRunConfig(
        run_id=str(args.run_id or run_id_from_time(prefix="repobench")),
        scope=str(args.scope or "1"),
        task_ids=tuple(str(item).strip() for item in args.task_id if str(item).strip()),
        report_type=str(args.report_type or "").strip() or None,
        limit=max(0, int(args.limit or 0)),
        random_count=max(0, int(args.random_count or 0)),
        random_seed=None if args.random_seed is None else int(args.random_seed),
        resume=bool(args.resume),
        continue_mode=normalize_continue_mode(args.continue_mode or "fresh", resume_flag=bool(args.resume)),
        dry_run=bool(args.dry_run),
        agent=str(args.agent or "repobench-eval").strip() or "repobench-eval",
        skills_mode=normalize_skills_mode(args.skills_mode or "on"),
        model=resolved_model,
        opencode_bin=str(args.opencode_bin or "opencode").strip() or "opencode",
        timeout_seconds=max(1, int(args.timeout_seconds or 900)),
        max_turns=max(1, int(args.max_turns or 14)),
        max_tool_calls=max(1, int(args.max_tool_calls or 36)),
        no_progress_cycles=max(1, int(args.no_progress_cycles or 3)),
        poll_interval_seconds=max(1, int(args.poll_interval_seconds or 60)),
        opencode_tool_timeout_ms=max(1000, int(args.opencode_tool_timeout_ms or 300000)),
        max_retries=max(0, int(args.max_retries or 1)),
        verbose=bool(args.verbose),
    )
    run_root = run_repobench_evaluation(project_root, config)
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
