from __future__ import annotations

from dataclasses import dataclass
import json
import shutil
from pathlib import Path
from typing import Any, Iterable

from gleskit_mcp.eval.bixbench import (
    collect_file_manifest,
    current_utc_iso,
    read_jsonl,
    sha256_file,
    sha256_text,
    write_json_atomic,
    write_text_atomic,
)
from gleskit_mcp.eval.config import EvalCase, ReportTypeSpec, load_report_type_specs


DEFAULT_REPOBENCH_ROOT = Path("var/repobench")
DEFAULT_REPOBENCH_RUNS_ROOT = DEFAULT_REPOBENCH_ROOT / "runs"
DEFAULT_REPOBENCH_HF_REPO_ROOT = DEFAULT_REPOBENCH_ROOT / "hf_repo"

# These fields describe the real task the agent is allowed to see.  Gold
# criteria remain in the canonical row for post-hoc evaluation only.
AGENT_VISIBLE_TASK_FIELDS = (
    "short_id",
    "question_id",
    "task_id",
    "case_id",
    "scope",
    "report_type",
    "template_id",
    "title",
    "task_prompt",
    "kickoff_context",
    "grounding_requirements",
    "section_grounding_expectations",
    "input_artifacts",
    "species",
    "domain",
    "difficulty",
    "notes",
    "tags",
)


@dataclass(frozen=True)
class RepoBenchSelection:
    scope: str = "1"
    task_ids: tuple[str, ...] = ()
    report_type: str | None = None
    limit: int = 0
    resume: bool = False


def repobench_dataset_path(project_root: Path) -> Path:
    return project_root / DEFAULT_REPOBENCH_HF_REPO_ROOT / "RepoBench.jsonl"


def _default_species(case: EvalCase) -> str:
    kickoff = case.kickoff_context or {}
    for key in ("species", "organism", "taxon"):
        value = kickoff.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return "human"


def _case_domain(spec: ReportTypeSpec) -> str:
    return {
        "biomarker": "translational_biomarker_reporting",
        "mechanism": "mechanism_interpretation_reporting",
        "disease_context": "disease_context_reporting",
    }.get(spec.report_type, "biomedical_reporting")


def _inline_artifacts_for_case(case: EvalCase) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = [
        {
            "artifact_id": "kickoff_context",
            "kind": "inline_json",
            "filename": "kickoff_context.json",
            "payload": case.kickoff_context,
            "description": "Structured kickoff context for the report task.",
        }
    ]
    genes = case.kickoff_context.get("genes") if isinstance(case.kickoff_context, dict) else None
    if isinstance(genes, list) and genes:
        text = "\n".join(str(gene).strip() for gene in genes if str(gene).strip()).strip()
        if text:
            artifacts.append(
                {
                    "artifact_id": "gene_panel",
                    "kind": "inline_text",
                    "filename": "gene_panel.txt",
                    "payload": text + "\n",
                    "description": "Gene panel extracted from kickoff context.",
                }
            )
    return artifacts


def _row_from_case(spec: ReportTypeSpec, case: EvalCase, *, scope: int = 1) -> dict[str, Any]:
    target_question = ""
    if isinstance(case.kickoff_context, dict):
        target_question = str(case.kickoff_context.get("target_question") or "").strip()
    expected_content = case.description.strip()
    if target_question:
        expected_content = f"{expected_content} Primary report question: {target_question}"
    return {
        "task_id": f"{spec.report_type}-{case.case_id}",
        "case_id": case.case_id,
        "scope": scope,
        "report_type": spec.report_type,
        "template_id": spec.template_id,
        "title": case.title,
        "task_prompt": case.task,
        "kickoff_context": case.kickoff_context,
        "expected_content": expected_content,
        "expected_nuggets": list(case.expected_nuggets),
        "forbidden_overclaims": list(case.forbidden_overclaims),
        "grounding_requirements": list(spec.grounding_requirements),
        "section_grounding_expectations": [item.to_dict() for item in spec.section_grounding_expectations],
        "input_artifacts": _inline_artifacts_for_case(case),
        "species": _default_species(case),
        "domain": _case_domain(spec),
        "difficulty": "standard",
        "notes": case.brief,
        "tags": list(case.tags),
    }


