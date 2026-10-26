from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
import csv
import html
import json
import os
import random
import re
import signal
import sys
import shutil
import subprocess
import tempfile
import time
import textwrap
import traceback
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterator

from gleskit_mcp.analysis.r_runner import RJsonScriptRunner
from gleskit_mcp.config import load_settings
from gleskit_mcp.eval.bixbench import (
    DEFAULT_BIXBENCH_RUNS_ROOT,
    build_failure_summary,
    build_portal_svg_bar_chart,
    build_question_prompt,
    build_tool_trace,
    build_visible_transcript,
    collect_file_manifest,
    current_utc_iso,
    ensure_clean_dir,
    find_session_id_by_title,
    load_bixbench_rows,
    load_opencode_session_export,
    load_opencode_session_stats,
    make_summary_rows,
    opencode_activity_kind,
    opencode_db_path,
    parse_final_answer_contract,
    read_json,
    run_id_from_time,
    numeric_for_row,
    score_bixbench_answer,
    select_bixbench_rows,
    sha256_file,
    sha256_text,
    _write_csv as write_csv,
    stage_capsule_bundle,
    summarize_rows,
    write_json_atomic,
    write_text_atomic,
)
from gleskit_mcp.eval.judge_bands import judge_band_kind, judge_category, judge_score_band
from gleskit_mcp.eval.runtime import GLESKitRuntime


REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPT_ASSET = REPO_ROOT / "docs" / "opencode" / "prompts" / "eval" / "bixbench-eval-prompt.md"
PROMPT_ASSET_NO_SKILLS = REPO_ROOT / "docs" / "opencode" / "prompts" / "eval" / "bixbench-eval-prompt-noskills.md"
OPENCODE_EVAL_AGENT_PROMPT = REPO_ROOT / "docs" / "opencode" / "prompts" / "agents" / "bixbench-eval-agent-prompt.md"
OPENCODE_EVAL_AGENT_PROMPT_NO_SKILLS = (
    REPO_ROOT / "docs" / "opencode" / "prompts" / "agents" / "bixbench-eval-agent-prompt-noskills.md"
)
OPENCODE_SKILL_ROOT = REPO_ROOT / "docs" / "opencode" / "skills" / "gleskit-bixbench-eval"
OPENCODE_CONFIG_PATH = REPO_ROOT / "docs" / "opencode" / "opencode.json"
REPO_SKILL_ROOT = OPENCODE_SKILL_ROOT
DEFAULT_BIXBENCH_MODEL = "openai/gpt-5.4-mini"
FALLBACK_MODEL_PREFERENCES = (
    "gpt-5.4-mini",
    "gpt-5-mini",
    "gpt-4.1-mini",
    "gpt-5.4-nano",
    "gpt-5-nano",
    "gpt-4.1-nano",
)
NATIVE_OPENAI_ENVIRONMENT_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
)
EVAL_PERSONA_FORBIDDEN_MARKERS = (
    "workspace_status",
    "gleskit-research-assistant",
    "references/10_workspace_mode.md",
    "references/11_uploads_mode.md",
    "references/40_literature_and_provenance.md",
    "references/50_visuals_and_dashboards.md",
    "references/60_reports_and_notes.md",
    "create_session",
    "update_session",
    "list_sessions",
    "search_papers",
)


def asset_has_forbidden_marker(text: str, marker: str) -> bool:
    for line in text.splitlines():
        lowered = line.lower()
        if marker.lower() not in lowered:
            continue
        if any(token in lowered for token in ("do not ", "don't ", "dont ", "never ", "forbidden")):
            continue
        return True
    return False