def build_repobench_rows_from_specs(specs: dict[str, ReportTypeSpec]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report_type in sorted(specs.keys()):
        spec = specs[report_type]
        for case in spec.cases:
            rows.append(_row_from_case(spec, case))
    return _with_benchmark_ids(rows)


def _with_benchmark_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add BixBench-style public identifiers without replacing internal slugs.

    ``task_id`` remains the descriptive, path-safe identifier used by existing
    runs.  ``short_id`` and ``question_id`` give papers and CLI selections a
    concise, stable-within-dataset-version label such as ``repo-3-q1``.
    """

    normalized_rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows, start=1):
        row = dict(raw_row)
        short_id = str(row.get("short_id") or f"repo-{index}").strip()
        row["short_id"] = short_id
        row["question_id"] = str(row.get("question_id") or f"{short_id}-q1").strip()
        normalized_rows.append(row)
    return normalized_rows


def write_repobench_dataset(project_root: Path) -> Path:
    dataset_path = repobench_dataset_path(project_root)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    rows = build_repobench_rows_from_specs(load_report_type_specs())
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    write_text_atomic(dataset_path, ("\n".join(lines) + "\n") if lines else "")
    return dataset_path


def ensure_repobench_dataset(project_root: Path) -> Path:
    dataset_path = repobench_dataset_path(project_root)
    if dataset_path.exists():
        return dataset_path
    return write_repobench_dataset(project_root)


def load_repobench_rows(project_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    dataset_path = ensure_repobench_dataset(project_root)
    # Older generated datasets predate compact public IDs. Normalize them at
    # load time so existing installations can select by either identifier.
    return dataset_path, _with_benchmark_ids(read_jsonl(dataset_path))


def build_task_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        for identifier_key in ("task_id", "short_id", "question_id"):
            identifier = str(row.get(identifier_key) or "").strip()
            if not identifier:
                continue
            existing = index.get(identifier)
            if existing is not None and existing is not row:
                raise ValueError(f"Duplicate RepoBench identifier: {identifier}")
            index[identifier] = row
    return index


def agent_visible_task_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Return the task data available to a RepoBench solving agent.

    ``expected_content``, ``expected_nuggets``, and
    ``forbidden_overclaims`` are evaluator-only rubric fields.  They are kept
    on the canonical task record for the judge but must never be present in an
    agent-visible prompt, session description, or attempt input file.
    """

    return {
        key: row[key]
        for key in AGENT_VISIBLE_TASK_FIELDS
        if key in row
    }


def select_repobench_rows(
    rows: list[dict[str, Any]],
    *,
    task_ids: Iterable[str] | None = None,
    scope: str = "1",
    report_type: str | None = None,
) -> list[dict[str, Any]]:
    normalized_scope = str(scope or "1").strip().lower()
    normalized_report_type = str(report_type or "").strip().lower()
    requested_ids = [str(item).strip() for item in (task_ids or []) if str(item).strip()]
    index = build_task_index(rows)

    def _matches(row: dict[str, Any]) -> bool:
        if normalized_scope != "all" and str(row.get("scope") or "").strip().lower() != normalized_scope:
            return False
        if normalized_report_type and str(row.get("report_type") or "").strip().lower() != normalized_report_type:
            return False
        return True

    if requested_ids:
        selected: list[dict[str, Any]] = []
        for task_id in requested_ids:
            if task_id not in index:
                available = ", ".join(
                    str(row.get("question_id") or row.get("task_id") or "")
                    for row in rows[:10]
                )
                raise KeyError(f"Unknown RepoBench identifier: {task_id}. Available examples: {available}")
            row = index[task_id]
            if _matches(row):
                selected.append(row)
        return selected

    return [row for row in rows if _matches(row)]


def _write_inline_artifact(path: Path, artifact: dict[str, Any]) -> None:
    kind = str(artifact.get("kind") or "").strip()
    payload = artifact.get("payload")
    if kind == "inline_json":
        write_json_atomic(path, payload)
    elif kind == "inline_text":
        text = str(payload or "")
        write_text_atomic(path, text)
    else:
        raise ValueError(f"Unsupported inline RepoBench artifact kind: {kind}")


def _copy_artifact(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)


def _make_read_only(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _make_read_only(child)
        try:
            path.chmod(0o555)
        except Exception:
            pass
        return
    try:
        path.chmod(0o444)
    except Exception:
        pass


def _remove_staged_input_tree(path: Path) -> None:
    """Remove a prior runner-owned stage, including its intentional read-only files."""

    def _onerror(func: Any, failed_path: str, _exc_info: Any) -> None:
        target = Path(failed_path)
        # Removing a file requires write access on its parent directory. Both
        # paths are inside the current run's staged-input tree.
        for candidate in (target.parent, target):
            try:
                candidate.chmod(0o700 if candidate.is_dir() else 0o600)
            except OSError:
                pass
        func(failed_path)

    shutil.rmtree(path, onerror=_onerror)


def stage_repobench_input_artifacts(
    *,
    project_root: Path,
    run_root: Path,
    row: dict[str, Any],
) -> dict[str, Any]:
    task_id = str(row.get("task_id") or "").strip()
    stage_root = run_root / "staged_inputs" / task_id
    if stage_root.exists():
        _remove_staged_input_tree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)

    staged_artifacts: list[dict[str, Any]] = []
    for artifact in row.get("input_artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        artifact_id = str(artifact.get("artifact_id") or "").strip() or f"artifact_{len(staged_artifacts) + 1}"
        filename = str(artifact.get("filename") or "").strip()
        if not filename:
            raise ValueError(f"RepoBench artifact `{artifact_id}` is missing `filename`.")
        target_path = stage_root / filename
        kind = str(artifact.get("kind") or "").strip()
        if kind in {"inline_json", "inline_text"}:
            _write_inline_artifact(target_path, artifact)
        elif kind == "path":
            source_raw = str(artifact.get("path") or "").strip()
            if not source_raw:
                raise ValueError(f"RepoBench artifact `{artifact_id}` is missing `path`.")
            source_path = Path(source_raw)
            if not source_path.is_absolute():
                source_path = project_root / source_path
            if not source_path.exists():
                raise FileNotFoundError(source_path)
            _copy_artifact(source_path, target_path)
        else:
            raise ValueError(
                f"Unsupported RepoBench artifact kind `{kind}` for task `{task_id}`."
            )
        staged_artifacts.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "filename": filename,
                "description": str(artifact.get("description") or "").strip(),
                "staged_path": str(target_path),
                "size_bytes": target_path.stat().st_size,
                "sha256": sha256_file(target_path),
            }
        )

    _make_read_only(stage_root)
    return {
        "task_id": task_id,
        "stage_root": str(stage_root),
        "bundle_sha256": sha256_text(json.dumps(staged_artifacts, sort_keys=True)),
        "artifacts": staged_artifacts,
        "files": collect_file_manifest(stage_root),
        "created_at": current_utc_iso(),
    }


def build_repobench_prompt(
    *,
    template_text: str,
    row: dict[str, Any],
    spec: ReportTypeSpec,
    staged_inputs: dict[str, Any],
    run_id: str,
    task_root: Path,
    session_id: str,
) -> str:
    artifact_lines = []
    for artifact in staged_inputs.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        description = str(artifact.get("description") or "").strip()
        description_suffix = f" ({description})" if description else ""
        artifact_lines.append(f"- {artifact.get('artifact_id')}: {artifact.get('staged_path')}{description_suffix}")
    if not artifact_lines:
        artifact_lines.append("- No external input artifacts were required for this task.")

    workflow_lines = []
    for index, step in enumerate(spec.workflow_steps, start=1):
        workflow_lines.append(f"{index}. {step.title}: {step.instructions}")
        if step.allowed_tools:
            workflow_lines.append(f"   Allowed tools: {', '.join(step.allowed_tools)}")
        if step.required_tools:
            workflow_lines.append(f"   Required tools: {', '.join(step.required_tools)}")
        if step.required_skill_files:
            workflow_lines.append(f"   Required skill refs: {', '.join(step.required_skill_files)}")

    grounding_lines = []
    for item in row.get("grounding_requirements") or []:
        token = str(item).strip()
        if token:
            grounding_lines.append(f"- {token}")
    if not grounding_lines:
        grounding_lines.append("- No additional grounding requirements were provided.")

    section_grounding_lines = []
    for item in row.get("section_grounding_expectations") or []:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading") or "").strip()
        evidence = str(item.get("evidence_expectation") or "").strip()
        provenance = str(item.get("provenance_expectation") or "").strip()
        interpretation = str(item.get("interpretation_expectation") or "").strip()
        if not heading:
            continue
        section_grounding_lines.append(f"- {heading}")
        if evidence:
            section_grounding_lines.append(f"  evidence: {evidence}")
        if provenance:
            section_grounding_lines.append(f"  provenance: {provenance}")
        if interpretation:
            section_grounding_lines.append(f"  interpretation: {interpretation}")
    if not section_grounding_lines:
        section_grounding_lines.append("- No section-specific grounding expectations were provided.")

    replacements = {
        "{{RUN_ID}}": run_id,
        "{{TASK_ID}}": str(row.get("task_id") or ""),
        "{{REPORT_TYPE}}": str(row.get("report_type") or ""),
        "{{TEMPLATE_ID}}": str(row.get("template_id") or spec.template_id),
        "{{TITLE}}": str(row.get("title") or ""),
        "{{TASK_PROMPT}}": str(row.get("task_prompt") or ""),
        "{{GROUNDING_REQUIREMENTS}}": "\n".join(grounding_lines),
        "{{SECTION_GROUNDING_EXPECTATIONS}}": "\n".join(section_grounding_lines),
        "{{KICKOFF_CONTEXT}}": json.dumps(row.get("kickoff_context") or {}, indent=2, sort_keys=True),
        "{{SESSION_ID}}": session_id,
        "{{STAGED_INPUT_ROOT}}": str(staged_inputs.get("stage_root") or ""),
        "{{STAGED_INPUTS}}": "\n".join(artifact_lines),
        "{{WORKFLOW_STEPS}}": "\n".join(workflow_lines),
    }
    prompt = str(template_text or "")
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    return prompt.strip()