@dataclass(frozen=True)
class BixBenchRunConfig:
    run_id: str
    scope: str = "1"
    question_ids: tuple[str, ...] = ()
    limit: int = 0
    random_count: int = 0
    random_seed: int | None = None
    resume: bool = False
    continue_mode: str = "fresh"
    dry_run: bool = False
    agent: str = "bixbench-eval"
    skills_mode: str = "on"
    model: str | None = None
    opencode_bin: str = "opencode"
    timeout_seconds: int = 600
    max_turns: int = 12
    max_tool_calls: int = 30
    no_progress_cycles: int = 3
    poll_interval_seconds: int = 60
    # Zero uses the question wall-clock budget minus a finalization reserve.
    opencode_tool_timeout_ms: int = 0
    max_retries: int = 1
    verbose: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the BixBench evaluation pipeline with OpenCode.")
    parser.add_argument("--run-id", default="", help="Optional explicit run id. Defaults to a timestamped id.")
    parser.add_argument("--question-id", action="append", default=[], help="Run one exact BixBench question id.")
    parser.add_argument("--scope", default="1", help="BixBench scope filter. Use 'all' for every selected question.")
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of selected questions.")
    parser.add_argument(
        "--random-count",
        type=int,
        default=0,
        help="Randomly sample this many selected questions after filters are applied.",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=None,
        help="Optional seed for random question sampling.",
    )
    parser.add_argument("--resume", action="store_true", help="Skip questions whose episode bundle is already complete.")
    parser.add_argument(
        "--continue-mode",
        choices=("fresh", "resume", "append", "retry_failed"),
        default="fresh",
        help="How to handle an existing run id: fresh, resume planned questions, append new questions, or retry failed questions.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan the run and write manifests without launching OpenCode.")
    parser.add_argument("--agent", default="bixbench-eval", help="OpenCode agent to use.")
    parser.add_argument(
        "--skills-mode",
        choices=("on", "off"),
        default="on",
        help="Enable or disable the evaluation skill pack for this run (default: on).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_BIXBENCH_MODEL,
        help=f"OpenCode model to use (default: {DEFAULT_BIXBENCH_MODEL}).",
    )
    parser.add_argument("--opencode-bin", default="opencode", help="OpenCode binary to invoke.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=600,
        help="Wall-clock timeout per question in seconds (default: 600).",
    )
    parser.add_argument("--max-turns", type=int, default=12, help="Maximum assistant turns before stopping an episode.")
    parser.add_argument("--max-tool-calls", type=int, default=30, help="Maximum tool calls before stopping an episode.")
    parser.add_argument(
        "--no-progress-cycles",
        type=int,
        default=3,
        help="Maximum consecutive polls with no visible progress before stopping an episode.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=60,
        help="Polling interval for the stall watchdog.",
    )
    parser.add_argument(
        "--opencode-tool-timeout-ms",
        type=int,
        default=0,
        help="Timeout for active MCP calls in milliseconds; 0 derives it from the question wall-clock budget.",
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


def normalize_skills_mode(value: Any) -> str:
    token = str(value or "on").strip().lower()
    if token in {"on", "1", "true", "yes"}:
        return "on"
    if token in {"off", "0", "false", "no"}:
        return "off"
    raise ValueError(f"Unsupported skills mode: {value!r}. Use 'on' or 'off'.")


def skills_mode_enabled(value: Any) -> bool:
    return normalize_skills_mode(value) == "on"


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


def build_selection_payload(config: BixBenchRunConfig) -> dict[str, Any]:
    return {
        "scope": config.scope,
        "question_ids": list(config.question_ids),
        "limit": config.limit,
        "random_count": config.random_count,
        "random_seed": config.random_seed,
        "resume": config.resume,
        "continue_mode": normalize_continue_mode(config.continue_mode, resume_flag=config.resume),
        "dry_run": config.dry_run,
        "skills_mode": normalize_skills_mode(config.skills_mode),
    }


def _answer_value_present(answer: Any) -> bool:
    if isinstance(answer, str):
        return bool(answer.strip())
    return answer is not None


def question_has_finalized_answer(record: dict[str, Any]) -> bool:
    if str(record.get("status") or "").strip().lower() != "completed":
        return False
    final_answer = record.get("final_answer")
    if not isinstance(final_answer, dict):
        return False
    if final_answer.get("parsed") is False:
        return False
    if bool(final_answer.get("abstain")):
        return not _answer_value_present(final_answer.get("answer"))
    return _answer_value_present(final_answer.get("answer"))


def question_needs_retry(record: dict[str, Any]) -> bool:
    return not question_has_finalized_answer(record)


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
        question_id = str(payload.get("question_id") or episode_root.name).strip()
        if question_id:
            records[question_id] = payload
    return records


def load_existing_planned_question_order(
    run_root: Path,
    existing_manifest: dict[str, Any] | None,
    existing_records: dict[str, dict[str, Any]],
) -> list[str]:
    planned_path = run_root / "planned_matrix.json"
    if planned_path.exists():
        try:
            payload = read_json(planned_path)
        except Exception:
            payload = None
        if isinstance(payload, list):
            ordered = [
                str(item.get("question_id") or item.get("episode_id") or "").strip()
                for item in payload
                if isinstance(item, dict)
            ]
            ordered = [item for item in ordered if item]
            if ordered:
                seen: set[str] = set()
                result: list[str] = []
                for item in ordered:
                    if item not in seen:
                        seen.add(item)
                        result.append(item)
                for item in existing_records.keys():
                    if item not in seen:
                        seen.add(item)
                        result.append(item)
                return result
    manifest_selection = (existing_manifest or {}).get("selection") or {}
    selected_questions = manifest_selection.get("selected_questions") or []
    if isinstance(selected_questions, list):
        ordered = [
            str(item.get("question_id") or "").strip()
            for item in selected_questions
            if isinstance(item, dict)
        ]
        ordered = [item for item in ordered if item]
        if ordered:
            seen = set(ordered)
            for item in existing_records.keys():
                if item not in seen:
                    ordered.append(item)
            return ordered
    return list(existing_records.keys())


def merge_question_attempt_history(
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


def merge_question_records_by_order(
    ordered_question_ids: list[str],
    records_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question_id in ordered_question_ids:
        payload = records_by_id.get(question_id)
        if payload is None:
            continue
        merged.append(payload)
        seen.add(question_id)
    for question_id, payload in records_by_id.items():
        if question_id not in seen:
            merged.append(payload)
    return merged


def prompt_asset_for_skills_mode(skills_mode: str) -> Path:
    return PROMPT_ASSET if skills_mode_enabled(skills_mode) else PROMPT_ASSET_NO_SKILLS


def agent_prompt_asset_for_skills_mode(skills_mode: str) -> Path:
    return OPENCODE_EVAL_AGENT_PROMPT if skills_mode_enabled(skills_mode) else OPENCODE_EVAL_AGENT_PROMPT_NO_SKILLS


def load_prompt_template(skills_mode: str = "on") -> str:
    prompt_asset = prompt_asset_for_skills_mode(skills_mode)
    if not prompt_asset.exists():
        raise FileNotFoundError(
            f"Missing BixBench OpenCode prompt template: {prompt_asset}. "
            "Run `make opencode_setup` to create the OpenCode symlinks."
        )
    validate_eval_persona_assets(skills_mode)
    return prompt_asset.read_text(encoding="utf-8").strip()


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
                OPENCODE_SKILL_ROOT / "references" / "00_eval_core_rules.md",
                OPENCODE_SKILL_ROOT / "references" / "12_tool_call_contract.md",
                OPENCODE_SKILL_ROOT / "references" / "30_rnaseq_and_statistics.md",
                OPENCODE_SKILL_ROOT / "references" / "40_finalization_and_abstention.md",
            ]
        )
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Controlled evaluation persona assets are incomplete. Missing: " + ", ".join(missing)
        )

    config_payload = json.loads(OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
    agent_payload = (((config_payload.get("agent") or {}) if isinstance(config_payload, dict) else {}) or {}).get("bixbench-eval")
    if not isinstance(agent_payload, dict):
        raise ValueError("OpenCode config is missing the bixbench-eval agent definition.")
    permission_payload = agent_payload.get("permission") if isinstance(agent_payload.get("permission"), dict) else {}
    allowed_skills = (permission_payload.get("skill") or {}) if isinstance(permission_payload, dict) else {}
    if skills_mode_enabled(normalized_mode) and (
        not isinstance(allowed_skills, dict) or allowed_skills.get("gleskit-bixbench-eval") != "allow"
    ):
        raise ValueError("bixbench-eval agent must allow the gleskit-bixbench-eval skill.")

    for path in required_paths[1:]:
        text = path.read_text(encoding="utf-8")
        for marker in EVAL_PERSONA_FORBIDDEN_MARKERS:
            if asset_has_forbidden_marker(text, marker):
                raise ValueError(
                    f"Controlled evaluation persona asset {path} contains forbidden marker {marker!r}."
                )


def snapshot_single_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def snapshot_tree(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "file_count": 0, "bundle_sha256": None, "files": []}
    files = collect_file_manifest(path)
    return {
        "path": str(path),
        "exists": True,
        "file_count": len(files),
        "bundle_sha256": sha256_text(json.dumps(files, sort_keys=True, default=str)),
        "files": files,
    }


def probe_binary_version(binary: str) -> dict[str, Any]:
    try:
        completed = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
        version_text = (completed.stdout or completed.stderr or "").strip()
        return {
            "binary": binary,
            "available": completed.returncode == 0,
            "returncode": completed.returncode,
            "version": version_text or None,
        }
    except Exception as exc:
        return {"binary": binary, "available": False, "error": str(exc)}


def load_opencode_model_catalog(opencode_config_path: Path) -> dict[str, tuple[str, ...]]:
    if not opencode_config_path.exists():
        return {}
    try:
        config = json.loads(opencode_config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    providers = config.get("provider") or {}
    if not isinstance(providers, dict):
        return {}
    catalog: dict[str, tuple[str, ...]] = {}
    for provider_id, payload in providers.items():
        if not isinstance(payload, dict):
            continue
        models_payload = payload.get("models") or {}
        model_ids: list[str] = []
        if isinstance(models_payload, dict):
            model_ids = [str(model_id).strip() for model_id in models_payload.keys() if str(model_id).strip()]
        elif isinstance(models_payload, list):
            model_ids = [str(model_id).strip() for model_id in models_payload if str(model_id).strip()]
        unique_ids = tuple(dict.fromkeys(model_ids))
        if unique_ids:
            catalog[str(provider_id).strip()] = unique_ids
    return catalog


def choose_preferred_opencode_model(catalog: dict[str, tuple[str, ...]]) -> str:
    if not catalog:
        return DEFAULT_BIXBENCH_MODEL
    provider_order = list(dict.fromkeys(["openai", *catalog.keys()]))
    for provider_id in provider_order:
        model_ids = catalog.get(provider_id, ())
        if not model_ids:
            continue
        for preferred_model in FALLBACK_MODEL_PREFERENCES:
            if preferred_model in model_ids:
                return f"{provider_id}/{preferred_model}"
    first_provider = next(iter(catalog))
    return f"{first_provider}/{catalog[first_provider][0]}"


def resolve_opencode_model(requested_model: str, opencode_config_path: Path) -> tuple[str, str | None]:
    cleaned = str(requested_model or "").strip()
    catalog = load_opencode_model_catalog(opencode_config_path)
    if not catalog:
        return cleaned or DEFAULT_BIXBENCH_MODEL, None

    def _providers_for_model(model_id: str) -> list[str]:
        return [provider_id for provider_id, model_ids in catalog.items() if model_id in model_ids]

    fallback_model = choose_preferred_opencode_model(catalog)
    if not cleaned:
        return fallback_model, f"No model was requested; using configured OpenCode model `{fallback_model}`."

    if "/" not in cleaned:
        matches = _providers_for_model(cleaned)
        if len(matches) == 1:
            resolved = f"{matches[0]}/{cleaned}"
            return resolved, (
                None
                if resolved == cleaned
                else f"Resolved bare model id `{cleaned}` to configured OpenCode model `{resolved}`."
            )
        if len(matches) > 1:
            options = ", ".join(f"{provider_id}/{cleaned}" for provider_id in matches)
            raise ValueError(f"Ambiguous OpenCode model `{cleaned}`. Choose one of: {options}.")
        if cleaned == DEFAULT_BIXBENCH_MODEL:
            return fallback_model, (
                f"Requested default model `{cleaned}` is not configured locally; using `{fallback_model}` instead."
            )
        raise ValueError(
            f"OpenCode model `{cleaned}` is not configured in {opencode_config_path}. "
            f"Available models: {', '.join(f'{provider_id}/{model_id}' for provider_id, model_ids in catalog.items() for model_id in model_ids)}."
        )

    provider_id, model_id = cleaned.split("/", 1)
    provider_id = provider_id.strip()
    model_id = model_id.strip()
    if provider_id in catalog and model_id in catalog[provider_id]:
        return f"{provider_id}/{model_id}", None

    available = ", ".join(f"{candidate_provider}/{candidate_model}" for candidate_provider, model_ids in catalog.items() for candidate_model in model_ids)
    raise ValueError(
        f"OpenCode model `{cleaned}` is not configured in {opencode_config_path}. Available models: {available}."
    )


def opencode_subprocess_environment(
    model: str | None,
    *,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Prepare a child environment without letting an API key override OpenAI OAuth."""
    env = dict(os.environ if base_environment is None else base_environment)
    provider_id = str(model or "").strip().partition("/")[0].lower()
    if provider_id == "openai":
        for key in NATIVE_OPENAI_ENVIRONMENT_KEYS:
            env.pop(key, None)
    return env


def sync_native_openai_oauth_from_codex(
    opencode_auth_path: Path,
    *,
    codex_auth_path: Path | None = None,
    now_seconds: float | None = None,
) -> bool:
    """Seed an isolated OpenCode home from the current local Codex OAuth session."""
    source_path = codex_auth_path or (Path.home() / ".codex" / "auth.json")
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
        tokens = source.get("tokens") if isinstance(source, dict) else None
        if not isinstance(tokens, dict):
            return False
        access = str(tokens.get("access_token") or "").strip()
        refresh = str(tokens.get("refresh_token") or "").strip()
        account_id = str(tokens.get("account_id") or "").strip()
        encoded_claims = access.split(".")[1]
        encoded_claims += "=" * (-len(encoded_claims) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded_claims))
        expires = int(float(claims.get("exp"))) * 1000
        now_millis = int((time.time() if now_seconds is None else now_seconds) * 1000)
        if not access or not refresh or not account_id or expires <= now_millis + 60_000:
            return False
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return False

    try:
        existing = json.loads(opencode_auth_path.read_text(encoding="utf-8")) if opencode_auth_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["openai"] = {
        "type": "oauth",
        "refresh": refresh,
        "access": access,
        "expires": expires,
        "accountId": account_id,
    }
    write_json_atomic(opencode_auth_path, existing)
    return True


def probe_git_metadata(project_root: Path) -> dict[str, Any]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        git_sha = (sha.stdout or "").strip() or None
        return {
            "sha": git_sha,
            "dirty": bool((status.stdout or "").strip()),
            "returncode": sha.returncode,
        }
    except Exception as exc:
        return {"sha": None, "dirty": None, "error": str(exc)}


async def snapshot_tool_schemas(project_root: Path, run_root: Path) -> dict[str, Any]:
    try:
        with tempfile.TemporaryDirectory(dir=run_root) as tmpdir:
            temp_root = Path(tmpdir)
            workspace_root = temp_root / "workspace"
            runtime_root = temp_root / "runtime"
            workspace_root.mkdir(parents=True, exist_ok=True)
            runtime_root.mkdir(parents=True, exist_ok=True)
            runtime = GLESKitRuntime(
                project_root=project_root,
                workspace_root=workspace_root,
                runtime_root=runtime_root,
            )
            try:
                await runtime.start()
                return {
                    "available": True,
                    "tool_count": len(runtime.tools),
                    "tools": runtime.openai_tools,
                }
            finally:
                try:
                    await runtime.shutdown()
                except Exception:
                    pass
    except Exception as exc:
        return {"available": False, "tool_count": 0, "tools": [], "error": str(exc)}


def make_session_title(run_id: str, question_id: str, attempt: int, *, nonce: str | None = None) -> str:
    suffix = f" {nonce}" if nonce else ""
    return f"GLESKit BixBench {run_id} {question_id} a{attempt}{suffix}"


def group_rows_by_capsule(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        capsule_uuid = str(row.get("capsule_uuid") or "").strip()
        grouped.setdefault(capsule_uuid, []).append(row)
    return grouped


def build_capsule_summary(
    *,
    capsule_uuid: str,
    capsule_bundle: dict[str, Any],
    question_ids: list[str],
) -> dict[str, Any]:
    file_manifest = capsule_bundle.get("files") or {"visible": [], "audit": []}
    visible_count = len(file_manifest.get("visible") or [])
    audit_count = len(file_manifest.get("audit") or [])
    return {
        "capsule_uuid": capsule_uuid,
        "capsule_root": capsule_bundle.get("capsule_root"),
        "visible_root": capsule_bundle.get("visible_root"),
        "audit_root": capsule_bundle.get("audit_root"),
        "source_root": capsule_bundle.get("source_root"),
        "question_ids": question_ids,
        "question_count": len(question_ids),
        "visible_file_count": visible_count,
        "audit_file_count": audit_count,
        "bundle_sha256": capsule_bundle.get("bundle_sha256"),
        "files": file_manifest,
    }


def build_question_input_payload(
    *,
    row: dict[str, Any],
    run_id: str,
    episode_root: Path,
    session_title: str,
    capsule_bundle: dict[str, Any],
    prompt_text: str,
    opencode_command: list[str],
    config: BixBenchRunConfig,
) -> dict[str, Any]:
    visible_files = list(
        capsule_bundle.get("prompt_visible_files")
        or [item["path"] for item in (capsule_bundle.get("files") or {}).get("visible", [])]
    )
    audit_files = [item["path"] for item in (capsule_bundle.get("files") or {}).get("audit", [])]
    prompt_sha256 = sha256_text(prompt_text)
    prompt_asset = prompt_asset_for_skills_mode(config.skills_mode)
    return {
        "run_id": run_id,
        "episode_root": str(episode_root),
        "session_title": session_title,
        "question": {
            "question_id": row.get("question_id"),
            "short_id": row.get("short_id"),
            "scope": row.get("scope"),
            "numeric": row.get("numeric"),
            "eval_mode": row.get("eval_mode"),
            "capsule_uuid": row.get("capsule_uuid"),
            "question": row.get("question"),
            "ideal": row.get("ideal"),
            "categories": row.get("categories"),
            "paper": row.get("paper"),
        },
        "capsule_bundle": {
            "capsule_uuid": capsule_bundle.get("capsule_uuid"),
            "capsule_root": capsule_bundle.get("capsule_root"),
            "visible_root": capsule_bundle.get("visible_root"),
            "audit_root": capsule_bundle.get("audit_root"),
            "source_root": capsule_bundle.get("source_root"),
            "bundle_sha256": capsule_bundle.get("bundle_sha256"),
            "visible_files": visible_files,
            "audit_files": audit_files,
        },
        "prompt": {
            "path": str(prompt_asset),
            "sha256": sha256_file(prompt_asset),
            "text": prompt_text,
            "text_sha256": prompt_sha256,
        },
        "opencode": {
            "binary": config.opencode_bin,
            "agent": config.agent,
            "skills_mode": normalize_skills_mode(config.skills_mode),
            "model": config.model,
            "command": opencode_command,
        },
        "selection": build_selection_payload(config),
    }


def build_opencode_command(
    *,
    config: BixBenchRunConfig,
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


def effective_opencode_tool_timeout_ms(config: BixBenchRunConfig) -> int:
    """Keep one active tool within the episode budget without using a short hidden cap."""
    reserve_seconds = max(30, int(config.poll_interval_seconds or 1))
    budget_ms = max(1_000, (max(1, int(config.timeout_seconds)) - reserve_seconds) * 1_000)
    requested_ms = max(0, int(config.opencode_tool_timeout_ms or 0))
    return min(requested_ms, budget_ms) if requested_ms else budget_ms


def link_episode_capsule_alias(*, episode_root: Path, capsule_root: str) -> Path:
    """Expose a safe compatibility path for agents that anchor paths at episode_root."""
    canonical_root = Path(capsule_root).expanduser().resolve()
    if not canonical_root.is_dir():
        raise FileNotFoundError(f"Capsule root does not exist: {canonical_root}")
    alias = episode_root / "capsules" / canonical_root.name
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_symlink():
        if alias.resolve() != canonical_root:
            raise RuntimeError(f"Refusing to reuse capsule alias with a different target: {alias}")
        return alias
    if alias.exists():
        if alias.resolve() != canonical_root:
            raise RuntimeError(f"Refusing to reuse capsule alias with a different target: {alias}")
        return alias
    alias.symlink_to(canonical_root, target_is_directory=True)
    return alias


def extend_opencode_tool_timeout(opencode_config_path: Path, timeout_ms: int) -> None:
    if not opencode_config_path.exists():
        return
    try:
        config = json.loads(opencode_config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    mcp_config = config.setdefault("mcp", {})
    if not isinstance(mcp_config, dict):
        return
    gleskit_config = mcp_config.get("gleskit")
    if not isinstance(gleskit_config, dict):
        return
    current_timeout = int(gleskit_config.get("timeout") or 0)
    gleskit_config["timeout"] = max(current_timeout, int(timeout_ms))
    write_json_atomic(opencode_config_path, config)


def harden_opencode_eval_runtime(opencode_config_path: Path) -> None:
    if not opencode_config_path.exists():
        return
    try:
        config = json.loads(opencode_config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    config["snapshot"] = False
    watcher_config = config.setdefault("watcher", {})
    if isinstance(watcher_config, dict):
        ignore_patterns = watcher_config.get("ignore")
        existing = ignore_patterns if isinstance(ignore_patterns, list) else []
        merged = list(dict.fromkeys([*existing, ".git", ".git/**", "**/.git/**"]))
        watcher_config["ignore"] = merged
    write_json_atomic(opencode_config_path, config)


def configure_opencode_eval_agent(
    opencode_config_path: Path,
    *,
    agent_name: str,
    agent_prompt_path: Path,
    skill_name: str | None,
    skill_root_name: str | None,
    skills_mode: str,
) -> None:
    if not opencode_config_path.exists():
        return
    try:
        config = json.loads(opencode_config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    agents = config.get("agent")
    if not isinstance(agents, dict):
        return
    agent_payload = agents.get(agent_name)
    if not isinstance(agent_payload, dict):
        return
    agent_payload["prompt"] = f"{{file:{agent_prompt_path.resolve()}}}"
    permission_payload = agent_payload.setdefault("permission", {})
    if not isinstance(permission_payload, dict):
        write_json_atomic(opencode_config_path, config)
        return
    if skill_name:
        skill_permissions = permission_payload.setdefault("skill", {})
        if isinstance(skill_permissions, dict):
            skill_permissions.setdefault("*", "deny")
            skill_permissions[skill_name] = "allow" if skills_mode_enabled(skills_mode) else "deny"
    if skill_root_name:
        for key in ("read", "external_directory"):
            file_permissions = permission_payload.get(key)
            if not isinstance(file_permissions, dict):
                continue
            for pattern in list(file_permissions.keys()):
                if skill_root_name in pattern:
                    file_permissions[pattern] = "allow" if skills_mode_enabled(skills_mode) else "deny"
    write_json_atomic(opencode_config_path, config)


def _allow_eval_read_paths(opencode_config_path: Path, paths: list[Path]) -> None:
    if not opencode_config_path.exists():
        return
    try:
        config = json.loads(opencode_config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    agent_payload = (((config.get("agent") or {}) if isinstance(config, dict) else {}) or {}).get("bixbench-eval")
    if not isinstance(agent_payload, dict):
        return
    permission_payload = agent_payload.setdefault("permission", {})
    if not isinstance(permission_payload, dict):
        return
    read_rules = permission_payload.setdefault("read", {})
    external_rules = permission_payload.setdefault("external_directory", {})
    if not isinstance(read_rules, dict) or not isinstance(external_rules, dict):
        return
    additions: dict[str, str] = {}
    for raw_path in paths:
        if not raw_path:
            continue
        resolved = Path(raw_path).resolve()
        patterns = [str(resolved)]
        if resolved.is_dir():
            patterns.append(f"{resolved}/**")
        for pattern in patterns:
            additions[pattern] = "allow"
    read_rules = {**additions, **{key: value for key, value in read_rules.items() if key not in additions}}
    external_rules = {**additions, **{key: value for key, value in external_rules.items() if key not in additions}}
    permission_payload["read"] = read_rules
    permission_payload["external_directory"] = external_rules
    write_json_atomic(opencode_config_path, config)


def build_manifest_payload(
    *,
    project_root: Path,
    run_root: Path,
    config: BixBenchRunConfig,
    dataset_path: Path,
    dataset_rows: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]],
    capsule_summaries: list[dict[str, Any]],
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
        "dataset": {
            "path": str(dataset_path),
            "sha256": sha256_file(dataset_path),
            "row_count": len(dataset_rows),
        },
        "selection": {
            **build_selection_payload(config),
            "selected_question_count": len(selected_rows),
            "selected_questions": [
                {
                    "question_id": row.get("question_id"),
                    "short_id": row.get("short_id"),
                    "scope": row.get("scope"),
                    "numeric": row.get("numeric"),
                    "species": row.get("species"),
                    "eval_mode": row.get("eval_mode"),
                    "capsule_uuid": row.get("capsule_uuid"),
                }
                for row in selected_rows
            ],
        },
        "opencode": {
            "binary": config.opencode_bin,
            "binary_version": probe_binary_version(config.opencode_bin),
            "agent": config.agent,
            "skills_mode": normalize_skills_mode(config.skills_mode),
            "skills_enabled": skills_mode_enabled(config.skills_mode),
            "model": config.model or None,
            "timeout_seconds": config.timeout_seconds,
            "tool_timeout_ms": config.opencode_tool_timeout_ms,
            "effective_tool_timeout_ms": effective_opencode_tool_timeout_ms(config),
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
        "repo_skill_pack": snapshot_tree(REPO_SKILL_ROOT),
        "tool_schema_snapshot": tool_schema_snapshot,
        "capsules": capsule_summaries,
        "planned_episode_count": len(selected_rows),
        "prompt_template_sha256": sha256_text(prompt_template),
    }


def load_existing_manifest(run_root: Path) -> dict[str, Any] | None:
    manifest_path = run_root / "run_manifest.json"
    if not manifest_path.exists():
        return None
    return read_json(manifest_path)


def load_existing_planned_matrix(run_root: Path) -> list[dict[str, Any]]:
    planned_path = run_root / "planned_matrix.json"
    if not planned_path.exists():
        return []
    payload = read_json(planned_path)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def merge_capsule_summaries(
    existing_manifest: dict[str, Any] | None,
    current_capsules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in ((existing_manifest or {}).get("capsules") or []):
        if isinstance(item, dict):
            capsule_uuid = str(item.get("capsule_uuid") or "").strip()
            if capsule_uuid:
                merged[capsule_uuid] = item
    for item in current_capsules:
        capsule_uuid = str(item.get("capsule_uuid") or "").strip()
        if capsule_uuid:
            merged[capsule_uuid] = item
    return [merged[key] for key in sorted(merged)]


def build_selection_history_event(
    config: BixBenchRunConfig,
    *,
    continue_mode: str,
    selected_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "created_at": current_utc_iso(),
        "continue_mode": continue_mode,
        "selection": build_selection_payload(config),
        "selected_question_ids": [str(row.get("question_id") or "").strip() for row in selected_rows],
        "selected_question_count": len(selected_rows),
    }


def _question_requires_apeglm(row: dict[str, Any]) -> bool:
    return "apeglm" in str(row.get("question") or "").strip().lower()


def _validate_selected_runtime_dependencies(project_root: Path, selected_rows: list[dict[str, Any]]) -> None:
    if not any(_question_requires_apeglm(row) for row in selected_rows):
        return
    settings = load_settings(project_root)
    runner = RJsonScriptRunner(settings)
    try:
        completed = subprocess.run(
            [
                settings.rscript_bin,
                "-e",
                "cat(as.character(requireNamespace('apeglm', quietly=TRUE)))",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=runner._runtime_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Selected BixBench questions require apeglm shrinkage, but `{settings.rscript_bin}` is unavailable."
        ) from exc
    available = (completed.stdout or "").strip().lower() == "true"
    if available and completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip()
    raise RuntimeError(
        "Selected BixBench questions require the R package `apeglm`, but it is not available in the current GLESKit runtime. "
        f"R_LIBS_USER is expected under {settings.r_lib_root}. {detail}".strip()
    )


def select_rows_for_continue_mode(
    *,
    dataset_rows: list[dict[str, Any]],
    config: BixBenchRunConfig,
    continue_mode: str,
    run_root: Path,
    existing_manifest: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    filtered_rows = select_bixbench_rows(dataset_rows, question_ids=config.question_ids, scope=config.scope)
    existing_records = load_existing_episode_records(run_root)
    existing_order = load_existing_planned_question_order(run_root, existing_manifest, existing_records)
    existing_ids = set(existing_order) | set(existing_records.keys())
    filtered_by_id = {
        str(row.get("question_id") or "").strip(): row
        for row in filtered_rows
        if str(row.get("question_id") or "").strip()
    }

    if continue_mode == "fresh":
        selected_rows = _sample_or_limit_rows(
            filtered_rows,
            limit=config.limit,
            random_count=config.random_count,
            random_seed=config.random_seed,
        )
        return selected_rows, existing_records, [str(row["question_id"]) for row in selected_rows]

    if not existing_manifest and not existing_records:
        raise FileNotFoundError(
            f"Run root does not contain an existing BixBench run to continue: {run_root}"
        )

    if continue_mode == "resume":
        resume_candidates = [
            filtered_by_id[question_id]
            for question_id in existing_order
            if question_id in filtered_by_id and question_needs_retry(existing_records.get(question_id, {}))
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
            if str(row.get("question_id") or "").strip() not in existing_ids
        ]
        selected_rows = _sample_or_limit_rows(
            append_candidates,
            limit=config.limit,
            random_count=config.random_count,
            random_seed=config.random_seed,
        )
        appended_ids = [str(row["question_id"]) for row in selected_rows]
        combined_order = list(existing_order)
        for question_id in appended_ids:
            if question_id not in combined_order:
                combined_order.append(question_id)
        return selected_rows, existing_records, combined_order

    retry_candidates = [
        filtered_by_id[question_id]
        for question_id in existing_order
        if question_id in filtered_by_id
        and question_id in existing_records
        and question_needs_retry(existing_records.get(question_id, {}))
    ]
    selected_rows = _sample_or_limit_rows(
        retry_candidates,
        limit=config.limit,
        random_count=config.random_count,
        random_seed=config.random_seed,
    )
    return selected_rows, existing_records, list(existing_order)


def build_planned_matrix(
    *,
    run_id: str,
    episode_root: Path,
    row: dict[str, Any],
    capsule_bundle: dict[str, Any],
    prompt_text: str,
    opencode_command: list[str],
    selected_index: int,
    config: BixBenchRunConfig,
) -> dict[str, Any]:
    prompt_sha256 = sha256_text(prompt_text)
    return {
        "run_id": run_id,
        "selected_index": selected_index,
        "episode_id": row["question_id"],
        "episode_root": str(episode_root),
        "question_id": row["question_id"],
        "short_id": row.get("short_id"),
        "scope": row.get("scope"),
        "numeric": row.get("numeric"),
        "species": row.get("species"),
        "eval_mode": row.get("eval_mode"),
        "capsule_uuid": row.get("capsule_uuid"),
        "session_title": make_session_title(run_id, str(row["question_id"]), 1),
        "capsule_root": capsule_bundle.get("capsule_root"),
        "visible_root": capsule_bundle.get("visible_root"),
        "audit_root": capsule_bundle.get("audit_root"),
        "bundle_sha256": capsule_bundle.get("bundle_sha256"),
        "prompt_sha256": prompt_sha256,
        "opencode_command": opencode_command,
        "question": row.get("question"),
        "ideal": row.get("ideal"),
        "selection": build_selection_payload(config),
        "files": capsule_bundle.get("files"),
    }


def make_retryable_error(text: str) -> bool:
    lowered = text.lower()
    patterns = (
        "unable to connect",
        "unexpected error",
        "failed to run the query",
        "database is locked",
        "connection reset",
        "connectionrefused",
        "econnreset",
        "econnrefused",
        "timed out",
        "temporary",
        "temporarily",
        "transport",
        "failed to fetch models.dev",
    )
    return any(pattern in lowered for pattern in patterns)


def _tool_error_metadata(item: dict[str, Any]) -> tuple[str, bool | None, str] | None:
    """Extract a stable error fingerprint without retaining bulky tool payloads."""
    status = str(item.get("status") or "").strip().lower()
    output = item.get("output")
    if isinstance(output, dict):
        error_code = str(output.get("error_code") or "").strip()
        retryable_value = output.get("retryable")
        diagnostic = str(output.get("diagnostic") or output.get("error") or output.get("message") or "").strip()
        if not diagnostic and status not in {"error", "failed"}:
            return None
        retryable = retryable_value if isinstance(retryable_value, bool) else None
        if not error_code:
            error_code_match = re.search(r"error_code\s*[:=]\s*([A-Za-z0-9_.-]+)", diagnostic, flags=re.IGNORECASE)
            error_code = error_code_match.group(1) if error_code_match else "tool_error"
        return error_code, retryable, diagnostic
    diagnostic = str(output or "").strip()
    if not diagnostic and status not in {"error", "failed"}:
        return None
    error_code_match = re.search(r"error_code\s*[:=]\s*([A-Za-z0-9_.-]+)", diagnostic, flags=re.IGNORECASE)
    retryable_match = re.search(r"retryable\s*[:=]\s*(true|false)", diagnostic, flags=re.IGNORECASE)
    error_code = error_code_match.group(1) if error_code_match else "tool_error"
    retryable = None if retryable_match is None else retryable_match.group(1).lower() == "true"
    return error_code, retryable, diagnostic


def repeated_nonretryable_tool_error(
    tool_trace: list[dict[str, Any]],
    *,
    repeat_limit: int = 2,
) -> dict[str, Any] | None:
    """Detect repeated schema/data failures so an agent cannot loop forever."""
    fingerprints: Counter[tuple[str, str, str]] = Counter()
    examples: dict[tuple[str, str, str], str] = {}
    for item in tool_trace:
        if not isinstance(item, dict):
            continue
        metadata = _tool_error_metadata(item)
        if metadata is None:
            continue
        error_code, retryable, diagnostic = metadata
        normalized = re.sub(r"\s+", " ", diagnostic).strip().casefold()
        if retryable is True or any(token in error_code.casefold() for token in ("timeout", "connection", "rate", "transport")):
            continue
        if retryable is None and not any(
            marker in normalized
            for marker in ("invalid", "requires", "must", "not found", "available columns", "cannot", "unsupported")
        ):
            continue
        key = (str(item.get("tool") or "unknown_tool"), error_code, normalized[:500])
        fingerprints[key] += 1
        examples[key] = diagnostic[:1000]
    for (tool, error_code, fingerprint), count in fingerprints.items():
        if count >= max(2, int(repeat_limit)):
            return {
                "error_code": error_code if error_code != "tool_error" else "invalid_input",
                "retryable": False,
                "suggested_next_step": "Correct the input schema or use the exact labels returned by inspection; do not repeat the same call.",
                "diagnostic": examples[(tool, error_code, fingerprint)],
                "tool": tool,
                "repeat_count": count,
            }
    return None


def _runtime_log_paths(log_dir: Path) -> list[Path]:
    if not log_dir.exists():
        return []
    return sorted(path for path in log_dir.glob("*.log") if path.is_file())


def diagnose_opencode_runtime_failure(log_dir: Path) -> dict[str, Any] | None:
    def _make_result(
        *,
        stop_reason: str,
        failure_type: str,
        retryable: bool,
        provider_id: str | None,
        model_id: str | None,
        path: Path,
        line: str,
    ) -> dict[str, Any]:
        return {
            "stop_reason": stop_reason,
            "failure_type": failure_type,
            "retryable": retryable,
            "provider_id": provider_id,
            "model_id": model_id,
            "log_path": str(path),
            "excerpt": line[:4000],
        }

    for log_path in _runtime_log_paths(log_dir):
        try:
            for raw_line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                provider_match = re.search(r"providerID=(\S+)", line)
                model_match = re.search(r"modelID=(\S+)", line)
                provider_id = provider_match.group(1) if provider_match else None
                model_id = model_match.group(1) if model_match else None
                lowered = line.lower()

                if "providermodelnotfounderror" in lowered:
                    return _make_result(
                        stop_reason="provider_model_not_found",
                        failure_type="provider_model_not_found",
                        retryable=False,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
                if "ai_apicallerror" in lowered and ("no endpoints found" in lowered or "\"code\":404" in lowered):
                    return _make_result(
                        stop_reason="provider_model_unavailable",
                        failure_type="provider_model_unavailable",
                        retryable=False,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
                if "connectionrefused" in lowered and (
                    "ai_apicallerror" in lowered
                    or "service=llm" in lowered
                    or "chat/completions" in lowered
                ):
                    return _make_result(
                        stop_reason="provider_connection_refused",
                        failure_type="provider_connection_refused",
                        retryable=True,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
                if "ai_apicallerror" in lowered and ("timed out" in lowered or "timeout" in lowered):
                    return _make_result(
                        stop_reason="provider_timeout",
                        failure_type="provider_timeout",
                        retryable=True,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
                if "ai_apicallerror" in lowered and ("rate limit" in lowered or "ratelimit" in lowered):
                    return _make_result(
                        stop_reason="provider_rate_limited",
                        failure_type="provider_rate_limited",
                        retryable=True,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
                if "usage_limit_reached" in lowered or "the usage limit has been reached" in lowered:
                    return _make_result(
                        stop_reason="provider_usage_limit_reached",
                        failure_type="provider_usage_limit_reached",
                        retryable=False,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
                if "insufficient_quota" in lowered or "quota exceeded" in lowered:
                    return _make_result(
                        stop_reason="provider_quota_exceeded",
                        failure_type="provider_quota_exceeded",
                        retryable=False,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
                if "ai_apicallerror" in lowered and ("authentication" in lowered or "unauthorized" in lowered):
                    return _make_result(
                        stop_reason="provider_auth_error",
                        failure_type="provider_auth_error",
                        retryable=False,
                        provider_id=provider_id,
                        model_id=model_id,
                        path=log_path,
                        line=line,
                    )
        except Exception:
            continue
    return None


def terminate_process(process: subprocess.Popen[Any], *, grace_seconds: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=grace_seconds)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=grace_seconds)
    except Exception:
        pass


def _assert_isolated_eval_roots(*, settings: Any, workspace_root: Path, runtime_root: Path) -> None:
    expected_workspace_root = Path(workspace_root).resolve()
    expected_runtime_root = Path(runtime_root).resolve()
    resolved_workspace_root = Path(settings.workspace_default_root).resolve()
    resolved_runtime_root = Path(settings.runtime_root).resolve()
    if resolved_workspace_root != expected_workspace_root:
        raise RuntimeError(
            "BixBench evaluation refused to start because the resolved workspace root does not match the run-local "
            f"workspace. expected={expected_workspace_root} resolved={resolved_workspace_root}"
        )
    if resolved_runtime_root != expected_runtime_root:
        raise RuntimeError(
            "BixBench evaluation refused to start because the resolved runtime root does not match the run-local "
            f"runtime. expected={expected_runtime_root} resolved={resolved_runtime_root}"
        )


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


@contextmanager
def _capture_runner_interruptions() -> Iterator[dict[str, int | None]]:
    """Convert normal CLI stop signals into a durable terminal episode record.

    SIGKILL cannot be intercepted, but SIGINT/SIGTERM (including a normal
    process stop from the evaluation operator) are retained long enough for the
    runner to terminate the child and persist its latest OpenCode session state.
    """
    state: dict[str, int | None] = {"signal": None}
    if threading.current_thread() is not threading.main_thread():
        yield state
        return
    handled_signals = (signal.SIGINT, signal.SIGTERM)
    previous_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}

    def _record_interruption(signum: int, _frame: Any) -> None:
        state["signal"] = signum

    try:
        for signum in handled_signals:
            signal.signal(signum, _record_interruption)
        yield state
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


def run_opencode_attempt(
    *,
    project_root: Path,
    run_root: Path,
    workspace_root: Path,
    runtime_root: Path,
    row: dict[str, Any],
    capsule_bundle: dict[str, Any],
    config: BixBenchRunConfig,
    attempt: int,
    prompt_text: str,
) -> dict[str, Any]:
    episode_root = run_root / "episodes" / str(row["question_id"])
    attempt_root = episode_root / "private" / "attempts" / f"attempt_{attempt:02d}"
    attempt_root.mkdir(parents=True, exist_ok=True)
    opencode_home = attempt_root / "opencode-home"
    ensure_clean_dir(opencode_home)
    source_opencode_config = REPO_ROOT / "docs" / "opencode"
    source_opencode_state = Path.home() / ".local" / "share" / "opencode"
    source_opencode_cache = Path.home() / ".cache" / "opencode"
    tool_timeout_ms = effective_opencode_tool_timeout_ms(config)
    target_opencode_config = opencode_home / ".config" / "opencode"
    target_opencode_state = opencode_home / ".local" / "share" / "opencode"
    if source_opencode_config.exists():
        target_opencode_config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_opencode_config, target_opencode_config, dirs_exist_ok=True)
        opencode_config_copy = target_opencode_config / "opencode.json"
        harden_opencode_eval_runtime(opencode_config_copy)
        extend_opencode_tool_timeout(
            opencode_config_copy,
            timeout_ms=tool_timeout_ms,
        )
        _allow_eval_read_paths(
            opencode_config_copy,
            [
                target_opencode_config / "skills" / OPENCODE_SKILL_ROOT.name,
            ],
        )
        configure_opencode_eval_agent(
            opencode_config_copy,
            agent_name=config.agent,
            agent_prompt_path=agent_prompt_asset_for_skills_mode(config.skills_mode),
            skill_name="gleskit-bixbench-eval",
            skill_root_name=OPENCODE_SKILL_ROOT.name,
            skills_mode=config.skills_mode,
        )
    if source_opencode_state.exists():
        target_opencode_state.mkdir(parents=True, exist_ok=True)
        # Preserve native provider credentials without importing stale sessions or logs.
        auth_path = source_opencode_state / "auth.json"
        if auth_path.is_file():
            shutil.copy2(auth_path, target_opencode_state / "auth.json")
    if str(config.model or "").split("/", 1)[0].lower() == "openai":
        target_opencode_state.mkdir(parents=True, exist_ok=True)
        sync_native_openai_oauth_from_codex(target_opencode_state / "auth.json")
    if source_opencode_cache.exists():
        target_opencode_cache = opencode_home / ".cache" / "opencode"
        target_opencode_cache.mkdir(parents=True, exist_ok=True)
        models_path = source_opencode_cache / "models.json"
        if models_path.is_file():
            shutil.copy2(models_path, target_opencode_cache / "models.json")
    session_title = make_session_title(config.run_id, str(row["question_id"]), attempt)
    live_session_title = make_session_title(config.run_id, str(row["question_id"]), attempt, nonce=str(time.time_ns()))
    opencode_command = build_opencode_command(
        config=config,
        project_dir=episode_root,
        session_title=live_session_title,
        prompt_text=prompt_text,
    )
    input_payload = build_question_input_payload(
        row=row,
        run_id=config.run_id,
        episode_root=episode_root,
        session_title=session_title,
        capsule_bundle=capsule_bundle,
        prompt_text=prompt_text,
        opencode_command=opencode_command,
        config=config,
    )
    input_payload["live_session_title"] = live_session_title
    input_payload.setdefault("opencode", {})
    input_payload["opencode"]["runtime_home"] = str(opencode_home)
    input_payload["opencode"]["runtime_config_home"] = str(opencode_home / ".config")
    input_payload["opencode"]["runtime_data_home"] = str(opencode_home / ".local" / "share")
    input_payload["opencode"]["gleskit_workspace_home"] = str(Path(workspace_root).resolve())
    input_payload["opencode"]["gleskit_runtime_home"] = str(Path(runtime_root).resolve())
    write_json_atomic(attempt_root / "input.json", input_payload)

    stdout_path = attempt_root / "opencode.stdout.log"
    stderr_path = attempt_root / "opencode.stderr.log"
    started_at = time.monotonic()
    session_id: str | None = None
    last_signature: tuple[Any, ...] | None = None
    no_progress = 0
    active_tool_started_at: float | None = None
    pending_provider_started_at: float | None = None
    watchdog_triggered = False
    stop_reason = "completed"
    returncode: int | None = None
    last_checked_tool_part_count = -1
    failure_diagnostic: dict[str, Any] | None = None
    interruption_signal: int | None = None

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

    try:
        env = opencode_subprocess_environment(config.model)
        env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
        env["GLESKIT_DISABLE_INLINE_FIGURES"] = "1"
        env["GLESKIT_COMPACT_TOOL_PAYLOADS"] = "1"
        # Let composite local workflows share the same ceiling as the active
        # MCP call instead of falling back to an unrelated per-child timeout.
        env["GLESKIT_EVAL_TOOL_TIMEOUT_SECONDS"] = str(max(1, tool_timeout_ms // 1_000))
        env["GLESKIT_WORKSPACE_HOME"] = str(Path(workspace_root).resolve())
        env["GLESKIT_RUNTIME_HOME"] = str(Path(runtime_root).resolve())
        visible_files = list(capsule_bundle.get("prompt_visible_files") or [])
        visible_files.extend(
            str(item.get("path"))
            for item in (capsule_bundle.get("files") or {}).get("visible", [])
            if isinstance(item, dict) and item.get("path")
        )
        allowed_roots = [
            capsule_bundle.get("visible_root"),
            capsule_bundle.get("prompt_visible_root"),
            str(Path(runtime_root).resolve() / "analysis_handoffs"),
            str(Path(workspace_root).resolve() / "derived_matrices"),
        ]
        env["GLESKIT_EVAL_PATH_POLICY"] = "1"
        env["GLESKIT_EVAL_ALLOWED_INPUT_PATHS"] = os.pathsep.join(
            dict.fromkeys(str(Path(path).resolve()) for path in visible_files if path)
        )
        env["GLESKIT_EVAL_ALLOWED_INPUT_ROOTS"] = os.pathsep.join(
            dict.fromkeys(str(Path(path).resolve()) for path in allowed_roots if path)
        )
        env.setdefault("CI", "1")

        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            with _capture_runner_interruptions() as interruption:
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
                        if interruption["signal"] is not None:
                            interruption_signal = interruption["signal"]
                            stop_reason = "interrupted"
                            break
                        returncode = process.poll()
                        if returncode is not None:
                            break
                        elapsed = time.monotonic() - started_at
                        if session_id is None:
                            session_id = find_session_id_by_title(db_path, live_session_title)
                        stats: dict[str, Any] | None = None
                        if session_id is not None:
                            stats = load_opencode_session_stats(db_path, session_id)
                            try:
                                stdout_heartbeat = stdout_path.stat().st_size
                            except OSError:
                                stdout_heartbeat = 0
                            current_signature = (
                                stats["message_count"],
                                stats["assistant_message_count"],
                                stats["part_count"],
                                stats["tool_part_count"],
                                stats.get("active_tool_count", 0),
                                stats["last_message_updated"],
                                stats["last_part_updated"],
                                stdout_heartbeat,
                            )
                            activity_kind = opencode_activity_kind(stats)
                            active_tool = activity_kind == "mcp_tool"
                            pending_provider = activity_kind == "provider"
                            if active_tool:
                                if active_tool_started_at is None:
                                    active_tool_started_at = time.monotonic()
                                pending_provider_started_at = None
                                no_progress = 0
                                if time.monotonic() - active_tool_started_at >= max(
                                    1.0,
                                    float(tool_timeout_ms) / 1000.0,
                                ):
                                    stop_reason = "tool_timeout"
                                    watchdog_triggered = True
                                    break
                            elif pending_provider:
                                active_tool_started_at = None
                                if pending_provider_started_at is None:
                                    pending_provider_started_at = time.monotonic()
                                no_progress = 0
                            elif last_signature is not None and current_signature == last_signature:
                                active_tool_started_at = None
                                pending_provider_started_at = None
                                no_progress += 1
                            else:
                                active_tool_started_at = None
                                pending_provider_started_at = None
                                no_progress = 0
                            last_signature = current_signature
                            if stats["assistant_message_count"] >= config.max_turns:
                                stop_reason = "turn_limit"
                                watchdog_triggered = True
                                break
                            if stats["tool_part_count"] >= config.max_tool_calls:
                                stop_reason = "tool_call_limit"
                                watchdog_triggered = True
                                break
                            if stats["tool_part_count"] != last_checked_tool_part_count:
                                last_checked_tool_part_count = int(stats["tool_part_count"] or 0)
                                try:
                                    live_export = load_opencode_session_export(db_path, session_id)
                                    failure_diagnostic = repeated_nonretryable_tool_error(
                                        build_tool_trace(live_export)
                                    )
                                except Exception:
                                    # OpenCode may be committing its sqlite rows while this
                                    # poll occurs; the final export still performs the same
                                    # classification if this read is temporarily unavailable.
                                    failure_diagnostic = None
                                if failure_diagnostic is not None:
                                    stop_reason = "invalid_input"
                                    watchdog_triggered = True
                                    break
                            if no_progress >= config.no_progress_cycles:
                                stop_reason = "no_progress"
                                watchdog_triggered = True
                                break
                        if elapsed >= config.timeout_seconds:
                            stop_reason = "provider_timeout" if stats and int(stats.get("pending_provider_count") or 0) > 0 else "wall_timeout"
                            watchdog_triggered = True
                            break
                        time.sleep(config.poll_interval_seconds)
                finally:
                    if watchdog_triggered or interruption["signal"] is not None:
                        interruption_signal = interruption["signal"]
                        terminate_process(process)
                    returncode = process.wait()
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if session_id is None:
        session_id = find_session_id_by_title(db_path, live_session_title)

    if stop_reason == "completed" and returncode not in (0, None):
        stop_reason = "opencode_exit_nonzero"
    if session_id is None and stop_reason == "completed":
        stop_reason = "session_missing"
    if watchdog_triggered and stop_reason == "completed":
        stop_reason = "stopped"

    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    runtime_log_dirs = [
        opencode_home / ".local" / "share" / "opencode" / "log",
        opencode_home / ".local" / "state" / "opencode" / "log",
    ]
    runtime_logs = [
        str(path)
        for log_dir in runtime_log_dirs
        for path in _runtime_log_paths(log_dir)
    ]
    runtime_failure = next(
        (
            failure
            for log_dir in runtime_log_dirs
            if (failure := diagnose_opencode_runtime_failure(log_dir)) is not None
        ),
        None,
    )

    stats = load_opencode_session_stats(db_path, session_id) if session_id else {
        "session_exists": False,
        "message_count": 0,
        "assistant_message_count": 0,
        "part_count": 0,
        "tool_part_count": 0,
        "active_tool_count": 0,
        "pending_provider_count": 0,
        "pending_provider_started_at": None,
        "last_message_updated": None,
        "last_part_updated": None,
    }

    session_export: dict[str, Any] | None = None
    transcript: list[dict[str, Any]] = []
    tool_trace: list[dict[str, Any]] = []
    final_answer: dict[str, Any] = {
        "raw_text": "",
        "parsed": False,
        "answer": None,
        "evidence": [],
        "confidence": None,
        "abstain": True,
        "parse_error": "No assistant answer was captured.",
    }
    score: dict[str, Any] = {
        "eval_mode": str(row.get("eval_mode") or ""),
        "ideal": row.get("ideal"),
        "predicted": None,
        "predicted_text": "",
        "abstain": True,
        "score": 0.0,
        "correct": False,
        "match_reason": "missing_session",
        "manual_review_required": str(row.get("eval_mode") or "") == "llm_verifier",
        "answer_present": False,
    }

    if session_id is not None:
        try:
            session_export = load_opencode_session_export(db_path, session_id)
            transcript = build_visible_transcript(session_export)
            tool_trace = build_tool_trace(session_export)
            final_assistant_text = ""
            if session_export:
                assistant_messages = [
                    message
                    for message in session_export.get("messages") or []
                    if str((message.get("data") or {}).get("role") or "") == "assistant"
                ]
                for message in reversed(assistant_messages):
                    texts = []
                    for part in message.get("parts") or []:
                        payload = part.get("data") or {}
                        if payload.get("type") == "text":
                            text = str(payload.get("text") or "").strip()
                            if text:
                                texts.append(text)
                    if texts:
                        final_assistant_text = "\n".join(texts).strip()
                        break
            if final_assistant_text:
                final_answer = parse_final_answer_contract(final_assistant_text)
            score = score_bixbench_answer(row, final_answer)
        except Exception as exc:
            session_export = {
                "session_id": session_id,
                "error": str(exc),
            }
            final_answer = {
                "raw_text": "",
                "parsed": False,
                "answer": None,
                "evidence": [],
                "confidence": None,
                "abstain": True,
                "parse_error": str(exc),
            }

    if runtime_failure and (
        (not final_answer.get("raw_text") and not final_answer.get("answer"))
        or stop_reason in {"completed", "no_progress", "stopped"}
    ):
        stop_reason = str(runtime_failure.get("stop_reason") or stop_reason)

    if failure_diagnostic is None and not final_answer.get("raw_text") and not final_answer.get("answer"):
        failure_diagnostic = repeated_nonretryable_tool_error(tool_trace)
        if failure_diagnostic is not None and stop_reason in {"completed", "no_progress", "tool_call_limit"}:
            stop_reason = "invalid_input"

    attempt_record = {
        "attempt": attempt,
        "session_title": live_session_title,
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": time.monotonic(),
        "duration_seconds": round(time.monotonic() - started_at, 3),
        "effective_tool_timeout_ms": tool_timeout_ms,
        "returncode": returncode,
        "stop_reason": stop_reason,
        "watchdog_triggered": watchdog_triggered,
        "session_found": session_id is not None,
        "session_stats": stats,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "stdout_excerpt": stdout_text[:4000],
        "stderr_excerpt": stderr_text[:4000],
        "runtime_log_dirs": [str(path) for path in runtime_log_dirs],
        "runtime_logs": runtime_logs,
        "runtime_failure": runtime_failure,
        "failure_diagnostic": failure_diagnostic,
        "interruption_signal": interruption_signal,
        "opencode_command": opencode_command,
        "input_payload": input_payload,
        "input_json": str(attempt_root / "input.json"),
        "session_export_path": str(attempt_root / "session_export.json") if session_export is not None else None,
        "transcript": transcript,
        "tool_trace": tool_trace,
        "final_answer": final_answer,
        "score": score,
        "session_export": session_export,
        "retryable": (
            bool(runtime_failure.get("retryable")) if runtime_failure else (
                not watchdog_triggered
                and (returncode not in (0, None) or make_retryable_error(stdout_text) or make_retryable_error(stderr_text))
                and (session_id is None or make_retryable_error(stdout_text) or make_retryable_error(stderr_text))
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
    if attempt_index >= max_retries:
        return False
    if str(attempt_record.get("stop_reason") or "") in {"invalid_input", "no_answer", "interrupted"}:
        return False
    if attempt_record.get("watchdog_triggered"):
        return str(attempt_record.get("stop_reason") or "") in {
            "no_progress",
            "wall_timeout",
            "tool_timeout",
        }
    return bool(attempt_record.get("retryable"))


def _agent_has_canonical_final_json(final_answer: dict[str, Any]) -> bool:
    return bool(final_answer) and bool(final_answer.get("parsed"))


def build_question_result(
    *,
    row: dict[str, Any],
    run_id: str,
    capsule_bundle: dict[str, Any],
    attempt_records: list[dict[str, Any]],
    config: BixBenchRunConfig,
    prompt_text: str,
    episode_root: Path,
) -> dict[str, Any]:
    final_attempt = attempt_records[-1]
    transcript = final_attempt.get("transcript") or []
    tool_trace = final_attempt.get("tool_trace") or []
    agent_final_answer = final_attempt.get("final_answer") or {}
    agent_score_details = score_bixbench_answer(row, agent_final_answer)
    agent_has_canonical_final_json = _agent_has_canonical_final_json(agent_final_answer)
    answer_resolution: dict[str, Any] = {
        "strategy": "agent" if agent_has_canonical_final_json else "abstain",
        "applied": False,
        "agent_final_json_present": agent_has_canonical_final_json,
    }
    # The runner never turns a tool payload or unfinished assistant message
    # into an answer. Preserve the raw agent output for audit, but score only
    # a canonical agent finalization.
    effective_final_answer = agent_final_answer
    if not agent_has_canonical_final_json:
        effective_final_answer = {
            **agent_final_answer,
            "answer": None,
            "abstain": True,
            "parsed": False,
        }
    score = score_bixbench_answer(row, effective_final_answer)
    total_duration = round(sum(float(item.get("duration_seconds") or 0.0) for item in attempt_records), 3)
    final_stop_reason = str(final_attempt.get("stop_reason") or "")
    repeated_error = final_attempt.get("failure_diagnostic")
    if not isinstance(repeated_error, dict):
        repeated_error = None
    if not agent_has_canonical_final_json:
        repeated_error = repeated_error or repeated_nonretryable_tool_error(tool_trace)
    no_captured_answer = str(agent_final_answer.get("parse_error") or "") == "No assistant answer was captured."
    provider_failure_reasons = {
        "provider_connection_refused",
        "provider_timeout",
        "provider_rate_limited",
        "provider_auth_error",
        "provider_model_not_found",
        "provider_model_unavailable",
    }
    if final_stop_reason in provider_failure_reasons:
        final_status = "failed"
    elif final_stop_reason == "interrupted":
        final_status = "stopped"
    elif repeated_error:
        final_stop_reason = "invalid_input"
        final_status = "failed"
    elif final_stop_reason in {"wall_timeout", "tool_timeout", "turn_limit", "tool_call_limit", "no_progress"}:
        final_status = "stopped"
    elif no_captured_answer:
        final_stop_reason = "no_answer"
        final_status = "failed"
    elif final_attempt.get("returncode") == 0 and not final_attempt.get("watchdog_triggered") and final_stop_reason != "session_missing":
        final_status = "completed"
    elif final_stop_reason == "session_missing":
        final_status = "failed"
    elif final_attempt.get("returncode") in (0, None):
        final_status = "stopped"
    else:
        final_status = "failed"
    agent_answer_value = agent_final_answer.get("answer")
    effective_answer_value = effective_final_answer.get("answer")
    question_record = {
        "episode_id": row["question_id"],
        "question_id": row["question_id"],
        "short_id": row.get("short_id"),
        "scope": row.get("scope"),
        "numeric": row.get("numeric"),
        "species": row.get("species"),
        "eval_mode": row.get("eval_mode"),
        "capsule_uuid": row.get("capsule_uuid"),
        "run_id": run_id,
        "skills_mode": normalize_skills_mode(config.skills_mode),
        "session_id": final_attempt.get("session_id"),
        "session_title": final_attempt.get("session_title"),
        "status": final_status,
        "stop_reason": final_stop_reason,
        "turns": int((final_attempt.get("session_stats") or {}).get("assistant_message_count") or 0),
        "tool_calls": int((final_attempt.get("session_stats") or {}).get("tool_part_count") or 0),
        "duration_seconds": float(final_attempt.get("duration_seconds") or 0.0),
        "total_duration_seconds": total_duration,
        "attempt_count": len(attempt_records),
        "retry_count": max(0, len(attempt_records) - 1),
        "question": row.get("question"),
        "ideal": row.get("ideal"),
        "score": float(score.get("score") or 0.0),
        "agent_only_score": float(agent_score_details.get("score") or 0.0),
        "correct": bool(score.get("correct")),
        "manual_review_required": bool(score.get("manual_review_required")),
        "match_reason": score.get("match_reason"),
        "predicted": score.get("predicted"),
        "predicted_text": score.get("predicted_text"),
        "answer_present": bool(score.get("answer_present")),
        "answer_source": answer_resolution["strategy"],
        "agent_answer": agent_answer_value,
        "effective_answer": effective_answer_value,
        "answer_discrepancy": False,
        "answer": effective_answer_value,
        "confidence": effective_final_answer.get("confidence"),
        "abstain": effective_final_answer.get("abstain"),
        "prompt_sha256": sha256_text(prompt_text),
        "bundle_sha256": capsule_bundle.get("bundle_sha256"),
        "session_stats": final_attempt.get("session_stats"),
        "runtime_failure": final_attempt.get("runtime_failure"),
        "failure_diagnostic": repeated_error,
        "agent_score_details": agent_score_details,
        "score_details": score,
        "attempts": [
            {
                "attempt": item.get("attempt"),
                "session_title": item.get("session_title"),
                "session_id": item.get("session_id"),
                "returncode": item.get("returncode"),
                "stop_reason": item.get("stop_reason"),
                "duration_seconds": item.get("duration_seconds"),
                "retryable": item.get("retryable"),
                "stdout_log": item.get("stdout_log"),
                "stderr_log": item.get("stderr_log"),
                "session_export_path": item.get("session_export_path"),
            }
            for item in attempt_records
        ],
        "input": final_attempt.get("input_payload") or {},
        "transcript": transcript,
        "tool_trace": tool_trace,
        "agent_final_answer": agent_final_answer,
        "final_answer": effective_final_answer,
        "answer_resolution": answer_resolution,
        "score_details": score,
    }
    return question_record


def write_question_files(episode_root: Path, question_record: dict[str, Any]) -> None:
    episode_root.mkdir(parents=True, exist_ok=True)
    private_root = episode_root / "private"
    private_root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(episode_root / "input.json", question_record.get("input") or {})
    write_json_atomic(episode_root / "transcript.json", question_record.get("transcript") or [])
    write_json_atomic(episode_root / "tool_calls.json", question_record.get("tool_trace") or [])
    write_json_atomic(episode_root / "agent_final_answer.json", question_record.get("agent_final_answer") or {})
    # Remove stale artifacts from historical runner-side answer derivation.
    for artifact_name in ("runner_suggestion.json", "runner_backfill.json"):
        (episode_root / artifact_name).unlink(missing_ok=True)
    write_json_atomic(episode_root / "final_answer.json", question_record.get("final_answer") or {})
    write_json_atomic(episode_root / "answer_resolution.json", question_record.get("answer_resolution") or {})
    write_json_atomic(episode_root / "score.json", question_record.get("score_details") or {})
    write_json_atomic(episode_root / "episode.json", question_record)


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def _markdown_scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _markdown_cell(value: Any, *, width: int | None = None) -> str:
    text = " ".join(_markdown_scalar(value).split())
    if width is not None:
        text = textwrap.shorten(text, width=width, placeholder="...")
    return text.replace("|", "\\|")


def _markdown_link(label: str, target: str) -> str:
    return f"[{_markdown_cell(label)}]({_markdown_cell(target)})"


def render_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_No data._"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def build_tool_usage_summary(question_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for record in question_records:
        for tool in record.get("tool_trace") or []:
            tool_name = str(tool.get("tool") or "").strip()
            if tool_name:
                counts[tool_name] += 1
    return [
        {"tool": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _judge_value(record: dict[str, Any], key: str, default: Any = None) -> Any:
    if record.get(key) is not None:
        return record.get(key)
    judge = record.get("judge")
    if isinstance(judge, dict) and judge.get(key) is not None:
        return judge.get(key)
    return default


def _judge_score_band(record: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(record.get("judge_score_band"), dict):
        return record.get("judge_score_band")
    judge = record.get("judge")
    if isinstance(judge, dict) and isinstance(judge.get("judge_score_band"), dict):
        return judge.get("judge_score_band")
    score = _judge_value(record, "judge_score")
    return judge_score_band(None if score is None else float(score))


def _committee_category(record: dict[str, Any]) -> dict[str, Any] | None:
    if judge_category(record.get("committee_category")) is not None:
        return judge_category(record.get("committee_category"))
    judge = record.get("judge")
    if isinstance(judge, dict) and judge_category(judge.get("committee_category")) is not None:
        return judge_category(judge.get("committee_category"))
    score_band = _judge_score_band(record)
    if isinstance(score_band, dict):
        return judge_category(score_band.get("key"))
    return None


def _committee_level_mean(record: dict[str, Any]) -> float | None:
    if record.get("committee_level_mean_final") is not None:
        return float(record.get("committee_level_mean_final"))
    judge = record.get("judge")
    if isinstance(judge, dict) and judge.get("committee_level_mean_final") is not None:
        return float(judge.get("committee_level_mean_final"))
    legacy_score = _judge_value(record, "judge_score")
    if legacy_score is None:
        return None
    return float(legacy_score) * 4.0


def _committee_level_mean_normalized(record: dict[str, Any]) -> float | None:
    if record.get("committee_level_mean_normalized") is not None:
        return float(record.get("committee_level_mean_normalized"))
    if record.get("committee_level_mean_final") is not None:
        return float(record.get("committee_level_mean_final")) / 4.0
    judge = record.get("judge")
    if isinstance(judge, dict):
        if judge.get("committee_level_mean_normalized") is not None:
            return float(judge.get("committee_level_mean_normalized"))
        if judge.get("committee_level_mean_final") is not None:
            return float(judge.get("committee_level_mean_final")) / 4.0
    legacy_score = _judge_value(record, "judge_score")
    if legacy_score is None:
        return None
    return float(legacy_score)


def _review_required(record: dict[str, Any]) -> bool:
    if record.get("review_required") is not None:
        return bool(record.get("review_required"))
    judge = record.get("judge")
    if isinstance(judge, dict):
        if judge.get("review_required") is not None:
            return bool(judge.get("review_required"))
        nested = judge.get("judge")
        if isinstance(nested, dict) and nested.get("review_required") is not None:
            return bool(nested.get("review_required"))
    return False


def _review_reasons(record: dict[str, Any]) -> list[str]:
    if isinstance(record.get("review_reasons"), list):
        return [str(item) for item in record.get("review_reasons") if str(item).strip()]
    judge = record.get("judge")
    if isinstance(judge, dict):
        if isinstance(judge.get("review_reasons"), list):
            return [str(item) for item in judge.get("review_reasons") if str(item).strip()]
        nested = judge.get("judge")
        if isinstance(nested, dict) and isinstance(nested.get("review_reasons"), list):
            return [str(item) for item in nested.get("review_reasons") if str(item).strip()]
    return []


def _record_has_answer(record: dict[str, Any]) -> bool:
    if "answer_present" in record:
        return bool(record.get("answer_present"))
    answer = record.get("effective_answer", record.get("answer"))
    return answer is not None and str(answer).strip() != ""


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    items: list[str] = []
    for item in values:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _format_weight_hint(weight_hint: Any) -> str:
    if not isinstance(weight_hint, dict) or not weight_hint:
        return "-"
    ordered_keys = ("stats", "biology", "evidence")
    parts: list[str] = []
    seen: set[str] = set()
    for key in ordered_keys:
        if key not in weight_hint or key in seen:
            continue
        seen.add(key)
        try:
            value_text = f"{float(weight_hint.get(key)):.2f}"
        except Exception:
            value_text = str(weight_hint.get(key))
        parts.append(f"{key}={value_text}")
    for key, value in weight_hint.items():
        if key in seen:
            continue
        seen.add(key)
        try:
            value_text = f"{float(value):.2f}"
        except Exception:
            value_text = str(value)
        parts.append(f"{key}={value_text}")
    return ", ".join(parts) if parts else "-"


def render_html_list_block(label: str, values: Any, *, wide: bool = False, empty_message: str = "None.") -> str:
    classes = "judge-detail wide" if wide else "judge-detail"
    items = _text_list(values)
    if items:
        content = '<ul class="bullet-list">' + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>"
    else:
        content = f'<p class="judge-empty">{html.escape(empty_message)}</p>'
    return (
        f'<div class="{classes}">'
        f'<div class="block-label">{html.escape(label)}</div>'
        f"{content}"
        "</div>"
    )


def render_html_detail_block(label: str, value: Any, *, wide: bool = False) -> str:
    classes = "judge-detail wide" if wide else "judge-detail"
    return (
        f'<div class="{classes}">'
        f'<div class="block-label">{html.escape(label)}</div>'
        f'<div class="full-text">{html.escape(html_scalar(value))}</div>'
        "</div>"
    )


def render_markdown_bullet_block(label: str, values: Any) -> str:
    items = _text_list(values)
    if not items:
        return f"- **{label}:** _None._"
    lines = [f"- **{label}:**"]
    lines.extend(f"  - {_markdown_cell(item)}" for item in items)
    return "\n".join(lines)


def render_markdown_detail_block(label: str, value: Any) -> str:
    return f"- **{label}:** {_markdown_cell(value)}"


def render_judge_specialist_html(title: str, specialist: dict[str, Any], *, list_specs: list[tuple[str, str]]) -> str:
    specialist = specialist if isinstance(specialist, dict) else {}
    list_html = "".join(render_html_list_block(label, specialist.get(key)) for label, key in list_specs)
    category_item = judge_category(specialist.get("category")) or {}
    return (
        '<div class="judge-section">'
        f'<h4>{html.escape(title)}</h4>'
        f'<div class="kpi-row">'
        f'{render_metric_card("Category", category_item.get("label") or specialist.get("category"))}'
        f"</div>"
        f'{render_html_detail_block("Summary", specialist.get("summary"), wide=True)}'
        f'<div class="judge-subgrid">{list_html}</div>'
        "</div>"
    )


def render_judge_final_decision_html(question_record: dict[str, Any]) -> str:
    judge_payload = question_record.get("judge") if isinstance(question_record.get("judge"), dict) else {}
    judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
    category_item = _committee_category(question_record) or {}
    committee_level = _committee_level_mean(question_record)
    if not judge and committee_level is None and not category_item:
        return ""

    return (
        '<div class="judge-section judge-final-decision">'
        "<h4>Arbiter synthesis</h4>"
        '<div class="kpi-row">'
        f'{render_metric_card("Committee category", category_item.get("label") or "n/a")}'
        f'{render_metric_card("Committee level mean (0-4)", committee_level)}'
        f'{render_metric_card("Agreement", _judge_value(question_record, "judge_agreement_with_benchmark"))}'
        f'{render_metric_card("Review required", "yes" if _review_required(question_record) else "no")}'
        "</div>"
        f'{render_html_detail_block("Short review", judge.get("short_review"), wide=True)}'
        f'{render_html_detail_block("Rationale", judge.get("rationale"), wide=True)}'
        "</div>"
    )


def render_judge_tool_provenance_html(entries: Any) -> str:
    items = entries if isinstance(entries, list) else []
    if not items:
        return (
            '<div class="judge-section">'
            "<h4>Tool provenance</h4>"
            '<p class="judge-empty">No tool provenance available.</p>'
            "</div>"
        )

    cards: list[str] = []
    for entry in items:
        entry = entry if isinstance(entry, dict) else {}
        evidence_refs = _text_list(entry.get("evidence_refs"))
        details = [
            render_html_detail_block("Tool", entry.get("tool")),
            render_html_detail_block("Status", entry.get("status")),
            render_html_detail_block("Call ID", entry.get("call_id")),
            render_html_detail_block("Description", entry.get("description"), wide=True),
            render_html_detail_block("Input summary", entry.get("input_summary"), wide=True),
            render_html_detail_block("Output summary", entry.get("output_summary"), wide=True),
        ]
        if entry.get("structured_summary") is not None:
            details.append(
                render_code_block(
                    "Structured summary",
                    json.dumps(entry.get("structured_summary"), indent=2, sort_keys=True, default=str),
                    wide=True,
                )
            )
        if entry.get("structured_result") is not None:
            details.append(
                render_code_block(
                    "Structured result",
                    json.dumps(entry.get("structured_result"), indent=2, sort_keys=True, default=str),
                    wide=True,
                )
            )
        if entry.get("details") is not None:
            details.append(
                render_code_block("Details", json.dumps(entry.get("details"), indent=2, sort_keys=True, default=str), wide=True)
            )
        if entry.get("artifact_path") is not None:
            details.append(render_html_detail_block("Artifact path", entry.get("artifact_path"), wide=True))
        if evidence_refs:
            details.append(render_html_list_block("Evidence refs", evidence_refs, wide=True))
        cards.append(
            "<details class=\"judge-provenance-item\">"
            f"<summary>{html.escape(html_scalar(entry.get('tool')))} · {html.escape(html_scalar(entry.get('status')))} · {html.escape(html_scalar(entry.get('call_id')))}</summary>"
            f"<div class=\"judge-provenance-grid\">{''.join(details)}</div>"
            "</details>"
        )

    return (
        '<div class="judge-section">'
        "<h4>Tool provenance</h4>"
        f'<div class="judge-provenance-list">{"".join(cards)}</div>'
        "</div>"
    )


def render_judge_committee_html(question_record: dict[str, Any]) -> str:
    judge_payload = question_record.get("judge") if isinstance(question_record.get("judge"), dict) else {}
    if not judge_payload and _committee_level_mean(question_record) is None and not _committee_category(question_record):
        return ""
    judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
    category_item = _committee_category(question_record) or {}
    specialists = judge_payload.get("specialists") if isinstance(judge_payload.get("specialists"), dict) else {}
    stats = specialists.get("stats_specialist") if isinstance(specialists.get("stats_specialist"), dict) else {}
    biology = specialists.get("biology_specialist") if isinstance(specialists.get("biology_specialist"), dict) else {}
    evidence = specialists.get("evidence_checker") if isinstance(specialists.get("evidence_checker"), dict) else {}

    overview_html = "".join(
        [
            render_metric_card("Committee category", category_item.get("label") or category_item.get("key")),
            render_metric_card("Committee level mean (0-4)", _committee_level_mean(question_record)),
            render_metric_card("Review required", "yes" if _review_required(question_record) else "no"),
            render_metric_card("Agreement", _judge_value(question_record, "judge_agreement_with_benchmark")),
        ]
    )
    decision_html = "".join(
        [
            render_html_detail_block("Committee category", category_item.get("label") or category_item.get("key")),
            render_html_detail_block("Weight hint", _format_weight_hint(judge_payload.get("weight_hint"))),
            render_html_detail_block("Score policy", judge_payload.get("score_policy") or "legacy"),
            render_html_detail_block("Quality flags", ", ".join(_text_list(judge_payload.get("quality_flags"))) or "-"),
            render_html_detail_block("Specialist disagreement span", judge_payload.get("specialist_disagreement_span")),
            render_html_detail_block("Judge short review", judge.get("short_review"), wide=True),
            render_html_detail_block("Judge rationale", judge.get("rationale"), wide=True),
        ]
    )
    support_html = "".join(
        [
            render_html_list_block("Major strengths", judge.get("major_strengths")),
            render_html_list_block("Major issues", judge.get("major_issues")),
            render_html_list_block("Disagreement flags", judge.get("disagreement_flags")),
            render_html_list_block("Review reasons", _review_reasons(question_record)),
        ]
    )
    specialists_html = "".join(
        [
            render_judge_specialist_html(
                "Statistics specialist",
                stats,
                list_specs=[
                    ("Key observations", "key_observations"),
                    ("Satisfied criteria", "satisfied_criteria"),
                    ("Failed criteria", "failed_criteria"),
                    ("Failure codes", "failure_codes"),
                ],
            ),
            render_judge_specialist_html(
                "Biology specialist",
                biology,
                list_specs=[
                    ("Key observations", "key_observations"),
                    ("Satisfied criteria", "satisfied_criteria"),
                    ("Failed criteria", "failed_criteria"),
                    ("Failure codes", "failure_codes"),
                ],
            ),
            render_judge_specialist_html(
                "Evidence checker",
                evidence,
                list_specs=[
                    ("Key observations", "key_observations"),
                    ("Satisfied criteria", "satisfied_criteria"),
                    ("Failed criteria", "failed_criteria"),
                    ("Failure codes", "failure_codes"),
                    ("Matching evidence", "matching_evidence"),
                    ("Unsupported claims", "unsupported_claims"),
                    ("Evidence gaps", "evidence_gaps"),
                ],
            ),
        ]
    )
    return (
        '<div class="judge-panel">'
        "<h3>Judge Committee</h3>"
        f'<div class="kpi-row">{overview_html}</div>'
        f'{render_judge_final_decision_html(question_record)}'
        f'<div class="judge-overview-grid">{decision_html}{support_html}</div>'
        f'<div class="judge-grid">{specialists_html}</div>'
        f'{render_judge_tool_provenance_html(judge_payload.get("tool_provenance") or question_record.get("judge_tool_provenance") or question_record.get("tool_provenance") or [])}'
        f'<details><summary>Raw judge JSON</summary><pre class="code-block">{html.escape(json.dumps(judge_payload, indent=2, sort_keys=True, default=str))}</pre></details>'
        "</div>"
    )


def render_judge_specialist_markdown(title: str, specialist: dict[str, Any], *, list_specs: list[tuple[str, str]]) -> str:
    specialist = specialist if isinstance(specialist, dict) else {}
    lines = [f"##### {title}"]
    category_item = judge_category(specialist.get("category")) or {}
    lines.append(render_markdown_detail_block("Category", category_item.get("label") or specialist.get("category")))
    lines.append(render_markdown_detail_block("Summary", specialist.get("summary")))
    for label, key in list_specs:
        lines.append(render_markdown_bullet_block(label, specialist.get(key)))
    return "\n".join(lines)


def render_judge_final_decision_markdown(question_record: dict[str, Any]) -> str:
    judge_payload = question_record.get("judge") if isinstance(question_record.get("judge"), dict) else {}
    judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
    category_item = _committee_category(question_record) or {}
    committee_level = _committee_level_mean(question_record)
    if not judge and committee_level is None and not category_item:
        return ""

    return "\n".join(
        [
            "##### Arbiter synthesis",
            render_markdown_detail_block("Committee category", category_item.get("label") or category_item.get("key")),
            render_markdown_detail_block("Committee level mean (0-4)", committee_level),
            render_markdown_detail_block("Agreement", _judge_value(question_record, "judge_agreement_with_benchmark")),
            render_markdown_detail_block("Review required", "yes" if _review_required(question_record) else "no"),
            render_markdown_detail_block("Short review", judge.get("short_review")),
            render_markdown_detail_block("Rationale", judge.get("rationale")),
        ]
    )


def render_judge_tool_provenance_markdown(entries: Any) -> str:
    items = entries if isinstance(entries, list) else []
    lines = ["##### Tool provenance"]
    if not items:
        lines.append("_No tool provenance available._")
        return "\n".join(lines)

    for index, entry in enumerate(items, start=1):
        entry = entry if isinstance(entry, dict) else {}
        lines.append(f"###### Tool call {index}")
        lines.append(render_markdown_detail_block("Tool", entry.get("tool")))
        lines.append(render_markdown_detail_block("Status", entry.get("status")))
        lines.append(render_markdown_detail_block("Call ID", entry.get("call_id")))
        lines.append(render_markdown_detail_block("Description", entry.get("description")))
        lines.append(render_markdown_detail_block("Input summary", entry.get("input_summary")))
        lines.append(render_markdown_detail_block("Output summary", entry.get("output_summary")))
        if entry.get("structured_summary") is not None:
            lines.append(
                render_markdown_detail_block(
                    "Structured summary",
                    json.dumps(entry.get("structured_summary"), indent=2, sort_keys=True, default=str),
                )
            )
        if entry.get("structured_result") is not None:
            lines.append(
                render_markdown_detail_block(
                    "Structured result",
                    json.dumps(entry.get("structured_result"), indent=2, sort_keys=True, default=str),
                )
            )
        if entry.get("details") is not None:
            lines.append(
                render_markdown_detail_block(
                    "Details",
                    json.dumps(entry.get("details"), indent=2, sort_keys=True, default=str),
                )
            )
        if entry.get("artifact_path") is not None:
            lines.append(render_markdown_detail_block("Artifact path", entry.get("artifact_path")))
        evidence_refs = _text_list(entry.get("evidence_refs"))
        if evidence_refs:
            lines.append(render_markdown_bullet_block("Evidence refs", evidence_refs))
    return "\n".join(lines)


def render_judge_committee_markdown(question_record: dict[str, Any]) -> str:
    judge_payload = question_record.get("judge") if isinstance(question_record.get("judge"), dict) else {}
    if not judge_payload and _committee_level_mean(question_record) is None and not _committee_category(question_record):
        return ""
    judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
    category_item = _committee_category(question_record) or {}
    specialists = judge_payload.get("specialists") if isinstance(judge_payload.get("specialists"), dict) else {}
    stats = specialists.get("stats_specialist") if isinstance(specialists.get("stats_specialist"), dict) else {}
    biology = specialists.get("biology_specialist") if isinstance(specialists.get("biology_specialist"), dict) else {}
    evidence = specialists.get("evidence_checker") if isinstance(specialists.get("evidence_checker"), dict) else {}

    sections = [
        "#### Judge Committee",
        render_markdown_detail_block("Committee category", category_item.get("label") or category_item.get("key")),
        render_markdown_detail_block("Committee level mean (0-4)", _committee_level_mean(question_record)),
        render_markdown_detail_block("Judge agreement with benchmark", _judge_value(question_record, "judge_agreement_with_benchmark")),
        render_markdown_detail_block("Weight hint", _format_weight_hint(judge_payload.get("weight_hint"))),
        render_markdown_detail_block("Score policy", judge_payload.get("score_policy") or "legacy"),
        render_markdown_detail_block("Quality flags", ", ".join(_text_list(judge_payload.get("quality_flags"))) or "-"),
        render_markdown_detail_block("Specialist disagreement span", judge_payload.get("specialist_disagreement_span")),
        render_markdown_detail_block("Judge short review", judge.get("short_review")),
        render_markdown_detail_block("Judge rationale", judge.get("rationale")),
        render_markdown_bullet_block("Major strengths", judge.get("major_strengths")),
        render_markdown_bullet_block("Major issues", judge.get("major_issues")),
        render_markdown_bullet_block("Disagreement flags", judge.get("disagreement_flags")),
        render_markdown_bullet_block("Review reasons", _review_reasons(question_record)),
        render_judge_specialist_markdown(
            "Statistics specialist",
            stats,
            list_specs=[
                ("Key observations", "key_observations"),
                ("Satisfied criteria", "satisfied_criteria"),
                ("Failed criteria", "failed_criteria"),
                ("Failure codes", "failure_codes"),
            ],
        ),
        render_judge_specialist_markdown(
            "Biology specialist",
            biology,
            list_specs=[
                ("Key observations", "key_observations"),
                ("Satisfied criteria", "satisfied_criteria"),
                ("Failed criteria", "failed_criteria"),
                ("Failure codes", "failure_codes"),
            ],
        ),
        render_judge_specialist_markdown(
            "Evidence checker",
            evidence,
            list_specs=[
                ("Key observations", "key_observations"),
                ("Satisfied criteria", "satisfied_criteria"),
                ("Failed criteria", "failed_criteria"),
                ("Failure codes", "failure_codes"),
                ("Matching evidence", "matching_evidence"),
                ("Unsupported claims", "unsupported_claims"),
                ("Evidence gaps", "evidence_gaps"),
            ],
        ),
        render_judge_final_decision_markdown(question_record),
        render_judge_tool_provenance_markdown(
            judge_payload.get("tool_provenance")
            or question_record.get("judge_tool_provenance")
            or question_record.get("tool_provenance")
            or []
        ),
    ]
    return "\n\n".join(sections)


def build_judge_summary_rows(question_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in question_records:
        if not _record_has_answer(record):
            continue
        committee_level = _committee_level_mean(record)
        committee_category = _committee_category(record) or {}
        judge_payload = record.get("judge") if isinstance(record.get("judge"), dict) else {}
        if str(judge_payload.get("status") or "").strip().lower() == "error":
            continue
        if (
            committee_level is None
            and record.get("judge") is None
            and record.get("judge_verdict") is None
            and not committee_category
        ):
            continue
        rows.append(
            {
                "question_id": record.get("question_id"),
                "scope": record.get("scope"),
                "numeric": record.get("numeric"),
                "eval_mode": record.get("eval_mode"),
                "benchmark_score": float(record.get("score") or 0.0),
                "benchmark_correct": bool(record.get("correct")),
                "committee_category": committee_category.get("key"),
                "committee_category_label": committee_category.get("label"),
                "committee_level_mean_final": committee_level,
                "committee_level_mean_normalized": _committee_level_mean_normalized(record),
                "judge_score": _judge_value(record, "judge_score"),
                "judge_verdict": committee_category.get("key"),
                "judge_agreement_with_benchmark": _judge_value(record, "judge_agreement_with_benchmark"),
                "judge_short_review": _judge_value(record, "judge_short_review"),
                "judge_rationale": _judge_value(record, "judge_rationale"),
                "judge_disagreement_flags": _judge_value(record, "judge_disagreement_flags"),
                "cap_applied": record.get("cap_applied"),
                "review_required": _review_required(record),
                "review_reasons": _review_reasons(record),
                "judge_committee_reference_score": record.get("committee_level_mean_raw"),
                "judge_weight_hint": _judge_value(record, "weight_hint"),
                "judge_stats_category": _judge_value(record, "judge_stats_category"),
                "judge_biology_category": _judge_value(record, "judge_biology_category"),
                "judge_evidence_category": _judge_value(record, "judge_evidence_category"),
                "tool_provenance_count": len(record.get("tool_provenance") or []),
                "answer_present": True,
            }
        )
    return rows


def build_judge_agreement_rows(judge_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[bool, str], list[dict[str, Any]]] = {}
    for row in judge_rows:
        key = (bool(row.get("benchmark_correct")), str(row.get("committee_category") or ""))
        buckets.setdefault(key, []).append(row)

    rows: list[dict[str, Any]] = []
    for (benchmark_correct, verdict), items in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1])):
        rows.append(
            {
                "benchmark_correct": benchmark_correct,
                "committee_category": verdict,
                "question_count": len(items),
                "mean_committee_level": mean(
                    float(item.get("committee_level_mean_final") or 0.0) for item in items
                ),
                "agreement_rate": round(
                    mean(
                        1.0 if str(item.get("judge_agreement_with_benchmark") or "").strip().lower() == "agree" else 0.0
                        for item in items
                    ),
                    3,
                ),
            }
        )
    return rows


def build_overall_summary(question_records: list[dict[str, Any]]) -> dict[str, Any]:
    answered_records = [record for record in question_records if _record_has_answer(record)]
    unanswered_records = [record for record in question_records if not _record_has_answer(record)]
    scores = [float(record.get("score") or 0.0) for record in answered_records]
    agent_only_scores = [float(record.get("agent_only_score") or 0.0) for record in answered_records]
    committee_levels = [
        float(_committee_level_mean(record))
        for record in answered_records
        if _committee_level_mean(record) is not None
        and str(((record.get("judge") if isinstance(record.get("judge"), dict) else {}) or {}).get("status") or "").strip().lower() != "error"
    ]
    judge_agreements = [
        1.0
        for record in answered_records
        if str(_judge_value(record, "judge_agreement_with_benchmark") or "").strip().lower() == "agree"
        and str(((record.get("judge") if isinstance(record.get("judge"), dict) else {}) or {}).get("status") or "").strip().lower() != "error"
    ]
    numeric_rows = [record for record in question_records if record.get("numeric") is not None]

    def _series_summary(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"count": 0, "mean": None, "std": None}
        return {
            "count": len(values),
            "mean": mean(values),
            "std": pstdev(values) if len(values) > 1 else 0.0,
        }

    official_score_summary = _series_summary(scores)
    committee_level_summary = _series_summary(committee_levels)
    return {
        "question_count": len(question_records),
        "answered_count": len(answered_records),
        "unanswered_count": len(unanswered_records),
        "scored_question_count": official_score_summary["count"],
        "completed_count": sum(1 for record in question_records if record.get("status") == "completed"),
        "stopped_count": sum(1 for record in question_records if record.get("status") == "stopped"),
        "failed_count": sum(1 for record in question_records if record.get("status") == "failed"),
        "accuracy": official_score_summary["mean"],
        "benchmark_score_mean": official_score_summary["mean"],
        "benchmark_score_std": official_score_summary["std"],
        "benchmark_score_count": official_score_summary["count"],
        "agent_only_score_mean": _series_summary(agent_only_scores)["mean"],
        "mean_turns": round(mean(float(record.get("turns") or 0.0) for record in question_records), 3) if question_records else 0.0,
        "mean_tool_calls": round(mean(float(record.get("tool_calls") or 0.0) for record in question_records), 3) if question_records else 0.0,
        "mean_duration_seconds": round(
            mean(float(record.get("duration_seconds") or 0.0) for record in question_records), 3
        )
        if question_records
        else 0.0,
        "mean_total_duration_seconds": round(
            mean(float(record.get("total_duration_seconds") or 0.0) for record in question_records), 3
        )
        if question_records
        else 0.0,
        "manual_review_count": sum(1 for record in answered_records if record.get("manual_review_required")),
        "agent_answer_count": sum(1 for record in question_records if record.get("answer_source") == "agent"),
        "abstain_count": sum(1 for record in question_records if record.get("answer_source") == "abstain"),
        "answer_discrepancy_count": sum(1 for record in question_records if record.get("answer_discrepancy")),
        "numeric_question_count": sum(1 for record in numeric_rows if int(record.get("numeric") or 0) == 1),
        "semantic_question_count": sum(1 for record in numeric_rows if int(record.get("numeric") or 0) == 0),
        "mean_judge_score": committee_level_summary["mean"],
        "committee_level_mean_mean": committee_level_summary["mean"],
        "committee_level_mean_std": committee_level_summary["std"],
        "committee_level_mean_count": committee_level_summary["count"],
        "committee_score_mean": (committee_level_summary["mean"] / 4.0) if committee_level_summary["mean"] is not None else None,
        "committee_score_std": (committee_level_summary["std"] / 4.0) if committee_level_summary["std"] is not None else None,
        "committee_score_count": committee_level_summary["count"],
        "judge_agreement_rate": round(sum(judge_agreements) / len(committee_levels), 3) if committee_levels else None,
    }


def format_duration_human(seconds: Any) -> str:
    try:
        total_seconds = int(round(float(seconds)))
    except Exception:
        return "-"
    if total_seconds < 0:
        return "-"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, secs = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"
    hours, remaining_minutes = divmod(minutes, 60)
    if secs:
        return f"{hours}h {remaining_minutes:02d}m {secs:02d}s"
    if remaining_minutes:
        return f"{hours}h {remaining_minutes:02d}m"
    return f"{hours}h"


def html_scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def html_escape_text(value: Any, *, default: str = "-") -> str:
    text = html_scalar(value)
    if text == "-":
        return default
    return html.escape(text)


def render_pill(label: Any, kind: str = "neutral") -> str:
    return f'<span class="pill {html.escape(kind)}">{html.escape(html_scalar(label))}</span>'


def render_metric_card(label: str, value: Any, *, suffix: str = "") -> str:
    value_text = html_scalar(value)
    if value_text == "-":
        value_text = "n/a"
    if suffix:
        value_text = f"{value_text}{suffix}"
    return (
        '<div class="kpi">'
        f'<span class="label">{html.escape(label)}</span>'
        f'<span class="value">{html.escape(value_text)}</span>'
        "</div>"
    )


def render_text_block(label: str, value: Any, *, wide: bool = False) -> str:
    classes = "block wide" if wide else "block"
    return (
        f'<div class="{classes}">'
        f'<div class="block-label">{html.escape(label)}</div>'
        f'<div class="full-text">{html.escape(html_scalar(value))}</div>'
        "</div>"
    )


def render_code_block(label: str, value: Any, *, wide: bool = False) -> str:
    classes = "block wide" if wide else "block"
    return (
        f'<div class="{classes}">'
        f'<div class="block-label">{html.escape(label)}</div>'
        f'<pre class="code-block">{html.escape(html_scalar(value))}</pre>'
        "</div>"
    )


def render_html_table(headers: list[str], rows: list[list[str]], *, empty_message: str = "No data.") -> str:
    if not rows:
        return f"<p>{html.escape(empty_message)}</p>"
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    row_html = []
    for row in rows:
        row_html.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return (
        "<table>"
        "<thead><tr>"
        f"{header_html}"
        "</tr></thead>"
        "<tbody>"
        f"{''.join(row_html)}"
        "</tbody>"
        "</table>"
    )


def _format_mean_std_text(mean_value: Any, std_value: Any, *, count: Any | None = None) -> str:
    if mean_value is None or std_value is None:
        return "n/a"
    text = f"{float(mean_value):.3f} ± {float(std_value):.3f}"
    if count is not None:
        try:
            count_value = int(count)
        except Exception:
            count_value = None
        if count_value is not None:
            text = f"{text} (n={count_value})"
    return text


def render_score_summary_section(overall: dict[str, Any]) -> str:
    return (
        '<section class="chart score-chart">'
        '<h2>Score Summary</h2>'
        '<p class="meta">The official BixBench binary score is the benchmark metric. The committee layer is reported as a category plus a committee level mean on the 0–4 ordinal ladder. Both aggregates are computed only over episodes that produced a non-empty final answer.</p>'
        f'<div class="kpi-row">{render_metric_card("Official BixBench binary score", _format_mean_std_text(overall.get("benchmark_score_mean"), overall.get("benchmark_score_std"), count=overall.get("benchmark_score_count")))}{render_metric_card("Committee level mean (0-4)", _format_mean_std_text(overall.get("committee_level_mean_mean"), overall.get("committee_level_mean_std"), count=overall.get("committee_level_mean_count")))}{render_metric_card("Judge agreement", overall.get("judge_agreement_rate"))}</div>'
        "</section>"
    )


def _compact_preview(value: Any, *, width: int = 180) -> str:
    text = " ".join(html_scalar(value).split())
    if text == "-":
        return "-"
    return textwrap.shorten(text, width=width, placeholder="…")


def _judge_verdict_kind(verdict: Any) -> str:
    normalized = str(verdict or "").strip().lower()
    if normalized in {"accept", "pass", "supported", "mostly_supported"}:
        return "good"
    if normalized in {"partial", "uncertain", "mixed", "weak"}:
        return "warn"
    if normalized in {"unsupported", "fail", "error"}:
        return "bad"
    return "neutral"


def render_question_overview_card(
    question_record: dict[str, Any],
    *,
    question_href_prefix: str,
) -> str:
    final_answer = question_record.get("final_answer") if isinstance(question_record.get("final_answer"), dict) else {}
    answer_resolution = question_record.get("answer_resolution") if isinstance(question_record.get("answer_resolution"), dict) else {}
    question_id = str(question_record.get("question_id") or "")
    question_href = f"{question_href_prefix}/{html.escape(question_id)}.html"
    status = str(question_record.get("status") or "")
    score = float(question_record.get("score") or 0.0)
    correct = bool(question_record.get("correct"))
    duration_text = format_duration_human(question_record.get("duration_seconds"))
    answer_source = str(question_record.get("answer_source") or "agent")
    answered = _record_has_answer(question_record)
    committee_level = _committee_level_mean(question_record)
    committee_category = _committee_category(question_record) or {}
    committee_key = str((committee_category or {}).get("key") or "").strip()
    committee_label = str((committee_category or {}).get("label") or "").strip()
    status_kind = "good" if status == "completed" and correct else "warn" if status == "completed" else "bad" if status == "failed" else "neutral"
    score_kind = "good" if score >= 1.0 else "bad"
    match_kind = "good" if correct else "bad"
    question_preview = _compact_preview(question_record.get("question"), width=220)
    response_preview = _compact_preview((final_answer or {}).get("raw_text"), width=220)
    meta_pills = [
        render_pill(question_record.get("scope"), "neutral"),
        render_pill("numeric" if int(question_record.get("numeric") or 0) == 1 else "semantic", "neutral"),
        render_pill(question_record.get("eval_mode"), "neutral"),
        render_pill(status or "unknown", status_kind),
        render_pill("answered" if answered else "unanswered", "good" if answered else "warn"),
        render_pill(answer_source, "neutral"),
        render_pill("matched" if correct else "mismatch", match_kind),
        render_pill(duration_text, "neutral"),
        render_pill(f"official {score:.3f}", score_kind),
        render_pill(f"turns {question_record.get('turns')}", "neutral"),
        render_pill(f"tools {question_record.get('tool_calls')}", "neutral"),
        render_pill(f"retries {question_record.get('retry_count')}", "neutral"),
    ]
    if committee_level is not None:
        meta_pills.append(render_pill(f"committee {committee_level:.3f}/4", "neutral"))
    if committee_label:
        meta_pills.append(render_pill(committee_label, judge_band_kind(committee_key)))
    if _review_required(question_record):
        meta_pills.append(render_pill("review required", "warn"))
    if question_record.get("answer_discrepancy"):
        meta_pills.append(render_pill("discrepancy", "warn"))
    return f"""
    <details class="question-accordion">
      <summary>
        <div class="question-summary">
          <div class="question-summary-main">
            <div class="question-summary-top">
              <a class="mono question-link" href="{question_href}">{html.escape(question_id)}</a>
              <div class="question-meta">
                {''.join(meta_pills)}
              </div>
            </div>
            <div class="question-preview">{html.escape(question_preview)}</div>
            <div class="question-preview question-response-preview"><span class="block-label">Returned response preview</span>{html.escape(response_preview)}</div>
          </div>
          <div class="kpi-row">
            {render_metric_card("Official score", f"{score:.3f}")}
            {render_metric_card("Committee category", committee_label or "n/a")}
            {render_metric_card("Committee level mean (0-4)", committee_level if committee_level is not None else "n/a")}
            {render_metric_card("Turns", question_record.get("turns"))}
            {render_metric_card("Tools", question_record.get("tool_calls"))}
            {render_metric_card("Retries", question_record.get("retry_count"))}
          </div>
        </div>
      </summary>
      <div class="question-accordion-body">
        <div class="stack">
          {render_text_block("Question", question_record.get("question"), wide=True)}
          <div class="comparison-grid">
            {render_text_block("Expected answer", question_record.get("ideal"))}
            {render_text_block("Agent answer", question_record.get("agent_answer"))}
            {render_text_block("Effective answer", question_record.get("effective_answer"))}
          </div>
          {render_code_block("Returned response", (final_answer or {}).get("raw_text"), wide=True)}
        </div>
        {render_judge_committee_html(question_record)}
        <div class="question-links">
          <a href="{question_href}">Open full question page</a>
        </div>
      </div>
    </details>
    """


def render_html_summary_report(
    *,
    run_root: Path,
    run_manifest: dict[str, Any],
    summary_bundle: dict[str, Any],
    question_records: list[dict[str, Any]],
    report_title: str = "BixBench Run Report",
    run_manifest_href: str = "../run_manifest.json",
    results_href: str = "../results.json",
    questions_json_href: str = "questions.json",
    portal_root_href: str = "../portal",
) -> str:
    overall = summary_bundle.get("overall_summary") or {}
    question_rows = summary_bundle.get("question_rows") or []
    scope_summary = summary_bundle.get("scope_summary") or []
    numeric_summary = summary_bundle.get("numeric_summary") or []
    eval_mode_summary = summary_bundle.get("eval_mode_summary") or []
    failure_summary = summary_bundle.get("failure_summary") or []
    latency_rows = summary_bundle.get("latency_rows") or []
    judge_rows = summary_bundle.get("judge_rows") or []
    judge_scope_summary = summary_bundle.get("judge_scope_summary") or []
    judge_numeric_summary = summary_bundle.get("judge_numeric_summary") or []
    judge_agreement_rows = summary_bundle.get("judge_agreement_rows") or []

    selection = run_manifest.get("selection") or {}
    opencode = run_manifest.get("opencode") or {}
    dataset = run_manifest.get("dataset") or {}
    created_at = run_manifest.get("created_at") or "-"
    run_id = run_manifest.get("run_id") or run_root.name

    selected_question_ids = selection.get("question_ids") or []
    selected_question_ids_text = ", ".join(str(item) for item in selected_question_ids) if selected_question_ids else "all selected by scope"

    run_info_rows = [
        ("Run ID", html_escape_text(run_id)),
        ("Created at", html_escape_text(created_at)),
        ("Run root", html_escape_text(str(run_root))),
        ("Dataset", html_escape_text(dataset.get("path"))),
        ("Scope", html_escape_text(selection.get("scope"))),
        ("Question IDs", html_escape_text(selected_question_ids_text)),
        ("Limit", html_escape_text(selection.get("limit"))),
        ("Resume", html_escape_text(selection.get("resume"))),
        ("Dry run", html_escape_text(selection.get("dry_run"))),
        ("Planned episodes", html_escape_text(run_manifest.get("planned_episode_count"))),
        ("OpenCode binary", html_escape_text(opencode.get("binary"))),
        ("OpenCode model", html_escape_text(opencode.get("model"))),
        ("Wall timeout", html_escape_text(format_duration_human(opencode.get("timeout_seconds")))),
        ("Poll interval", html_escape_text(format_duration_human(opencode.get("poll_interval_seconds")))),
        ("Tool timeout", html_escape_text(f"{html_scalar(opencode.get('tool_timeout_ms'))} ms")),
    ]

    kpi_cards = [
        render_metric_card("Questions", overall.get("question_count")),
        render_metric_card("Answered", overall.get("answered_count")),
        render_metric_card("Unanswered", overall.get("unanswered_count")),
        render_metric_card(
            "Official BixBench score",
            _format_mean_std_text(
                overall.get("benchmark_score_mean"),
                overall.get("benchmark_score_std"),
                count=overall.get("benchmark_score_count"),
            ),
        ),
        render_metric_card(
            "Committee level mean (0-4)",
            _format_mean_std_text(
                overall.get("committee_level_mean_mean"),
                overall.get("committee_level_mean_std"),
                count=overall.get("committee_level_mean_count"),
            ),
        ),
        render_metric_card("Completed", overall.get("completed_count")),
        render_metric_card("Stopped", overall.get("stopped_count")),
        render_metric_card("Failed", overall.get("failed_count")),
        render_metric_card("Manual review", overall.get("manual_review_count")),
        render_metric_card("Numeric", overall.get("numeric_question_count")),
        render_metric_card("Semantic", overall.get("semantic_question_count")),
        render_metric_card("Mean turns", overall.get("mean_turns")),
        render_metric_card("Mean tool calls", overall.get("mean_tool_calls")),
        render_metric_card("Mean duration", format_duration_human(overall.get("mean_duration_seconds"))),
        render_metric_card("Mean total duration", format_duration_human(overall.get("mean_total_duration_seconds"))),
    ]
    if overall.get("mean_judge_score") is not None:
        kpi_cards.extend(
            [
                render_metric_card("Judge agreement", overall.get("judge_agreement_rate")),
            ]
        )

    scope_rows = [
        [
            html_escape_text(row.get("scope")),
            html_escape_text(row.get("question_count")),
            html_escape_text(row.get("answered_count")),
            html_escape_text(row.get("unanswered_count")),
            html_escape_text(row.get("accuracy")),
            html_escape_text(row.get("manual_review_rate")),
            html_escape_text(row.get("mean_turns")),
            html_escape_text(row.get("mean_tool_calls")),
            html.escape(format_duration_human(row.get("mean_duration_seconds"))),
        ]
        for row in scope_summary
    ]
    eval_mode_rows = [
        [
            html_escape_text(row.get("eval_mode")),
            html_escape_text(row.get("question_count")),
            html_escape_text(row.get("answered_count")),
            html_escape_text(row.get("unanswered_count")),
            html_escape_text(row.get("accuracy")),
            html_escape_text(row.get("manual_review_rate")),
            html_escape_text(row.get("mean_turns")),
            html_escape_text(row.get("mean_tool_calls")),
            html.escape(format_duration_human(row.get("mean_duration_seconds"))),
        ]
        for row in eval_mode_summary
    ]
    failure_rows = [
        [
            render_pill(row.get("stop_reason"), "warn" if row.get("stop_reason") not in {"completed"} else "good"),
            html_escape_text(row.get("count")),
            html_escape_text(row.get("share")),
        ]
        for row in failure_summary
    ]

    question_summary_rows = []
    for row in question_rows:
        status = str(row.get("status") or "")
        score = float(row.get("score") or 0.0)
        committee_level = row.get("committee_level_mean_final")
        committee_category_label = str(row.get("committee_category_label") or "").strip()
        committee_category_key = str(row.get("committee_category") or "").strip()
        judge_review = _compact_preview(row.get("judge_short_review"), width=110)
        status_kind = "good" if status == "completed" and score >= 1.0 else "warn" if status == "completed" else "bad" if status == "failed" else "neutral"
        score_kind = "good" if score >= 1.0 else "bad"
        question_summary_rows.append(
            [
                f'<a href="{html.escape(portal_root_href)}/questions/{html.escape(str(row.get("question_id") or ""))}.html">{html.escape(str(row.get("question_id") or ""))}</a>',
                render_pill(status, status_kind),
                render_pill("answered" if bool(row.get("answer_present")) else "unanswered", "good" if bool(row.get("answer_present")) else "warn"),
                render_pill(committee_category_label, judge_band_kind(committee_category_key)) if committee_category_label else html_escape_text("-"),
                html_escape_text(committee_level),
                render_pill("yes", "warn") if row.get("review_required") else render_pill("no", "neutral"),
                html_escape_text(judge_review),
                render_pill(f"{score:.3f}", score_kind),
                html_escape_text(format_duration_human(row.get("duration_seconds"))),
                html_escape_text(row.get("turns")),
                html_escape_text(row.get("tool_calls")),
                html_escape_text(row.get("retry_count")),
                html_escape_text(row.get("stop_reason")),
            ]
        )

    slowest_rows = sorted(latency_rows, key=lambda item: float(item.get("duration_seconds") or 0.0), reverse=True)[:5]
    slowest_rows_html = [
        [
            f'<a href="{html.escape(portal_root_href)}/questions/{html.escape(str(row.get("question_id") or ""))}.html">{html.escape(str(row.get("question_id") or ""))}</a>',
            html_escape_text(format_duration_human(row.get("duration_seconds"))),
            html_escape_text(row.get("turns")),
            html_escape_text(row.get("tool_calls")),
            render_pill(
                row.get("status") or row.get("stop_reason"),
                "good" if (row.get("status") or row.get("stop_reason")) == "completed" else "warn",
            ),
            html_escape_text(row.get("stop_reason") or row.get("status")),
        ]
        for row in slowest_rows
    ]

    question_cards = []
    for record in question_records:
        final_answer = record.get("final_answer") or {}
        status = str(record.get("status") or "")
        correct = bool(record.get("correct"))
        score = float(record.get("score") or 0.0)
        answered = _record_has_answer(record)
        status_kind = "good" if status == "completed" and correct else "warn" if status == "completed" else "bad" if status == "failed" else "neutral"
        match_kind = "good" if correct else "bad"
        manual_review_kind = "warn" if record.get("manual_review_required") else "neutral"
        time_kind = "neutral"
        question_cards.append(
            f"""
            <article class="question-card">
              <div class="question-head">
                <div>
                  <h3><span class="mono">{html.escape(str(record.get("question_id") or ""))}</span></h3>
                  <div class="question-meta">
                    {render_pill(record.get("scope"), "neutral")}
                    {render_pill("numeric" if int(record.get("numeric") or 0) == 1 else "semantic", "neutral")}
                    {render_pill(record.get("eval_mode"), "neutral")}
                    {render_pill(status, status_kind)}
                    {render_pill("answered" if answered else "unanswered", "good" if answered else "warn")}
                    {render_pill(str(record.get("answer_source") or "agent"), "neutral")}
                    {render_pill("matched" if correct else "mismatch", match_kind)}
                    {render_pill("manual review" if record.get("manual_review_required") else "auto-scored", manual_review_kind)}
                    {render_pill(format_duration_human(record.get("duration_seconds")), time_kind)}
                    {render_pill(f"score {score:.3f}", "neutral")}
                    {render_pill("discrepancy", "warn") if record.get("answer_discrepancy") else ""}
                    {"".join([
                        render_pill(f"committee {_committee_level_mean(record):.3f}/4", "neutral"),
                        render_pill(str((_committee_category(record) or {}).get("label") or (_committee_category(record) or {}).get("key") or ""), judge_band_kind((_committee_category(record) or {}).get("key"))),
                    ]) if _committee_level_mean(record) is not None else ""}
                    {render_pill("review required", "warn") if _review_required(record) else ""}
                  </div>
                </div>
                <div class="kpi-row">
                  {render_metric_card("Official score", f"{score:.3f}")}
                  {render_metric_card("Committee category", (_committee_category(record) or {}).get("label") or "n/a")}
                  {render_metric_card("Committee level mean (0-4)", _committee_level_mean(record) if _committee_level_mean(record) is not None else "n/a")}
                  {render_metric_card("Turns", record.get("turns"))}
                  {render_metric_card("Tool calls", record.get("tool_calls"))}
                  {render_metric_card("Retries", record.get("retry_count"))}
                </div>
              </div>
                {render_text_block("Question", record.get("question"))}
              <div class="comparison-grid">
                {render_text_block("Expected answer", record.get("ideal"))}
                {render_text_block("Agent answer", record.get("agent_answer"), wide=True)}
                {render_text_block("Effective answer", record.get("effective_answer"), wide=True)}
                {render_code_block("Returned response", final_answer.get("raw_text"), wide=True)}
              </div>
                {render_judge_committee_html(record)}
            </article>
            """
        )

    if judge_rows:
        judge_review_section = "\n".join(
            [
                "<section>",
                "<h2>Judge Review</h2>",
                "<div class='grid'>",
                f"<div class='block'><h3>By scope</h3>{render_html_table(['Scope', 'Questions', 'Answered', 'Unanswered', 'Accuracy', 'Manual review rate', 'Mean turns', 'Mean tool calls', 'Mean seconds'], [[html_escape_text(row.get('scope')), html_escape_text(row.get('question_count')), html_escape_text(row.get('answered_count')), html_escape_text(row.get('unanswered_count')), html_escape_text(row.get('accuracy')), html_escape_text(row.get('manual_review_rate')), html_escape_text(row.get('mean_turns')), html_escape_text(row.get('mean_tool_calls')), html.escape(format_duration_human(row.get('mean_duration_seconds')))] for row in judge_scope_summary])}</div>",
                f"<div class='block'><h3>By numeric</h3>{render_html_table(['Numeric', 'Questions', 'Answered', 'Unanswered', 'Accuracy', 'Manual review rate', 'Mean turns', 'Mean tool calls', 'Mean seconds'], [[html_escape_text(row.get('numeric')), html_escape_text(row.get('question_count')), html_escape_text(row.get('answered_count')), html_escape_text(row.get('unanswered_count')), html_escape_text(row.get('accuracy')), html_escape_text(row.get('manual_review_rate')), html_escape_text(row.get('mean_turns')), html_escape_text(row.get('mean_tool_calls')), html.escape(format_duration_human(row.get('mean_duration_seconds')))] for row in judge_numeric_summary])}</div>",
                f"<div class='block'><h3>Agreement</h3>{render_html_table(['Benchmark correct', 'Committee category', 'Questions', 'Mean committee level', 'Agreement rate'], [[html_escape_text(row.get('benchmark_correct')), html_escape_text(row.get('committee_category')), html_escape_text(row.get('question_count')), html_escape_text(row.get('mean_committee_level')), html_escape_text(row.get('agreement_rate'))] for row in judge_agreement_rows])}</div>",
                "</div>",
                "</section>",
            ]
        )
    else:
        judge_review_section = ""

    body = f"""
    <header class="hero">
      <div class="meta">BixBench evaluation run</div>
      <h1>{html.escape(report_title)}</h1>
      <div class="meta">Run ID: <span class="mono">{html.escape(str(run_id))}</span></div>
      <div class="meta">The JSON files remain authoritative. Official BixBench binary score is the benchmark metric; committee category plus committee level mean are the separate judge layer. Aggregate scores below use only answered episodes.</div>
      <div class="question-meta">
        {render_pill("compact summary", "neutral")}
        {render_pill(f"{overall.get('question_count', 0)} questions", "neutral")}
        {render_pill(f"{int(overall.get('answered_count') or 0)} answered", "neutral")}
        {render_pill(f"{int(overall.get('unanswered_count') or 0)} unanswered", "warn" if int(overall.get('unanswered_count') or 0) else "neutral")}
        {render_pill(f"official {html_scalar(overall.get('benchmark_score_mean'))}", "neutral")}
        {render_pill(f"committee {html_scalar(overall.get('committee_level_mean_mean'))}/4", "neutral")}
      </div>
    </header>

    <section class="grid cards">
      {''.join(kpi_cards)}
    </section>

    {render_score_summary_section(overall)}

    <section>
      <h2>Run Info</h2>
      {render_html_table(["Field", "Value"], [[label, value] for label, value in run_info_rows])}
    </section>

    <section>
      <h2>Breakdown</h2>
      <div class="grid">
        <div class="block">
          <h3>By scope</h3>
          {render_html_table(["Scope", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"], scope_rows)}
        </div>
        <div class="block">
          <h3>By numeric</h3>
          {render_html_table(["Numeric", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"], [
            [
              html_escape_text(row.get("numeric")),
              html_escape_text(row.get("question_count")),
              html_escape_text(row.get("answered_count")),
              html_escape_text(row.get("unanswered_count")),
              html_escape_text(row.get("accuracy")),
              html_escape_text(row.get("manual_review_rate")),
              html_escape_text(row.get("mean_turns")),
              html_escape_text(row.get("mean_tool_calls")),
              html.escape(format_duration_human(row.get("mean_duration_seconds"))),
            ]
            for row in numeric_summary
          ])}
        </div>
        <div class="block">
          <h3>By eval mode</h3>
          {render_html_table(["Eval mode", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"], eval_mode_rows)}
        </div>
      </div>
    </section>

    {judge_review_section}

    <section>
      <h2>Failure Modes</h2>
      {render_html_table(["Stop reason", "Count", "Share"], failure_rows)}
    </section>

    <section>
      <h2>Question Snapshot</h2>
      {render_html_table(["Question", "Status", "Answered", "Committee category", "Committee level", "Review required", "Judge review", "Official score", "Time", "Turns", "Tools", "Retries", "Stop reason"], question_summary_rows)}
    </section>

    <section>
      <h2>Question Comparisons</h2>
      <div class="stack">
        {''.join(question_cards) or '<p>No questions were run.</p>'}
      </div>
    </section>

    <section>
      <h2>Slowest Questions</h2>
      {render_html_table(["Question", "Time", "Turns", "Tools", "Status", "Stop reason"], slowest_rows_html)}
    </section>

    <section>
      <h2>Artifacts</h2>
      <ul class="question-list">
        <li><a href="{html.escape(run_manifest_href)}">Run manifest</a></li>
        <li><a href="{html.escape(results_href)}">Results JSON</a></li>
        <li><a href="{html.escape(questions_json_href)}">Questions JSON</a></li>
        <li><a href="{html.escape(portal_root_href)}/index.html">Portal index</a></li>
      </ul>
    </section>
    """
    from gleskit_mcp.eval.bixbench import render_html_page

    return render_html_page(f"BixBench {run_id}", body)


def write_html_summary_report(
    *,
    run_root: Path,
    run_manifest: dict[str, Any],
    summary_bundle: dict[str, Any],
    question_records: list[dict[str, Any]],
    report_path: Path | None = None,
    report_title: str = "BixBench Run Report",
    run_manifest_href: str = "../run_manifest.json",
    results_href: str = "../results.json",
    questions_json_href: str = "questions.json",
    portal_root_href: str = "../portal",
) -> Path:
    report_path = report_path or (run_root / "summary" / "report.html")
    report_text = render_html_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=summary_bundle,
        question_records=question_records,
        report_title=report_title,
        run_manifest_href=run_manifest_href,
        results_href=results_href,
        questions_json_href=questions_json_href,
        portal_root_href=portal_root_href,
    )
    write_text_atomic(report_path, report_text)
    return report_path


def render_markdown_summary_report(
    *,
    run_root: Path,
    run_manifest: dict[str, Any],
    summary_bundle: dict[str, Any],
    report_title: str = "BixBench Run Report",
    run_manifest_href: str = "../run_manifest.json",
    results_href: str = "../results.json",
    questions_json_href: str = "questions.json",
    portal_root_href: str = "../portal",
) -> str:
    overall = summary_bundle.get("overall_summary") or {}
    question_rows = summary_bundle.get("question_rows") or []
    scope_summary = summary_bundle.get("scope_summary") or []
    numeric_summary = summary_bundle.get("numeric_summary") or []
    eval_mode_summary = summary_bundle.get("eval_mode_summary") or []
    failure_summary = summary_bundle.get("failure_summary") or []
    latency_rows = summary_bundle.get("latency_rows") or []
    judge_rows = summary_bundle.get("judge_rows") or []
    judge_scope_summary = summary_bundle.get("judge_scope_summary") or []
    judge_numeric_summary = summary_bundle.get("judge_numeric_summary") or []
    judge_agreement_rows = summary_bundle.get("judge_agreement_rows") or []
    question_records = summary_bundle.get("question_records") or []

    selection = run_manifest.get("selection") or {}
    opencode = run_manifest.get("opencode") or {}
    dataset = run_manifest.get("dataset") or {}
    created_at = run_manifest.get("created_at") or "-"
    run_id = run_manifest.get("run_id") or run_root.name

    selected_question_ids = selection.get("question_ids") or []
    selected_question_ids_text = ", ".join(str(item) for item in selected_question_ids) if selected_question_ids else "all selected by scope"

    run_info_rows = [
        ["Run ID", _markdown_cell(run_id)],
        ["Created at", _markdown_cell(created_at)],
        ["Run root", _markdown_cell(str(run_root))],
        ["Dataset", _markdown_cell(dataset.get("path"))],
        ["Scope", _markdown_cell(selection.get("scope"))],
        ["Question IDs", _markdown_cell(selected_question_ids_text, width=140)],
        ["Limit", _markdown_cell(selection.get("limit"))],
        ["Resume", _markdown_cell(selection.get("resume"))],
        ["Dry run", _markdown_cell(selection.get("dry_run"))],
        ["Planned episodes", _markdown_cell(run_manifest.get("planned_episode_count"))],
        ["OpenCode binary", _markdown_cell(opencode.get("binary"))],
        ["OpenCode model", _markdown_cell(opencode.get("model"))],
        ["Wall timeout (s)", _markdown_cell(opencode.get("timeout_seconds"))],
        ["Poll interval (s)", _markdown_cell(opencode.get("poll_interval_seconds"))],
        ["Tool timeout (ms)", _markdown_cell(opencode.get("tool_timeout_ms"))],
    ]

    kpi_rows = [
        ["Questions", _markdown_cell(overall.get("question_count"))],
        ["Answered", _markdown_cell(overall.get("answered_count"))],
        ["Unanswered", _markdown_cell(overall.get("unanswered_count"))],
        ["Accuracy", _markdown_cell(overall.get("accuracy"))],
        ["Completed", _markdown_cell(overall.get("completed_count"))],
        ["Stopped", _markdown_cell(overall.get("stopped_count"))],
        ["Failed", _markdown_cell(overall.get("failed_count"))],
        ["Manual review", _markdown_cell(overall.get("manual_review_count"))],
        ["Numeric", _markdown_cell(overall.get("numeric_question_count"))],
        ["Semantic", _markdown_cell(overall.get("semantic_question_count"))],
        ["Mean turns", _markdown_cell(overall.get("mean_turns"))],
        ["Mean tool calls", _markdown_cell(overall.get("mean_tool_calls"))],
        ["Mean duration (s)", _markdown_cell(overall.get("mean_duration_seconds"))],
        ["Mean total duration (s)", _markdown_cell(overall.get("mean_total_duration_seconds"))],
    ]
    if overall.get("mean_judge_score") is not None:
        kpi_rows.extend(
            [
                ["Committee level mean (0-4)", _markdown_cell(overall.get("committee_level_mean_mean"))],
                ["Judge agreement", _markdown_cell(overall.get("judge_agreement_rate"))],
            ]
        )

    scope_rows = [
        [
            _markdown_cell(row.get("scope")),
            _markdown_cell(row.get("question_count")),
            _markdown_cell(row.get("answered_count")),
            _markdown_cell(row.get("unanswered_count")),
            _markdown_cell(row.get("accuracy")),
            _markdown_cell(row.get("manual_review_rate")),
            _markdown_cell(row.get("mean_turns")),
            _markdown_cell(row.get("mean_tool_calls")),
            _markdown_cell(row.get("mean_duration_seconds")),
        ]
        for row in scope_summary
    ]
    eval_mode_rows = [
        [
            _markdown_cell(row.get("eval_mode")),
            _markdown_cell(row.get("question_count")),
            _markdown_cell(row.get("answered_count")),
            _markdown_cell(row.get("unanswered_count")),
            _markdown_cell(row.get("accuracy")),
            _markdown_cell(row.get("manual_review_rate")),
            _markdown_cell(row.get("mean_turns")),
            _markdown_cell(row.get("mean_tool_calls")),
            _markdown_cell(row.get("mean_duration_seconds")),
        ]
        for row in eval_mode_summary
    ]
    failure_rows = [
        [
            _markdown_cell(row.get("stop_reason")),
            _markdown_cell(row.get("count")),
            _markdown_cell(row.get("share")),
        ]
        for row in failure_summary
    ]

    question_rows_md = [
        [
            _markdown_link(str(row.get("question_id") or ""), f"{portal_root_href}/questions/{row.get('question_id')}.html"),
            _markdown_cell(row.get("question"), width=120),
            _markdown_cell(row.get("ideal"), width=120),
            _markdown_cell(row.get("answer_source"), width=40),
            _markdown_cell("answered" if row.get("answer_present") else "unanswered"),
            _markdown_cell(row.get("agent_answer"), width=80),
            _markdown_cell(row.get("effective_answer"), width=80),
            _markdown_cell(row.get("correct")),
            _markdown_cell(row.get("score")),
            _markdown_cell(row.get("committee_category_label") or row.get("committee_category")),
            _markdown_cell(row.get("status")),
            _markdown_cell(row.get("stop_reason")),
            _markdown_cell(row.get("turns")),
            _markdown_cell(row.get("tool_calls")),
            _markdown_cell(row.get("duration_seconds")),
            _markdown_cell(row.get("retry_count")),
        ]
        for row in question_rows
    ]

    question_detail_sections: list[str] = []
    for record in question_records:
        judge_block = render_judge_committee_markdown(record)
        if not judge_block:
            continue
        final_answer = record.get("final_answer") or {}
        agent_final_answer = record.get("agent_final_answer") or {}
        answer_resolution = record.get("answer_resolution") or {}
        question_detail_sections.append(
            "\n".join(
                [
                    f"### {_markdown_cell(record.get('question_id'))}",
                    render_markdown_detail_block("Scope", record.get("scope")),
                    render_markdown_detail_block("Numeric", record.get("numeric")),
                    render_markdown_detail_block("Status", record.get("status")),
                    render_markdown_detail_block("Stop reason", record.get("stop_reason")),
                    render_markdown_detail_block("Question", record.get("question")),
                    render_markdown_detail_block("Expected answer", record.get("ideal")),
                    render_markdown_detail_block("Answer source", record.get("answer_source") or answer_resolution.get("strategy")),
                    render_markdown_detail_block("Returned response", final_answer.get("raw_text")),
                    render_markdown_detail_block("Agent returned response", agent_final_answer.get("raw_text")),
                    render_markdown_detail_block("Agent answer", record.get("agent_answer")),
                    render_markdown_detail_block("Effective answer", record.get("effective_answer")),
                    render_markdown_detail_block("Discrepancy flag", record.get("answer_discrepancy")),
                    render_markdown_detail_block("Official score", record.get("score")),
                    render_markdown_detail_block("Agent-only score", record.get("agent_only_score")),
                    render_markdown_detail_block("Match reason", record.get("match_reason")),
                    render_markdown_detail_block("Manual review required", record.get("manual_review_required")),
                    judge_block,
                ]
            )
        )

    if judge_rows:
        judge_review_markdown = "\n".join(
            [
                "## Judge Review",
                "",
                "### By scope",
                "",
                render_markdown_table(
                    ["Scope", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"],
                    [[
                        _markdown_cell(row.get("scope")),
                        _markdown_cell(row.get("question_count")),
                        _markdown_cell(row.get("answered_count")),
                        _markdown_cell(row.get("unanswered_count")),
                        _markdown_cell(row.get("accuracy")),
                        _markdown_cell(row.get("manual_review_rate")),
                        _markdown_cell(row.get("mean_turns")),
                        _markdown_cell(row.get("mean_tool_calls")),
                        _markdown_cell(row.get("mean_duration_seconds")),
                    ] for row in judge_scope_summary],
                ),
                "",
                "### By numeric",
                "",
                render_markdown_table(
                    ["Numeric", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"],
                    [[
                        _markdown_cell(row.get("numeric")),
                        _markdown_cell(row.get("question_count")),
                        _markdown_cell(row.get("answered_count")),
                        _markdown_cell(row.get("unanswered_count")),
                        _markdown_cell(row.get("accuracy")),
                        _markdown_cell(row.get("manual_review_rate")),
                        _markdown_cell(row.get("mean_turns")),
                        _markdown_cell(row.get("mean_tool_calls")),
                        _markdown_cell(row.get("mean_duration_seconds")),
                    ] for row in judge_numeric_summary],
                ),
                "",
                "### Agreement",
                "",
                render_markdown_table(
                    ["Benchmark correct", "Committee category", "Questions", "Mean committee level", "Agreement rate"],
                    [[
                        _markdown_cell(row.get("benchmark_correct")),
                        _markdown_cell(row.get("committee_category")),
                        _markdown_cell(row.get("question_count")),
                        _markdown_cell(row.get("mean_committee_level")),
                        _markdown_cell(row.get("agreement_rate")),
                    ] for row in judge_agreement_rows],
                ),
                "",
                "## Judge Details",
                "",
                "\n\n".join(question_detail_sections) if question_detail_sections else "_No judge details available._",
            ]
        )
    else:
        judge_review_markdown = ""

    slowest_rows = sorted(latency_rows, key=lambda item: float(item.get("duration_seconds") or 0.0), reverse=True)[:5]
    slowest_rows_md = [
        [
            _markdown_link(str(row.get("question_id") or ""), f"{portal_root_href}/questions/{row.get('question_id')}.html"),
            _markdown_cell(row.get("duration_seconds")),
            _markdown_cell(row.get("turns")),
            _markdown_cell(row.get("tool_calls")),
            _markdown_cell(row.get("status")),
            _markdown_cell(row.get("stop_reason")),
        ]
        for row in slowest_rows
    ]

    body = f"""# {report_title}

This is the compact human-facing summary for run `{_markdown_cell(run_id)}`. The JSON files remain the source of truth, but this page is the quickest way to inspect the main KPIs and the expected-vs-returned comparison for each question. Aggregate official scores and committee level means are computed only over episodes that produced a non-empty final answer.

## Run Info

{render_markdown_table(["Field", "Value"], run_info_rows)}

## Main KPIs

{render_markdown_table(["Metric", "Value"], kpi_rows)}

## Breakdown

### By scope

{render_markdown_table(["Scope", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"], scope_rows)}

### By numeric

{render_markdown_table(["Numeric", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"], [
    [
        _markdown_cell(row.get("numeric")),
        _markdown_cell(row.get("question_count")),
        _markdown_cell(row.get("answered_count")),
        _markdown_cell(row.get("unanswered_count")),
        _markdown_cell(row.get("accuracy")),
        _markdown_cell(row.get("manual_review_rate")),
        _markdown_cell(row.get("mean_turns")),
        _markdown_cell(row.get("mean_tool_calls")),
        _markdown_cell(row.get("mean_duration_seconds")),
    ]
    for row in numeric_summary
])}

### By eval mode

{render_markdown_table(["Eval mode", "Questions", "Answered", "Unanswered", "Accuracy", "Manual review rate", "Mean turns", "Mean tool calls", "Mean seconds"], eval_mode_rows)}

### Failure modes

{render_markdown_table(["Stop reason", "Count", "Share"], failure_rows)}

{judge_review_markdown}

## Questions

{render_markdown_table(["Question ID", "Question", "Expected", "Source", "Answered", "Agent answer", "Effective answer", "Match", "Score", "Committee category", "Status", "Stop reason", "Turns", "Tools", "Seconds", "Retries"], question_rows_md)}

## Slowest Questions

{render_markdown_table(["Question ID", "Seconds", "Turns", "Tools", "Status", "Stop reason"], slowest_rows_md)}

## Artifacts

- [Run manifest]({run_manifest_href})
- [Results JSON]({results_href})
- [Questions JSON]({questions_json_href})
- [Portal index]({portal_root_href}/index.html)
"""
    return body.strip() + "\n"


def write_markdown_summary_report(
    *,
    run_root: Path,
    run_manifest: dict[str, Any],
    summary_bundle: dict[str, Any],
    report_path: Path | None = None,
    report_title: str = "BixBench Run Report",
    run_manifest_href: str = "../run_manifest.json",
    results_href: str = "../results.json",
    questions_json_href: str = "questions.json",
    portal_root_href: str = "../portal",
) -> Path:
    report_path = report_path or (run_root / "summary" / "report.md")
    report_text = render_markdown_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=summary_bundle,
        report_title=report_title,
        run_manifest_href=run_manifest_href,
        results_href=results_href,
        questions_json_href=questions_json_href,
        portal_root_href=portal_root_href,
    )
    write_text_atomic(report_path, report_text)
    return report_path


def render_question_page(question_record: dict[str, Any], *, episode_root_href_prefix: str = "../../episodes") -> str:
    input_payload = question_record.get("input") or {}
    transcript = question_record.get("transcript") or []
    tool_trace = question_record.get("tool_trace") or []
    final_answer = question_record.get("final_answer") or {}
    agent_final_answer = question_record.get("agent_final_answer") or {}
    answer_resolution = question_record.get("answer_resolution") or {}
    score_value = question_record.get("score")
    score_details = question_record.get("score_details") or {}
    judge_payload = question_record.get("judge") if isinstance(question_record.get("judge"), dict) else {}
    committee_level = _committee_level_mean(question_record)
    committee_category = _committee_category(question_record) or {}

    transcript_html = []
    for item in transcript:
        role = html.escape(str(item.get("role") or ""))
        text = html.escape(str(item.get("text") or ""))
        transcript_html.append(
            f"<details><summary>{role}</summary><pre>{text}</pre></details>"
        )

    tool_html = []
    for item in tool_trace:
        tool_name = html.escape(str(item.get("tool") or "tool"))
        status = html.escape(str(item.get("status") or "unknown"))
        call_id = html.escape(str(item.get("call_id") or ""))
        input_text = html.escape(json.dumps(item.get("input") or {}, indent=2, sort_keys=True, default=str))
        output_text = html.escape(json.dumps(item.get("output"), indent=2, sort_keys=True, default=str))
        tool_html.append(
            "<details>"
            f"<summary>{tool_name} ({status}) {call_id}</summary>"
            f"<h3>Input</h3><pre>{input_text}</pre>"
            f"<h3>Output</h3><pre>{output_text}</pre>"
            "</details>"
        )

    attempts_html = []
    for item in question_record.get("attempts") or []:
        attempts_html.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('attempt')))}</td>"
            f"<td>{html.escape(str(item.get('session_title')))}</td>"
            f"<td>{html.escape(str(item.get('session_id')))}</td>"
            f"<td>{html.escape(str(item.get('returncode')))}</td>"
            f"<td>{html.escape(str(item.get('stop_reason')))}</td>"
            f"<td>{html.escape(str(item.get('duration_seconds')))}</td>"
            "</tr>"
        )

    artifact_links = [
        ("episode.json", f"{episode_root_href_prefix}/{question_record['question_id']}/episode.json"),
        ("input.json", f"{episode_root_href_prefix}/{question_record['question_id']}/input.json"),
        ("transcript.json", f"{episode_root_href_prefix}/{question_record['question_id']}/transcript.json"),
        ("tool_calls.json", f"{episode_root_href_prefix}/{question_record['question_id']}/tool_calls.json"),
        ("agent_final_answer.json", f"{episode_root_href_prefix}/{question_record['question_id']}/agent_final_answer.json"),
        ("final_answer.json", f"{episode_root_href_prefix}/{question_record['question_id']}/final_answer.json"),
        ("answer_resolution.json", f"{episode_root_href_prefix}/{question_record['question_id']}/answer_resolution.json"),
        ("score.json", f"{episode_root_href_prefix}/{question_record['question_id']}/score.json"),
    ]
    if judge_payload:
        artifact_links.append(("judge.json", f"{episode_root_href_prefix}/{question_record['question_id']}/judge.json"))
    artifact_html = " ".join(
        f'<a href="{html.escape(link)}">{html.escape(label)}</a>' for label, link in artifact_links
    )

    body = f"""
    <header class="hero">
      <div class="meta">BixBench reviewer page</div>
      <h1>{html.escape(str(question_record.get("question_id") or ""))}</h1>
      <div class="meta">Session: <span class="mono">{html.escape(str(question_record.get("session_title") or ""))}</span></div>
      <div class="meta">Artifacts: {artifact_html}</div>
    </header>

    <section>
      <h2>Question</h2>
      <p><strong>Short id:</strong> {html.escape(str(question_record.get("short_id") or ""))}</p>
      <p><strong>Scope:</strong> {html.escape(str(question_record.get("scope") or ""))}</p>
      <p><strong>Numeric:</strong> {html.escape(str(question_record.get("numeric")) if question_record.get("numeric") is not None else "")}</p>
      <p><strong>Eval mode:</strong> {html.escape(str(question_record.get("eval_mode") or ""))}</p>
      <p><strong>Capsule:</strong> {html.escape(str(question_record.get("capsule_uuid") or ""))}</p>
      <p><strong>Benchmark answer:</strong> {html.escape(str(question_record.get("ideal") or ""))}</p>
      <pre>{html.escape(str(question_record.get("question") or ""))}</pre>
    </section>

    <section>
      <h2>Prompt</h2>
      <pre class="code-block">{html.escape(str((input_payload.get("prompt") or {}).get("text") or ""))}</pre>
    </section>

    <section>
      <h2>Outcome</h2>
      <p><strong>Status:</strong> {html.escape(str(question_record.get("status") or ""))}</p>
      <p><strong>Stop reason:</strong> {html.escape(str(question_record.get("stop_reason") or ""))}</p>
      <p><strong>Official BixBench binary score:</strong> {html.escape(f"{float(score_value):.3f}" if score_value is not None else "n/a")}</p>
      <p><strong>Agent-only score:</strong> {html.escape(f"{float(question_record.get('agent_only_score') or 0.0):.3f}")}</p>
      <p><strong>Committee category:</strong> {html.escape(str(committee_category.get("label") or committee_category.get("key") or "n/a"))}</p>
      <p><strong>Committee level mean (0-4):</strong> {html.escape(str(committee_level if committee_level is not None else "n/a"))}</p>
      <p><strong>Match reason:</strong> {html.escape(str(question_record.get("match_reason") or ""))}</p>
      <p><strong>Answer source:</strong> {html.escape(str(question_record.get("answer_source") or answer_resolution.get("strategy") or "agent"))}</p>
      <p><strong>Agent answer:</strong> {html.escape(str(question_record.get("agent_answer") or ""))}</p>
      <p><strong>Effective answer:</strong> {html.escape(str(question_record.get("effective_answer") or ""))}</p>
      <p><strong>Discrepancy flag:</strong> {html.escape(str(bool(question_record.get("answer_discrepancy"))))}</p>
      <p><strong>Abstain:</strong> {html.escape(str((final_answer or {}).get("abstain")))}</p>
      <p><strong>Manual review required:</strong> {html.escape(str(question_record.get("manual_review_required")))}</p>
      {f'<h3>Agent final answer</h3><pre class="code-block">{html.escape(str((agent_final_answer or {}).get("raw_text") or ""))}</pre>' if agent_final_answer and agent_final_answer != final_answer else ''}
      {f'<h3>Runner answer resolution</h3><pre class="code-block">{html.escape(json.dumps(answer_resolution, indent=2, sort_keys=True, default=str))}</pre>' if str(answer_resolution.get("strategy") or "").strip() and str(answer_resolution.get("strategy") or "").strip() != "agent" else ''}
      <h3>Raw final answer text</h3>
      <pre class="code-block">{html.escape(str((final_answer or {}).get("raw_text") or ""))}</pre>
      <h3>Score details</h3>
      <pre class="code-block">{html.escape(json.dumps(score_details, indent=2, sort_keys=True, default=str))}</pre>
    </section>

    {f'''
    <section>
      <h2>Judge</h2>
      {render_judge_committee_html(question_record)}
    </section>
    ''' if judge_payload or _committee_level_mean(question_record) is not None else ""}

    <section>
      <h2>Attempts</h2>
      <table>
        <thead>
          <tr><th>Attempt</th><th>Title</th><th>Session</th><th>Return</th><th>Stop</th><th>Seconds</th></tr>
        </thead>
        <tbody>
          {''.join(attempts_html) or '<tr><td colspan="6">No attempts.</td></tr>'}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Visible Transcript</h2>
      {''.join(transcript_html) or '<p>No visible transcript captured.</p>'}
    </section>

    <section>
      <h2>Tool Trace</h2>
      {''.join(tool_html) or '<p>No tool calls captured.</p>'}
    </section>
    """
    from gleskit_mcp.eval.bixbench import render_html_page

    return render_html_page(f"BixBench {question_record.get('question_id')}", body)


def render_index_page(
    *,
    run_manifest: dict[str, Any],
    question_records: list[dict[str, Any]],
    scope_summary: list[dict[str, Any]],
    eval_mode_summary: list[dict[str, Any]],
    capsule_summary: list[dict[str, Any]],
    tool_usage_summary: list[dict[str, Any]],
    failure_summary: list[dict[str, Any]],
    overall_summary: dict[str, Any],
) -> str:
    run_info_rows = [
        ["Run ID", html_escape_text(run_manifest.get("run_id"))],
        ["Created at", html_escape_text(run_manifest.get("created_at"))],
        ["Dataset", html_escape_text((run_manifest.get("dataset") or {}).get("path"))],
        ["Selected questions", html_escape_text((run_manifest.get("selection") or {}).get("selected_question_count"))],
        ["Scope filter", html_escape_text((run_manifest.get("selection") or {}).get("scope"))],
        ["Model", html_escape_text((run_manifest.get("opencode") or {}).get("model"))],
        ["Agent", html_escape_text((run_manifest.get("opencode") or {}).get("agent"))],
        ["Skills mode", html_escape_text((run_manifest.get("selection") or {}).get("skills_mode") or (run_manifest.get("opencode") or {}).get("skills_mode"))],
        ["Timeout", html.escape(format_duration_human((run_manifest.get("opencode") or {}).get("timeout_seconds")))],
        ["Max turns", html_escape_text((run_manifest.get("opencode") or {}).get("max_turns"))],
        ["Max tool calls", html_escape_text((run_manifest.get("opencode") or {}).get("max_tool_calls"))],
        ["Prompt asset", html_escape_text((run_manifest.get("prompt_asset") or {}).get("path"))],
    ]

    scope_chart = build_portal_svg_bar_chart("Accuracy by scope", scope_summary, value_key="accuracy", label_key="scope")
    eval_mode_chart = build_portal_svg_bar_chart(
        "Accuracy by eval mode", eval_mode_summary, value_key="accuracy", label_key="eval_mode"
    )
    tool_chart = build_portal_svg_bar_chart("Tool usage", tool_usage_summary, value_key="count", label_key="tool")
    failure_chart = build_portal_svg_bar_chart(
        "Failure modes", failure_summary, value_key="count", label_key="stop_reason"
    )
    capsule_rows = []
    for record in capsule_summary:
        capsule_rows.append(
            "<tr>"
            f"<td class=\"mono\">{html.escape(str(record.get('capsule_uuid')))}</td>"
            f"<td>{html.escape(str(record.get('question_count')))}</td>"
            f"<td>{html.escape(str(record.get('answered_count')))}</td>"
            f"<td>{html.escape(str(record.get('unanswered_count')))}</td>"
            f"<td>{html.escape(str(record.get('accuracy')))}</td>"
            f"<td>{html.escape(str(record.get('mean_duration_seconds')))}</td>"
            f"<td>{html.escape(str(record.get('mean_tool_calls')))}</td>"
            "</tr>"
        )

    question_cards = [
        render_question_overview_card(record, question_href_prefix="questions")
        for record in question_records
    ]

    body = f"""
    <header>
      <div class="meta">BixBench evaluation run</div>
      <h1>{html.escape(str(run_manifest.get("run_id") or ""))}</h1>
      <div class="meta">Dataset: {html.escape(str((run_manifest.get("dataset") or {}).get("path") or ""))}</div>
      <div class="meta">Click a question to expand the full question text, returned response, and judge notes. Aggregate scores below use only answered episodes.</div>
    </header>

    <section class="grid cards">
      <div class="card"><span>Questions</span><strong>{overall_summary.get('question_count', 0)}</strong></div>
      <div class="card"><span>Answered</span><strong>{overall_summary.get('answered_count', 0)}</strong></div>
      <div class="card"><span>Unanswered</span><strong>{overall_summary.get('unanswered_count', 0)}</strong></div>
      <div class="card"><span>Official BixBench score</span><strong>{_format_mean_std_text(overall_summary.get('benchmark_score_mean'), overall_summary.get('benchmark_score_std'), count=overall_summary.get('benchmark_score_count'))}</strong></div>
      <div class="card"><span>Committee level mean (0-4)</span><strong>{_format_mean_std_text(overall_summary.get('committee_level_mean_mean'), overall_summary.get('committee_level_mean_std'), count=overall_summary.get('committee_level_mean_count'))}</strong></div>
      <div class="card"><span>Completed</span><strong>{overall_summary.get('completed_count', 0)}</strong></div>
      <div class="card"><span>Stopped</span><strong>{overall_summary.get('stopped_count', 0)}</strong></div>
      <div class="card"><span>Failed</span><strong>{overall_summary.get('failed_count', 0)}</strong></div>
      <div class="card"><span>Mean turns</span><strong>{overall_summary.get('mean_turns', 0.0)}</strong></div>
      <div class="card"><span>Mean tools</span><strong>{overall_summary.get('mean_tool_calls', 0.0)}</strong></div>
      <div class="card"><span>Mean time</span><strong>{html.escape(format_duration_human(overall_summary.get('mean_duration_seconds')))}</strong></div>
      <div class="card"><span>Manual review</span><strong>{overall_summary.get('manual_review_count', 0)}</strong></div>
    </section>

    {render_score_summary_section(overall_summary)}

    <section>
      <h2>Run Info</h2>
      {render_html_table(["Field", "Value"], run_info_rows)}
    </section>

    <section class="grid">
      {scope_chart}
      {eval_mode_chart}
      {tool_chart}
      {failure_chart}
    </section>

    <section>
      <h2>Questions</h2>
      <div class="stack">
        {''.join(question_cards) or '<p>No questions were run.</p>'}
      </div>
    </section>

    <section>
      <h2>Capsules</h2>
      <table>
        <thead>
          <tr><th>Capsule</th><th>Questions</th><th>Answered</th><th>Unanswered</th><th>Accuracy</th><th>Mean seconds</th><th>Mean tools</th></tr>
        </thead>
        <tbody>
          {''.join(capsule_rows) or '<tr><td colspan="7">No capsule summaries.</td></tr>'}
        </tbody>
      </table>
    </section>
    """
    from gleskit_mcp.eval.bixbench import render_html_page

    return render_html_page(f"BixBench {run_manifest.get('run_id')}", body)


def write_portal(
    *,
    run_root: Path,
    run_manifest: dict[str, Any],
    question_records: list[dict[str, Any]],
    scope_summary: list[dict[str, Any]],
    eval_mode_summary: list[dict[str, Any]],
    capsule_summary: list[dict[str, Any]],
    tool_usage_summary: list[dict[str, Any]],
    failure_summary: list[dict[str, Any]],
    overall_summary: dict[str, Any],
    portal_root: Path | None = None,
    episode_root_href_prefix: str = "../../episodes",
) -> None:
    portal_root = portal_root or (run_root / "portal")
    question_root = portal_root / "questions"
    question_root.mkdir(parents=True, exist_ok=True)
    index_html = render_index_page(
        run_manifest=run_manifest,
        question_records=question_records,
        scope_summary=scope_summary,
        eval_mode_summary=eval_mode_summary,
        capsule_summary=capsule_summary,
        tool_usage_summary=tool_usage_summary,
        failure_summary=failure_summary,
        overall_summary=overall_summary,
    )
    write_text_atomic(portal_root / "index.html", index_html)
    for record in question_records:
        question_html = render_question_page(record, episode_root_href_prefix=episode_root_href_prefix)
        write_text_atomic(question_root / f"{record['question_id']}.html", question_html)


def write_summary_artifacts(
    run_root: Path,
    question_records: list[dict[str, Any]],
    summary_root: Path | None = None,
) -> dict[str, Any]:
    summary_root = summary_root or (run_root / "summary")
    summary_root.mkdir(parents=True, exist_ok=True)
    question_rows = make_summary_rows(question_records)
    judge_rows = build_judge_summary_rows(question_records)
    scope_summary = summarize_rows(question_rows, ["scope"])
    numeric_summary = summarize_rows(question_rows, ["numeric"])
    eval_mode_summary = summarize_rows(question_rows, ["eval_mode"])
    capsule_summary = summarize_rows(question_rows, ["capsule_uuid"])
    short_id_summary = summarize_rows(question_rows, ["short_id"])
    failure_summary_rows = build_failure_summary(question_rows)
    tool_usage_summary = build_tool_usage_summary(question_records)
    overall_summary = build_overall_summary(question_records)
    judge_scope_summary = summarize_rows(judge_rows, ["scope"]) if judge_rows else []
    judge_numeric_summary = summarize_rows(judge_rows, ["numeric"]) if judge_rows else []
    judge_agreement_rows = build_judge_agreement_rows(judge_rows) if judge_rows else []
    latency_rows = sorted(
        [
            {
                "question_id": row["question_id"],
                "short_id": row.get("short_id"),
                "scope": row.get("scope"),
                "numeric": row.get("numeric"),
                "eval_mode": row.get("eval_mode"),
                "score": row.get("score"),
                "turns": row.get("turns"),
                "tool_calls": row.get("tool_calls"),
                "duration_seconds": row.get("duration_seconds"),
                "total_duration_seconds": row.get("total_duration_seconds"),
                "stop_reason": row.get("stop_reason"),
            }
            for row in question_rows
        ],
        key=lambda item: float(item.get("duration_seconds") or 0.0),
        reverse=True,
    )

    write_json_atomic(summary_root / "overall.json", overall_summary)
    write_json_atomic(summary_root / "questions.json", question_rows)
    write_csv(summary_root / "questions.csv", question_rows)
    write_json_atomic(summary_root / "scope_summary.json", scope_summary)
    write_csv(summary_root / "scope_summary.csv", scope_summary)
    write_json_atomic(summary_root / "numeric_summary.json", numeric_summary)
    write_csv(summary_root / "numeric_summary.csv", numeric_summary)
    write_json_atomic(summary_root / "eval_mode_summary.json", eval_mode_summary)
    write_csv(summary_root / "eval_mode_summary.csv", eval_mode_summary)
    write_json_atomic(summary_root / "capsule_summary.json", capsule_summary)
    write_csv(summary_root / "capsule_summary.csv", capsule_summary)
    write_json_atomic(summary_root / "short_id_summary.json", short_id_summary)
    write_csv(summary_root / "short_id_summary.csv", short_id_summary)
    write_json_atomic(summary_root / "failure_summary.json", failure_summary_rows)
    write_csv(summary_root / "failure_summary.csv", failure_summary_rows)
    write_json_atomic(summary_root / "tool_usage_summary.json", tool_usage_summary)
    write_csv(summary_root / "tool_usage_summary.csv", tool_usage_summary)
    write_json_atomic(summary_root / "latency_summary.json", latency_rows)
    write_csv(summary_root / "latency_summary.csv", latency_rows)
    if judge_rows:
        write_json_atomic(summary_root / "judge_metrics.json", judge_rows)
        write_csv(summary_root / "judge_metrics.csv", judge_rows)
        write_json_atomic(summary_root / "judge_by_scope.json", judge_scope_summary)
        write_csv(summary_root / "judge_by_scope.csv", judge_scope_summary)
        write_json_atomic(summary_root / "judge_by_numeric.json", judge_numeric_summary)
        write_csv(summary_root / "judge_by_numeric.csv", judge_numeric_summary)
        write_json_atomic(summary_root / "judge_agreement.json", judge_agreement_rows)
        write_csv(summary_root / "judge_agreement.csv", judge_agreement_rows)
    return {
        "summary_root": str(summary_root),
        "question_records": question_records,
        "question_rows": question_rows,
        "judge_rows": judge_rows,
        "scope_summary": scope_summary,
        "numeric_summary": numeric_summary,
        "eval_mode_summary": eval_mode_summary,
        "capsule_summary": capsule_summary,
        "short_id_summary": short_id_summary,
        "failure_summary": failure_summary_rows,
        "tool_usage_summary": tool_usage_summary,
        "latency_rows": latency_rows,
        "overall_summary": overall_summary,
        "judge_scope_summary": judge_scope_summary,
        "judge_numeric_summary": judge_numeric_summary,
        "judge_agreement_rows": judge_agreement_rows,
    }


def run_bixbench_evaluation(project_root: Path, config: BixBenchRunConfig) -> Path:
    dataset_path, dataset_rows = load_bixbench_rows(project_root)
    continue_mode = normalize_continue_mode(config.continue_mode, resume_flag=config.resume)
    run_root = project_root / DEFAULT_BIXBENCH_RUNS_ROOT / config.run_id
    run_root.mkdir(parents=True, exist_ok=True)
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

    existing_manifest = load_existing_manifest(run_root)
    if continue_mode == "fresh" and existing_manifest is not None:
        raise FileExistsError(
            f"Run root already exists: {run_root}. Use --continue-mode resume|append|retry_failed or choose a new run id."
        )

    selected_rows, existing_records, cumulative_question_order = select_rows_for_continue_mode(
        dataset_rows=dataset_rows,
        config=config,
        continue_mode=continue_mode,
        run_root=run_root,
        existing_manifest=existing_manifest,
    )

    question_ids = [str(row.get("question_id") or "").strip() for row in selected_rows]
    if len(question_ids) != len(set(question_ids)):
        raise ValueError("BixBench question ids must be unique for this runner.")

    prompt_template = load_prompt_template(config.skills_mode)
    capsule_groups = group_rows_by_capsule(selected_rows)
    capsule_bundles: dict[str, dict[str, Any]] = {}
    capsule_summaries: list[dict[str, Any]] = []
    for capsule_uuid, capsule_rows in capsule_groups.items():
        representative = capsule_rows[0]
        capsule_bundle = stage_capsule_bundle(project_root=project_root, run_root=run_root, row=representative)
        capsule_bundles[capsule_uuid] = capsule_bundle
        capsule_question_ids = [str(row["question_id"]) for row in capsule_rows]
        capsule_summaries.append(
            build_capsule_summary(
                capsule_uuid=capsule_uuid,
                capsule_bundle=capsule_bundle,
                question_ids=capsule_question_ids,
            )
        )

    tool_schema_snapshot = asyncio.run(snapshot_tool_schemas(project_root, run_root))
    run_manifest = build_manifest_payload(
        project_root=project_root,
        run_root=run_root,
        config=config,
        dataset_path=dataset_path,
        dataset_rows=dataset_rows,
        selected_rows=selected_rows,
        capsule_summaries=capsule_summaries,
        tool_schema_snapshot=tool_schema_snapshot,
        prompt_template=prompt_template,
    )
    run_manifest["created_at"] = str((existing_manifest or {}).get("created_at") or run_manifest.get("created_at"))
    run_manifest["updated_at"] = current_utc_iso()
    run_manifest["planned_episode_count"] = len(cumulative_question_order)
    run_manifest["capsules"] = merge_capsule_summaries(existing_manifest, capsule_summaries)
    run_manifest["selection"] = {
        **build_selection_payload(config),
        "selected_question_count": len(selected_rows),
        "selected_questions": [
            {
                "question_id": row.get("question_id"),
                "short_id": row.get("short_id"),
                "scope": row.get("scope"),
                "numeric": row.get("numeric"),
                "species": row.get("species"),
                "eval_mode": row.get("eval_mode"),
                "capsule_uuid": row.get("capsule_uuid"),
            }
            for row in selected_rows
        ],
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
        "planned_question_count": len(cumulative_question_order),
        "planned_question_ids": list(cumulative_question_order),
        "completed_question_count": sum(
            1 for payload in existing_records.values() if question_has_finalized_answer(payload)
        ),
    }
    run_manifest["eval_environment"] = {
        "workspace_root": str(workspace_root),
        "runtime_root": str(runtime_root),
    }

    existing_planned_entries = {
        str(item.get("question_id") or item.get("episode_id") or "").strip(): item
        for item in load_existing_planned_matrix(run_root)
        if str(item.get("question_id") or item.get("episode_id") or "").strip()
    }
    current_planned_entries: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(selected_rows, start=1):
        capsule_bundle = capsule_bundles[str(row["capsule_uuid"])]
        episode_root = run_root / "episodes" / str(row["question_id"])
        session_title = make_session_title(config.run_id, str(row["question_id"]), next_attempt_number(episode_root))
        prompt_text = build_question_prompt(
            template_text=prompt_template,
            row=row,
            staged_bundle=capsule_bundle,
            run_id=config.run_id,
            question_root=episode_root,
        )
        opencode_command = build_opencode_command(
            config=config,
            project_dir=episode_root,
            session_title=session_title,
            prompt_text=prompt_text,
        )
        current_planned_entries[str(row["question_id"])] = build_planned_matrix(
            run_id=config.run_id,
            episode_root=episode_root,
            row=row,
            capsule_bundle=capsule_bundle,
            prompt_text=prompt_text,
            opencode_command=opencode_command,
            selected_index=index,
            config=config,
        )
    planned_matrix = [
        current_planned_entries.get(question_id)
        or existing_planned_entries.get(question_id)
        for question_id in cumulative_question_order
        if current_planned_entries.get(question_id) or existing_planned_entries.get(question_id)
    ]
    write_json_atomic(run_root / "run_manifest.json", run_manifest)
    write_json_atomic(run_root / "planned_matrix.json", planned_matrix)

    if config.dry_run:
        write_json_atomic(
            run_root / "dry_run.json",
            {
                "run_id": config.run_id,
                "selected_question_count": len(selected_rows),
                "planned_question_count": len(cumulative_question_order),
                "capsule_count": len(capsule_summaries),
                "run_root": str(run_root),
                "continue_mode": continue_mode,
            },
        )
        return run_root

    _validate_selected_runtime_dependencies(project_root, selected_rows)

    question_records_by_id = dict(existing_records)
    interrupted_run: dict[str, Any] | None = None
    for row in selected_rows:
        question_id = str(row["question_id"])
        capsule_bundle = capsule_bundles[str(row["capsule_uuid"])]
        episode_root = run_root / "episodes" / question_id
        episode_root.mkdir(parents=True, exist_ok=True)
        link_episode_capsule_alias(
            episode_root=episode_root,
            capsule_root=str(capsule_bundle["capsule_root"]),
        )
        existing_episode_path = episode_root / "episode.json"
        existing_episode = question_records_by_id.get(question_id)
        if config.resume and existing_episode_path.exists():
            try:
                existing_episode = read_json(existing_episode_path)
            except Exception:
                existing_episode = existing_episode if isinstance(existing_episode, dict) else None
            if isinstance(existing_episode, dict) and question_has_finalized_answer(existing_episode):
                question_records_by_id[question_id] = existing_episode
                continue
        prompt_text = build_question_prompt(
            template_text=prompt_template,
            row=row,
            staged_bundle=capsule_bundle,
            run_id=config.run_id,
            question_root=episode_root,
        )
        attempt_records: list[dict[str, Any]] = []
        first_attempt_number = next_attempt_number(episode_root)
        for retry_index in range(config.max_retries + 1):
            attempt = first_attempt_number + retry_index
            attempt_record = run_opencode_attempt(
                project_root=project_root,
                run_root=run_root,
                workspace_root=workspace_root,
                runtime_root=runtime_root,
                row=row,
                capsule_bundle=capsule_bundle,
                config=config,
                attempt=attempt,
                prompt_text=prompt_text,
            )
            attempt_records.append(attempt_record)
            if not should_retry_attempt(
                attempt_record,
                attempt_index=retry_index,
                max_retries=config.max_retries,
            ):
                break

        final_attempt = attempt_records[-1]
        if final_attempt.get("session_export_path"):
            private_export_path = Path(str(final_attempt["session_export_path"]))
        else:
            private_export_path = episode_root / "private" / "attempts" / f"attempt_{final_attempt['attempt']:02d}" / "session_export.json"
        question_record = build_question_result(
            row=row,
            run_id=config.run_id,
            capsule_bundle=capsule_bundle,
            attempt_records=attempt_records,
            config=config,
            prompt_text=prompt_text,
            episode_root=episode_root,
        )
        question_record = merge_question_attempt_history(existing_episode, question_record)
        question_record["private_session_export_path"] = str(private_export_path) if private_export_path else None
        question_record["session_export"] = None
        question_record["selection"] = build_selection_payload(config)
        write_question_files(episode_root, question_record)
        question_records_by_id[question_id] = question_record
        if final_attempt.get("interruption_signal") is not None:
            interrupted_run = {
                "question_id": question_id,
                "signal": int(final_attempt["interruption_signal"]),
                "recorded_at": current_utc_iso(),
            }
            run_manifest["interruption"] = interrupted_run
            run_manifest["updated_at"] = current_utc_iso()
            write_json_atomic(run_root / "run_manifest.json", run_manifest)
            break
        if _env_flag("BIXBENCH_AUTO_JUDGE"):
            # Keep the live report current by judging the completed slice so far.
            _run_auto_bixbench_judge(
                run_root,
                question_ids=_completed_question_ids(
                    merge_question_records_by_order(cumulative_question_order, question_records_by_id)
                ),
            )

    run_manifest["run_inventory"]["completed_question_count"] = sum(
        1 for payload in question_records_by_id.values() if question_has_finalized_answer(payload)
    )
    write_json_atomic(run_root / "run_manifest.json", run_manifest)
    question_records = merge_question_records_by_order(cumulative_question_order, question_records_by_id)
    summary_bundle = write_summary_artifacts(run_root, question_records)
    html_report_path = write_html_summary_report(
        run_root=run_root,
        run_manifest=run_manifest,
        summary_bundle=summary_bundle,
        question_records=question_records,
    )
    write_portal(
        run_root=run_root,
        run_manifest=run_manifest,
        question_records=question_records,
        scope_summary=summary_bundle["scope_summary"],
        eval_mode_summary=summary_bundle["eval_mode_summary"],
        capsule_summary=summary_bundle["capsule_summary"],
        tool_usage_summary=summary_bundle["tool_usage_summary"],
        failure_summary=summary_bundle["failure_summary"],
        overall_summary=summary_bundle["overall_summary"],
    )
    write_json_atomic(
        run_root / "results.json",
        {
            "run_manifest": run_manifest,
            "summary": summary_bundle["overall_summary"],
            "question_count": len(question_records),
            "questions": make_summary_rows(question_records),
            "summary_report_path": str(html_report_path.relative_to(run_root)),
            "summary_report_format": "html",
        },
    )
    if _env_flag("BIXBENCH_AUTO_JUDGE") and interrupted_run is None:
        _run_auto_bixbench_judge(run_root, question_ids=_completed_question_ids(question_records))
    return run_root


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


def _completed_question_ids(question_records: list[dict[str, Any]]) -> tuple[str, ...]:
    question_ids: list[str] = []
    seen: set[str] = set()
    for record in question_records:
        question_id = str(record.get("question_id") or "").strip()
        if not question_id or question_id in seen:
            continue
        seen.add(question_id)
        question_ids.append(question_id)
    return tuple(question_ids)


def _run_auto_bixbench_judge(run_root: Path, *, question_ids: tuple[str, ...] | None = None) -> None:
    from gleskit_mcp.eval.bixbench_judge import BixBenchJudgeConfig, judge_bixbench_run
    raw_scope = _env_text("BIXBENCH_JUDGE_SCOPE", "all") or "all"
    raw_question_ids = _env_text("BIXBENCH_JUDGE_QUESTION_IDS", "")
    raw_limit = _env_text("BIXBENCH_JUDGE_LIMIT", "0") or "0"
    raw_timeout = _env_text("BIXBENCH_JUDGE_TIMEOUT_SECONDS", "180") or "180"
    raw_provider = _env_text("BIXBENCH_JUDGE_PROVIDER", "openai") or "openai"
    raw_base_url = _env_text("BIXBENCH_JUDGE_BASE_URL", "") or None
    raw_model = _env_text("BIXBENCH_JUDGE_MODEL", DEFAULT_BIXBENCH_MODEL) or DEFAULT_BIXBENCH_MODEL
    try:
        live_question_ids = tuple(question_ids or ())
        if live_question_ids:
            judge_scope = "all"
            judge_question_ids = live_question_ids
            judge_limit = 0
        else:
            judge_scope = raw_scope
            judge_question_ids = tuple(item.strip() for item in raw_question_ids.split(",") if item.strip())
            judge_limit = max(0, int(raw_limit))
        judge_config = BixBenchJudgeConfig(
            run_root=run_root,
            scope=judge_scope,
            question_ids=judge_question_ids,
            limit=judge_limit,
            resume=True,
            timeout_seconds=max(1.0, float(raw_timeout)),
            provider=raw_provider,
            base_url=raw_base_url,
            model=raw_model,
            stats_model=_env_text("BIXBENCH_JUDGE_STATS_MODEL", "") or None,
            biology_model=_env_text("BIXBENCH_JUDGE_BIOLOGY_MODEL", "") or None,
            evidence_model=_env_text("BIXBENCH_JUDGE_EVIDENCE_MODEL", "") or None,
            arbiter_model=_env_text("BIXBENCH_JUDGE_ARBITER_MODEL", "") or None,
            verbose=_env_flag("BIXBENCH_JUDGE_VERBOSE"),
        )
        judge_bixbench_run(judge_config)
    except Exception as exc:
        error_payload = {
            "created_at": current_utc_iso(),
            "run_root": str(run_root),
            "status": "auto_judge_failed",
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "judge_selection": {
                "scope": judge_scope if live_question_ids else raw_scope,
                "question_ids": list(live_question_ids) if live_question_ids else [item.strip() for item in raw_question_ids.split(",") if item.strip()],
                "limit": "0" if live_question_ids else raw_limit,
                "timeout_seconds": raw_timeout,
                "provider": raw_provider,
                "base_url": raw_base_url,
                "model": raw_model,
            },
        }
        try:
            write_json_atomic(run_root / "summary" / "judge" / "auto_judge_error.json", error_payload)
        except Exception:
            pass
        print(f"Auto judge skipped or failed for {run_root}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = resolve_project_root()
    run_id = args.run_id.strip() if str(args.run_id or "").strip() else run_id_from_time("bixbench")
    raw_model = str(args.model or DEFAULT_BIXBENCH_MODEL).strip() or DEFAULT_BIXBENCH_MODEL
    resolved_model, model_note = resolve_opencode_model(raw_model, OPENCODE_CONFIG_PATH)
    if model_note:
        print(model_note, file=sys.stderr)
    config = BixBenchRunConfig(
        run_id=run_id,
        scope=str(args.scope or "1").strip(),
        question_ids=tuple(str(item).strip() for item in (args.question_id or []) if str(item).strip()),
        limit=max(0, int(args.limit or 0)),
        random_count=max(0, int(args.random_count or 0)),
        random_seed=None if args.random_seed is None else int(args.random_seed),
        resume=bool(args.resume),
        continue_mode=normalize_continue_mode(args.continue_mode or "fresh", resume_flag=bool(args.resume)),
        dry_run=bool(args.dry_run),
        agent=str(args.agent or "bixbench-eval").strip() or "bixbench-eval",
        skills_mode=normalize_skills_mode(args.skills_mode or "on"),
        model=resolved_model,
        opencode_bin=str(args.opencode_bin or "opencode").strip() or "opencode",
        timeout_seconds=max(1, int(args.timeout_seconds or 600)),
        max_turns=max(1, int(args.max_turns or 12)),
        max_tool_calls=max(1, int(args.max_tool_calls or 30)),
        no_progress_cycles=max(1, int(args.no_progress_cycles or 3)),
        poll_interval_seconds=max(1, int(args.poll_interval_seconds or 60)),
        opencode_tool_timeout_ms=max(0, int(args.opencode_tool_timeout_ms or 0)),
        max_retries=max(0, int(args.max_retries if args.max_retries is not None else 1)),
        verbose=bool(args.verbose),
    )
    run_root = run_bixbench_evaluation(project_root, config)
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
