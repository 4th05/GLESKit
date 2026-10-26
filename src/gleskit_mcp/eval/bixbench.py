from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import textwrap
import time
import uuid
import zipfile
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import pandas as pd

from gleskit_mcp.analysis.expression_matrix import ExpressionMatrixAnalysisBackend
from gleskit_mcp.eval.judge_bands import judge_score_band
from gleskit_mcp.tabular import is_excel_workbook
from gleskit_mcp.utils import clean_text


DEFAULT_BIXBENCH_ROOT = Path("var/bixbench")
DEFAULT_BIXBENCH_RUNS_ROOT = DEFAULT_BIXBENCH_ROOT / "runs"
DEFAULT_BIXBENCH_CACHE_ROOT = DEFAULT_BIXBENCH_ROOT / "cache" / "capsules"
DEFAULT_BIXBENCH_EXTRACTED_ROOT = DEFAULT_BIXBENCH_ROOT / "extracted"
DEFAULT_BIXBENCH_HF_REPO_ROOT = DEFAULT_BIXBENCH_ROOT / "hf_repo"
DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


@dataclass(frozen=True)
class BixBenchSelection:
    scope: str = "1"
    question_ids: tuple[str, ...] = ()
    limit: int = 0
    resume: bool = False


def current_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def write_json_atomic(path: Path, payload: Any) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    write_json(tmp_path, payload)
    tmp_path.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def dataset_candidates(project_root: Path) -> list[Path]:
    root = project_root / DEFAULT_BIXBENCH_HF_REPO_ROOT
    return [
        root / "BixBench.gleskit_scope.jsonl",
        root / "BixBench.jsonl",
    ]


def resolve_bixbench_dataset_path(project_root: Path) -> Path:
    for candidate in dataset_candidates(project_root):
        if candidate.exists():
            return candidate
    expected = ", ".join(str(path) for path in dataset_candidates(project_root))
    raise FileNotFoundError(f"Could not find a BixBench JSONL file. Expected one of: {expected}")


def load_bixbench_rows(project_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    dataset_path = resolve_bixbench_dataset_path(project_root)
    return dataset_path, read_jsonl(dataset_path)


def build_question_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        question_id = str(row.get("question_id") or "").strip()
        if not question_id:
            continue
        index[question_id] = row
    return index


def select_bixbench_rows(
    rows: list[dict[str, Any]],
    *,
    question_ids: Iterable[str] | None = None,
    scope: str = "1",
) -> list[dict[str, Any]]:
    normalized_scope = str(scope or "1").strip().lower()
    requested_ids = [str(item).strip() for item in (question_ids or []) if str(item).strip()]
    index = build_question_index(rows)

    if requested_ids:
        selected: list[dict[str, Any]] = []
        for question_id in requested_ids:
            if question_id not in index:
                available = ", ".join(sorted(index.keys())[:10])
                raise KeyError(f"Unknown BixBench question_id: {question_id}. Available examples: {available}")
            row = index[question_id]
            if normalized_scope != "all" and str(row.get("scope", "")).strip().lower() != normalized_scope:
                continue
            selected.append(row)
        return selected

    selected = []
    for row in rows:
        if normalized_scope == "all" or str(row.get("scope", "")).strip().lower() == normalized_scope:
            selected.append(row)
    return selected


_NUMERIC_INTERVAL_RE = re.compile(
    r"^\s*[\(\[]\s*[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*[\)\]]\s*$"
)
_NUMERIC_RANGE_RE = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?(?:\s*(?:%|percent|percentage))?(?:\b.*)?$")
_NUMERIC_SCIENTIFIC_RE = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?\s*[x×]\s*10\^[+-]?\d+\s*$", re.IGNORECASE)
_NUMERIC_FOLD_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:-fold|fold(?: change)?|times)\b", re.IGNORECASE)
_NUMERIC_RATIO_RE = re.compile(r"\b\d+(?:\.\d+)?x\b", re.IGNORECASE)
_NUMERIC_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|percentage)\b", re.IGNORECASE)
_NUMERIC_CONTEXT_RE = re.compile(
    r"\b(?:odds ratio|p[- ]?value|padj|adjusted p[- ]?value|variance|median|mean|iqr|"
    r"skewness|statistic|score|change|increase|decrease|ratio|f[- ]?statistic|u statistic|"
    r"chi[- ]?square|log2 fold change|log2fc|lfc|vaf|treeness|dvmc)\b",
    re.IGNORECASE,
)


def _ideal_is_numeric(ideal: str) -> bool:
    normalized = str(ideal or "").strip()
    if not normalized:
        return False
    if normalized.startswith("(") or normalized.startswith("["):
        if _NUMERIC_INTERVAL_RE.match(normalized):
            return True
    if _to_decimal(normalized) is not None:
        return True
    if _NUMERIC_SCIENTIFIC_RE.match(normalized):
        return True
    if _NUMERIC_RANGE_RE.match(normalized):
        return True
    if _NUMERIC_FOLD_RE.search(normalized):
        return True
    if _NUMERIC_RATIO_RE.search(normalized):
        return True
    if _NUMERIC_PERCENT_RE.search(normalized):
        return True
    if re.search(r"\d", normalized) and _NUMERIC_CONTEXT_RE.search(normalized):
        return True
    return False


def numeric_for_row(row: dict[str, Any]) -> int:
    return int(_ideal_is_numeric(str(row.get("ideal") or "")))


def run_id_from_time(prefix: str = "bixbench") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / entry.name
        if entry.is_dir():
            _copy_tree(entry, target)
        else:
            shutil.copy2(entry, target)


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


def _collect_file_manifest(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rows.append(
            {
                "path": str(path),
                "relative_path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return rows


def collect_file_manifest(root: Path) -> list[dict[str, Any]]:
    return _collect_file_manifest(root)


def _link_or_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
        return
    except Exception:
        pass
    shutil.copy2(source, target)


def _create_flat_file_aliases(source_root: Path, alias_root: Path) -> list[dict[str, str]]:
    files = [path for path in sorted(source_root.rglob("*")) if path.is_file()]
    basename_counts = Counter(path.name for path in files)
    aliases: list[dict[str, str]] = []
    for source_path in files:
        if basename_counts[source_path.name] != 1:
            continue
        alias_path = alias_root / source_path.name
        _link_or_copy_file(source_path, alias_path)
        aliases.append(
            {
                "alias_path": str(alias_path),
                "source_path": str(source_path),
                "relative_source_path": source_path.relative_to(source_root).as_posix(),
            }
        )
    return aliases


def _extract_zip_to_cache(zip_path: Path, cache_root: Path) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    marker = cache_root / ".extracted"
    if marker.exists():
        return cache_root
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(cache_root)
    marker.write_text(current_utc_iso(), encoding="utf-8")
    return cache_root


def _find_capsule_subdir(root: Path, prefix: str) -> Path | None:
    matches = [path for path in root.iterdir() if path.is_dir() and path.name.startswith(prefix)]
    if not matches:
        return None
    matches.sort(key=lambda item: item.name)
    return matches[0]


def resolve_question_source_root(project_root: Path, row: dict[str, Any]) -> Path:
    question_id = str(row["question_id"])
    extracted_root = project_root / DEFAULT_BIXBENCH_EXTRACTED_ROOT / question_id
    if extracted_root.exists():
        return extracted_root

    capsule_uuid = str(row["capsule_uuid"])
    zip_name = str(row["data_folder"])
    zip_path = project_root / DEFAULT_BIXBENCH_HF_REPO_ROOT / zip_name
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)

    cache_root = project_root / DEFAULT_BIXBENCH_CACHE_ROOT / capsule_uuid
    return _extract_zip_to_cache(zip_path, cache_root)


def stage_capsule_bundle(
    *,
    project_root: Path,
    run_root: Path,
    row: dict[str, Any],
) -> dict[str, Any]:
    capsule_uuid = str(row["capsule_uuid"])
    capsule_root = run_root / "capsules" / capsule_uuid
    manifest_path = capsule_root / "bundle_manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        manifest["capsule_root"] = str(capsule_root)
        return manifest

    ensure_clean_dir(capsule_root)
    visible_root = capsule_root / "visible"
    audit_root = capsule_root / "audit"
    visible_root.mkdir(parents=True, exist_ok=True)
    audit_root.mkdir(parents=True, exist_ok=True)

    source_root = resolve_question_source_root(project_root, row)
    visible_source = _find_capsule_subdir(source_root, "CapsuleData-")
    if visible_source is None:
        raise FileNotFoundError(f"Could not find a CapsuleData directory under {source_root}")
    audit_source = _find_capsule_subdir(source_root, "CapsuleNotebook-")

    visible_stage_root = visible_root / visible_source.name
    _copy_tree(visible_source, visible_stage_root)
    visible_aliases = _create_flat_file_aliases(visible_stage_root, visible_root)
    _make_read_only(visible_stage_root)
    for alias in visible_aliases:
        _make_read_only(Path(alias["alias_path"]))

    audit_stage_root: Path | None = None
    if audit_source is not None:
        audit_stage_root = audit_root / audit_source.name
        _copy_tree(audit_source, audit_stage_root)
        _make_read_only(audit_stage_root)

    files_manifest = {
        "visible": _collect_file_manifest(visible_stage_root),
        "audit": _collect_file_manifest(audit_stage_root) if audit_stage_root is not None else [],
    }
    bundle_manifest = {
        "capsule_uuid": capsule_uuid,
        "capsule_root": str(capsule_root),
        "visible_root": str(visible_stage_root),
        "prompt_visible_root": str(visible_root),
        "prompt_visible_files": [item["alias_path"] for item in visible_aliases] or [item["path"] for item in files_manifest["visible"]],
        "audit_root": str(audit_stage_root) if audit_stage_root is not None else None,
        "source_root": str(source_root),
        "files": files_manifest,
        "visible_aliases": visible_aliases,
        "bundle_sha256": sha256_text(json.dumps(files_manifest, sort_keys=True, default=str)),
    }
    write_json_atomic(manifest_path, bundle_manifest)
    _make_read_only(capsule_root)
    return bundle_manifest


def stage_question_bundle(
    *,
    project_root: Path,
    run_root: Path,
    row: dict[str, Any],
) -> dict[str, Any]:
    return stage_capsule_bundle(project_root=project_root, run_root=run_root, row=row)


def file_list_for_prompt(paths: Iterable[str]) -> str:
    entries = [str(item).strip() for item in paths if str(item).strip()]
    return "\n".join(f"- {item}" for item in entries) if entries else "- none"


def _preview_header_tokens(path: Path, *, max_tokens: int = 6) -> list[str]:
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except Exception:
        return []
    if not first_line:
        return []
    if "\t" in first_line:
        raw_tokens = [token.strip() for token in first_line.split("\t")]
    elif "," in first_line:
        try:
            raw_tokens = [token.strip() for token in next(csv.reader([first_line]))]
        except Exception:
            raw_tokens = [token.strip() for token in first_line.split(",")]
    else:
        raw_tokens = re.split(r"\s+", first_line)
    raw_tokens = [token.strip() for token in raw_tokens]
    tokens = list(raw_tokens)
    if tokens and not tokens[0]:
        tokens[0] = "<blank gene_id column>"
    tokens = [token for token in tokens if token]
    return tokens[:max_tokens]


def _format_preview_tokens(tokens: list[str], *, total: int | None = None) -> str:
    if not tokens:
        return "[]"
    preview = ", ".join(tokens)
    if total is not None and total > len(tokens):
        return f"[{preview}, ... (+{total - len(tokens)} more)]"
    return f"[{preview}]"


_METADATA_HINT_TOKENS = (
    "sample",
    "condition",
    "group",
    "treatment",
    "strain",
    "patient",
    "class",
    "phenotype",
    "status",
    "celltype",
    "cell_type",
)


def _column_unique_values(frame: pd.DataFrame, column_name: str, *, max_values: int = 8) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw_value in frame[column_name].tolist():
        value = clean_text(raw_value)
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
        if len(values) >= max_values:
            break
    return values


def _looks_like_metadata_preview(frame: pd.DataFrame) -> bool:
    columns = [str(column).strip() for column in frame.columns if str(column).strip()]
    if not columns or len(columns) > 40:
        return False
    lowered = " ".join(column.lower() for column in columns)
    return any(token in lowered for token in _METADATA_HINT_TOKENS)


def _looks_like_counts_preview(frame: pd.DataFrame) -> bool:
    columns = [str(column).strip() for column in frame.columns if str(column).strip()]
    if len(columns) < 3:
        return False
    if frame.empty:
        return False
    body = frame.iloc[:, 1:].apply(pd.to_numeric, errors="coerce")
    total_cells = int(body.shape[0] * body.shape[1])
    if total_cells <= 0:
        return False
    numeric_cells = int(body.notna().sum().sum())
    return numeric_cells >= max(2, int(total_cells * 0.6))


def _metadata_level_hints(frame: pd.DataFrame) -> list[str]:
    hints: list[str] = []
    for column_name in [str(column).strip() for column in frame.columns if str(column).strip()]:
        lowered = column_name.lower()
        if not any(token in lowered for token in _METADATA_HINT_TOKENS):
            continue
        values = _column_unique_values(frame, column_name)
        if 2 <= len(values) <= 8:
            hints.append(f"{column_name} levels {_format_preview_tokens(values, total=len(values))}")
        if len(hints) >= 2:
            break
    return hints


def _workbook_role_hints(path: Path) -> tuple[list[str], list[str], str | None, list[str]]:
    matrix_like: list[str] = []
    metadata_like: list[str] = []
    first_sheet_summary: str | None = None
    metadata_level_hints: list[str] = []
    if not is_excel_workbook(path):
        return matrix_like, metadata_like, first_sheet_summary, metadata_level_hints
    try:
        workbook = pd.ExcelFile(path)
    except Exception:
        return matrix_like, metadata_like, first_sheet_summary, metadata_level_hints
    try:
        for sheet_name in workbook.sheet_names[:8]:
            sheet_token = _canonical_hint_token(sheet_name)
            if any(marker in sheet_token for marker in ("count", "counts", "norm", "expr", "expression", "matrix")) and sheet_name not in matrix_like:
                matrix_like.append(sheet_name)
            if any(marker in sheet_token for marker in ("meta", "mapping", "annot", "design", "sample")) and sheet_name not in metadata_like:
                metadata_like.append(sheet_name)
            try:
                preview = pd.read_excel(path, sheet_name=sheet_name, nrows=10, dtype=str)
            except Exception:
                continue
            columns = [str(column).strip() for column in preview.columns if str(column).strip()]
            if _looks_like_counts_preview(preview):
                if sheet_name not in matrix_like:
                    matrix_like.append(sheet_name)
            if _looks_like_metadata_preview(preview):
                if sheet_name not in metadata_like:
                    metadata_like.append(sheet_name)
                if not metadata_level_hints:
                    metadata_level_hints = _metadata_level_hints(preview)
            elif len(columns) >= 25 and sum(1 for column in columns if _is_blank_header_alias(column)) >= max(10, len(columns) // 3):
                if sheet_name not in matrix_like:
                    matrix_like.append(sheet_name)
            if first_sheet_summary is None:
                first_sheet_summary = f"first sheet {sheet_name}; columns {_format_preview_tokens(columns[:6], total=len(columns))}"
        if not matrix_like and len(workbook.sheet_names) == 2 and len(metadata_like) == 1:
            for sheet_name in workbook.sheet_names:
                if sheet_name not in metadata_like:
                    matrix_like.append(sheet_name)
    finally:
        workbook.close()
    return matrix_like, metadata_like, first_sheet_summary, metadata_level_hints


def _infer_workbook_matrix_sample_labels(path: Path, sheet_name: str) -> list[str]:
    if not sheet_name:
        return []
    backend = ExpressionMatrixAnalysisBackend()
    try:
        source = backend._load_matrix_source(
            matrix_path=str(path),
            sheet_name=sheet_name,
        )
    except Exception:
        return []
    frame = source.frame.copy()
    if frame.empty:
        return []
    row_id_column = clean_text(backend._infer_row_id_column(frame))
    ignored_tokens = {"index", "id", "row", "rowid"}
    labels: list[str] = []
    for column in frame.columns:
        label = clean_text(column)
        if not label or label == row_id_column:
            continue
        if _canonical_hint_token(label) in ignored_tokens:
            continue
        labels.append(label)
    return labels


def _visible_file_hint(path: Path) -> str | None:
    suffix = path.suffix.lower()
    name = path.name
    if suffix == ".rds":
        return f"- {name}: RDS differential-expression result; call analyze_rnaseq_result directly, do not inspect it as text"
    if is_excel_workbook(path):
        try:
            workbook = pd.ExcelFile(path)
            try:
                sheet_names = list(workbook.sheet_names)
            finally:
                workbook.close()
            preview_sheets = _format_preview_tokens(sheet_names[:4], total=len(sheet_names))
            matrix_like, metadata_like, first_sheet_summary, metadata_level_hints = _workbook_role_hints(path)
            role_bits = []
            if matrix_like:
                role_bits.append(f"likely matrix sheets {_format_preview_tokens(matrix_like[:3], total=len(matrix_like))}")
            if metadata_like:
                role_bits.append(f"likely metadata sheets {_format_preview_tokens(metadata_like[:3], total=len(metadata_like))}")
            if metadata_level_hints:
                role_bits.extend(metadata_level_hints)
            if first_sheet_summary:
                role_bits.append(first_sheet_summary)
            if role_bits:
                return f"- {name}: Excel workbook; sheets {preview_sheets}; {'; '.join(role_bits)}; do not read it as text"
            return f"- {name}: Excel workbook; do not read it as text"
        except Exception:
            return f"- {name}: Excel workbook; do not read it as text"
    if suffix in {".csv", ".txt", ".tsv", ".tab", ".xlsx", ".xls"}:
        tokens = _preview_header_tokens(path)
        if not tokens:
            return f"- {name}: tabular file"
        preview = _format_preview_tokens(tokens[:6], total=len(tokens))
        if len(tokens) > 1 and tokens[0].lower() in {"geneid", "unnamed: 0", "sample", "sampleid", "sample_number"}:
            return f"- {name}: header {preview}"
        return f"- {name}: tabular file; header {preview}"
    return f"- {name}"


def build_schema_hints(visible_files: list[str]) -> list[str]:
    hints: list[str] = []
    for raw_path in visible_files:
        try:
            path = Path(raw_path)
            hint = _visible_file_hint(path)
        except Exception:
            hint = None
        if hint:
            hints.append(hint)
    return hints[:6]


def _detect_counts_and_metadata_paths(visible_files: list[str]) -> tuple[Path | None, Path | None]:
    visible_paths = [Path(path) for path in visible_files]
    counts_path = next(
        (
            path
            for path in visible_paths
            if path.suffix.lower() in {".csv", ".tsv", ".txt", ".tab"}
            and (
                any(token in path.name.lower() for token in ("count", "counts", "featurecount", "featurecounts", "matrix"))
                or _looks_like_counts_file(path)
            )
        ),
        None,
    )
    metadata_path = next(
        (
            path
            for path in visible_paths
            if path.suffix.lower() in {".csv", ".tsv", ".txt", ".tab", ".xlsx", ".xls"}
            and (
                any(token in path.name.lower() for token in ("metadata", "sample", "design", "annotation", "coldata"))
                or _looks_like_metadata_file(path)
            )
        ),
        None,
    )
    return counts_path, metadata_path


def _load_small_table(path: Path) -> pd.DataFrame | None:
    try:
        suffix = path.suffix.lower()
        if is_excel_workbook(path):
            return pd.read_excel(path, nrows=50, dtype=str)
        if suffix in {".tsv", ".tab"}:
            return pd.read_csv(path, sep="\t", nrows=50, dtype=str, keep_default_na=False)
        return pd.read_csv(path, nrows=50, dtype=str, keep_default_na=False)
    except Exception:
        return None


def _looks_like_metadata_file(path: Path) -> bool:
    preview = _load_small_table(path)
    if preview is None or preview.empty:
        return False
    return _looks_like_metadata_preview(preview)


def _looks_like_counts_file(path: Path) -> bool:
    preview = _load_small_table(path)
    if preview is None or preview.empty:
        return False
    return _looks_like_counts_preview(preview)


def _canonical_hint_token(value: object) -> str:
    return "".join(ch for ch in clean_text(value).lower() if ch.isalnum())


def _is_blank_header_alias(value: object) -> bool:
    token = _canonical_hint_token(value)
    return token.startswith("unnamed") or token in {"column1", "column01", "blankgeneidcolumn"}


def _safe_row_id_hint(column_name: object) -> str:
    token = clean_text(column_name)
    if not token or _is_blank_header_alias(token):
        return ""
    return token


def _detect_matrix_and_gene_annotation_paths(visible_files: list[str]) -> tuple[Path | None, Path | None]:
    visible_paths = [Path(path) for path in visible_files]
    matrix_path = next(
        (
            path
            for path in visible_paths
            if path.suffix.lower() in {".csv", ".tsv", ".txt", ".tab"}
            and any(token in path.name.lower() for token in ("count", "counts", "featurecount", "featurecounts", "matrix"))
        ),
        None,
    )
    annotation_path = next(
        (
            path
            for path in visible_paths
            if path.suffix.lower() in {".csv", ".tsv", ".txt", ".tab", ".xlsx", ".xls"}
            and any(token in path.name.lower() for token in ("gene", "feature", "biotype", "length", "annotation", "meta"))
            and not any(token in path.name.lower() for token in ("sample", "design", "metadata"))
        ),
        None,
    )
    return matrix_path, annotation_path


def _detect_sample_metadata_path(visible_files: list[str]) -> Path | None:
    visible_paths = [Path(path) for path in visible_files]
    return next(
        (
            path
            for path in visible_paths
            if path.suffix.lower() in {".csv", ".tsv", ".txt", ".tab", ".xlsx", ".xls"}
            and (
                any(token in path.name.lower() for token in ("sample", "metadata", "design", "annotated", "coldata"))
                or _looks_like_metadata_file(path)
            )
            and not any(token in path.name.lower() for token in ("count", "matrix", "gene"))
        ),
        None,
    )


def _infer_preview_overlap_column(left: pd.DataFrame, right: pd.DataFrame) -> str:
    if left.empty or right.empty:
        return ""
    left_key = str(left.columns[0])
    left_tokens = {
        _canonical_hint_token(value)
        for value in left[left_key].tolist()
        if _canonical_hint_token(value)
    }
    if len(left_tokens) < 2:
        return ""
    ranked: list[tuple[int, int, str]] = []
    for column in right.columns:
        values = {
            _canonical_hint_token(value)
            for value in right[str(column)].tolist()
            if _canonical_hint_token(value)
        }
        overlap = len(values & left_tokens)
        if overlap < 2:
            continue
        token = _canonical_hint_token(column)
        bonus = 1 if any(marker in token for marker in ("gene", "symbol", "ensembl", "feature", "id")) else 0
        ranked.append((overlap, bonus, str(column)))
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][:2] == best[:2]:
        return ""
    return best[2]


def _infer_preview_overlap_column_from_labels(
    labels: list[str],
    right: pd.DataFrame,
    *,
    preferred_tokens: tuple[str, ...] = (),
) -> str:
    label_tokens = {_canonical_hint_token(value) for value in labels if _canonical_hint_token(value)}
    if len(label_tokens) < 2 or right.empty:
        return ""
    ranked: list[tuple[int, int, str]] = []
    for column in right.columns:
        values = {
            _canonical_hint_token(value)
            for value in right[str(column)].tolist()
            if _canonical_hint_token(value)
        }
        overlap = len(values & label_tokens)
        if overlap < 2:
            continue
        column_token = _canonical_hint_token(column)
        bonus = 1 if any(marker in column_token for marker in preferred_tokens) else 0
        ranked.append((overlap, bonus, str(column)))
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][:2] == best[:2]:
        return ""
    return best[2]


def _find_column_by_keywords(frame: pd.DataFrame, keywords: tuple[str, ...]) -> str:
    for column in frame.columns:
        token = _canonical_hint_token(column)
        if any(keyword in token for keyword in keywords):
            return str(column)
    return ""


def _find_question_value_match(frame: pd.DataFrame, question_text: str) -> tuple[str, str]:
    question_tokens = {_canonical_hint_token(part) for part in re.split(r"[^A-Za-z0-9_+-]+", question_text) if _canonical_hint_token(part)}
    question_canonical = _canonical_hint_token(question_text)
    if not question_tokens:
        if not question_canonical:
            return "", ""
    for column in frame.columns:
        seen: set[str] = set()
        for raw_value in frame[str(column)].tolist():
            value = clean_text(raw_value)
            canonical = _canonical_hint_token(value)
            if not canonical or canonical in seen:
                continue
            if canonical.isdigit() and len(canonical) <= 2:
                continue
            seen.add(canonical)
            if canonical in question_tokens or (question_canonical and canonical in question_canonical):
                return str(column), value
    return "", ""


def _find_question_group_pair(frame: pd.DataFrame, question_text: str) -> tuple[str, list[str]]:
    question_tokens = {_canonical_hint_token(part) for part in re.split(r"[^A-Za-z0-9_+-]+", question_text) if _canonical_hint_token(part)}
    question_canonical = _canonical_hint_token(question_text)
    if not question_tokens and not question_canonical:
        return "", []

    preferred_columns: tuple[str, ...] = ()
    if any(token in question_text for token in ("cell type", "celltype", "immune cell", "pbmc")):
        preferred_columns = ("celltype", "cell_type", "cell", "samtype")
    elif any(token in question_text for token in ("sex", "gender", "male", "female")):
        preferred_columns = ("sex", "gender")
    elif any(token in question_text for token in ("batch", "batch effect")):
        preferred_columns = ("batch", "batch_id")
    elif any(token in question_text for token in ("condition", "treatment", "control", "group")):
        preferred_columns = ("condition", "treatment", "group", "class")

    def column_priority(column: object) -> tuple[int, str]:
        token = _canonical_hint_token(column)
        for index, preferred in enumerate(preferred_columns):
            if preferred in token or token in preferred:
                return (index, str(column))
        return (len(preferred_columns) + 1, str(column))

    best_column = ""
    best_values: list[str] = []
    for column in sorted(frame.columns, key=column_priority):
        seen: set[str] = set()
        matched: list[str] = []
        for raw_value in frame[str(column)].tolist():
            value = clean_text(raw_value)
            canonical = _canonical_hint_token(value)
            if not canonical or canonical in seen:
                continue
            if canonical.isdigit() and len(canonical) <= 2:
                continue
            # Single-letter levels such as M/F are too easy to match as a
            # substring of ordinary prose. Accept them only when the question
            # names the level explicitly or the column is semantically preferred.
            if len(canonical) == 1 and canonical not in question_tokens and not any(
                preferred in _canonical_hint_token(column) for preferred in preferred_columns
            ):
                continue
            seen.add(canonical)
            if canonical in question_tokens or (question_canonical and canonical in question_canonical):
                matched.append(value)
        if len(matched) >= 2 and (
            len(matched) > len(best_values)
            or (len(matched) == len(best_values) and column_priority(column) < column_priority(best_column))
        ):
            best_column = str(column)
            best_values = matched
    return best_column, best_values


def _extract_question_gene_targets(question_text: str) -> list[str]:
    matches = re.findall(r"\bgene\s+([A-Za-z0-9_.-]+)\b", question_text, flags=re.IGNORECASE)
    genes: list[str] = []
    seen: set[str] = set()
    for raw in matches:
        token = clean_text(raw).strip(".,;:()[]{}")
        if not token or token.lower() in {"expression", "set", "signature"}:
            continue
        if token not in seen:
            seen.add(token)
            genes.append(token)
    return genes[:3]


def build_matrix_correlation_execution_hints(row: dict[str, Any], visible_files: list[str]) -> list[str]:
    question_text = str(row.get("question") or "").strip().lower()
    if not any(token in question_text for token in ("pearson", "spearman", "correlation", "correlate")):
        return []

    matrix_path, annotation_path = _detect_matrix_and_gene_annotation_paths(visible_files)
    if matrix_path is None or annotation_path is None:
        return []

    matrix_df = _load_small_table(matrix_path)
    annotation_df = _load_small_table(annotation_path)
    if matrix_df is None or matrix_df.empty or annotation_df is None or annotation_df.empty:
        return []

    row_id_column = str(matrix_df.columns[0])
    row_id_hint = _safe_row_id_hint(row_id_column)
    annotation_key_column = _infer_preview_overlap_column(matrix_df, annotation_df)
    annotation_value_column = ""
    if "length" in question_text:
        annotation_value_column = _find_column_by_keywords(annotation_df, ("length",))
    if not annotation_value_column:
        annotation_value_column = _find_column_by_keywords(annotation_df, ("value", "score", "mean"))
    filter_column, filter_value = _find_question_value_match(annotation_df, question_text)
    sample_metadata_path = _detect_sample_metadata_path(visible_files)
    sample_hint = ""
    metadata_key_column = ""
    if sample_metadata_path is not None:
        sample_df = _load_small_table(sample_metadata_path)
        if sample_df is not None and not sample_df.empty:
            sample_labels = [str(column) for column in matrix_df.columns[1:]]
            metadata_key_column = _infer_preview_overlap_column_from_labels(
                sample_labels,
                sample_df,
                preferred_tokens=("sample", "sampleid", "name", "id"),
            )
            sample_column, sample_value = _find_question_value_match(sample_df, question_text)
            if sample_column and sample_value:
                sample_hint = (
                    f"- Likely sample-subset metadata filter: `{sample_column}={sample_value}`."
                )

    hints = ["### Matrix Correlation Hints"]
    if row_id_hint:
        hints.append(f"- Likely matrix row-id column: `{row_id_hint}`.")
    else:
        hints.append("- The matrix row-id lives in the first blank gene-id column, so `row_id_column` can be omitted.")
    if annotation_key_column:
        hints.append(f"- Likely annotation key column: `{annotation_key_column}`.")
    if annotation_value_column:
        hints.append(f"- Likely annotation value column: `{annotation_value_column}`.")
    if filter_column and filter_value:
        hints.append(f"- Likely annotation filter: `{filter_column}={filter_value}`.")
    if metadata_key_column:
        hints.append(f"- Likely sample metadata key column: `{metadata_key_column}`.")
    if sample_hint:
        hints.append(sample_hint)
    return hints if len(hints) > 1 else []


def build_deseq2_execution_hints(row: dict[str, Any], visible_files: list[str]) -> list[str]:
    question_text_raw = str(row.get("question") or "").strip()
    question_text = question_text_raw.lower()
    focus_genes = _extract_question_gene_targets(question_text_raw)
    if not any(
        token in question_text
        for token in ("deseq", "deseq2", "differential expression", "differential gene expression", "rna-seq", "rna seq")
    ):
        return []

    counts_path, metadata_path = _detect_counts_and_metadata_paths(visible_files)
    if counts_path is None or metadata_path is None:
        return []

    sample_headers = _preview_header_tokens(counts_path, max_tokens=64)
    if sample_headers and sample_headers[0] == "<blank gene_id column>":
        sample_headers = sample_headers[1:]
    sample_id_set = {str(value).strip() for value in sample_headers if str(value).strip()}
    if not sample_id_set:
        return []

    metadata_df = _load_small_table(metadata_path)
    if metadata_df is None or metadata_df.empty:
        return []

    overlap_scores: list[tuple[str, int]] = []
    for column in metadata_df.columns:
        values = {str(value).strip() for value in metadata_df[column].tolist() if str(value).strip()}
        overlap_scores.append((str(column), len(values & sample_id_set)))
    overlap_scores.sort(key=lambda item: (-item[1], item[0].lower()))

    hints = ["### DESeq2 Execution Hints"]
    if overlap_scores:
        best_column, best_score = overlap_scores[0]
        compared = ", ".join(f"{column} ({score})" for column, score in overlap_scores[:4])
        hints.append(
            f"- Count-header overlap suggests `sample_column={best_column}` ({best_score}/{len(sample_id_set)} matched sample ids). Candidate columns: {compared}."
        )

    for column in metadata_df.columns:
        column_name = str(column)
        if not any(token in column_name.lower() for token in ("condition", "strain", "group", "treatment", "phenotype", "class")):
            continue
        values: list[str] = []
        seen: set[str] = set()
        for raw_value in metadata_df[column].tolist():
            value = str(raw_value).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
        if not (2 <= len(values) <= 10):
            continue
        hints.append(f"- `{column_name}` levels: [{', '.join(values)}].")
        referenced_levels = [value for value in values if value.lower() in question_text]
        if len(values) > 2 and len(referenced_levels) >= 2:
            hints.append(
                f"- `{column_name}` has more than two levels and this question references named groups. Run separate pairwise `gleskit_analyze_deseq2_counts` calls with explicit `condition_a` and `condition_b`; do not rely on implicit contrast inference here."
            )
            break
    if focus_genes:
        hints.append(
            f"- This is a single-gene question. Prefer `gleskit_analyze_deseq2_counts(..., focus_genes={focus_genes})` so the gene metrics are returned directly."
        )

    return hints if len(hints) > 1 else []


def _detect_explicit_enrichment_background_path(
    visible_files: list[str],
    question_text: str,
) -> Path | None:
    """Find a supplied gene-reference file when the question explicitly names its background."""
    normalized_question = clean_text(question_text).lower()
    if "background" not in normalized_question or not any(
        phrase in normalized_question
        for phrase in ("all genes", "gene universe", "all tested genes", "complete gene set")
    ):
        return None

    candidates: list[tuple[int, Path]] = []
    for raw_path in visible_files:
        path = Path(raw_path)
        name = path.name.lower()
        score = 0
        if "gencode" in name:
            score += 100
        if "background" in name:
            score += 80
        if "reference" in name:
            score += 60
        if "annotation" in name:
            score += 40
        if "gene" in name:
            score += 10
        if score:
            candidates.append((score, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], -len(item[1].name)))[1]


def build_first_move_hint(row: dict[str, Any], visible_files: list[str], schema_hints: list[str]) -> list[str]:
    question_text_raw = str(row.get("question") or "").strip()
    question_text = question_text_raw.lower()
    categories_value = row.get("categories") or []
    if isinstance(categories_value, (list, tuple, set)):
        categories_text = " ".join(str(item) for item in categories_value).lower()
    else:
        categories_text = str(categories_value).lower()
    focus_genes = _extract_question_gene_targets(question_text_raw)
    visible_paths = [Path(path) for path in visible_files]
    # A workbook-like suffix still warrants inspection, even when the bytes do
    # not validate as Excel.  The inspector now chooses the parser from content
    # and will safely report a delimited text matrix or an invalid input.
    workbook_paths = [
        path
        for path in visible_paths
        if is_excel_workbook(path) or path.suffix.lower() in {".xlsx", ".xls"}
    ]
    counts_path, metadata_path = _detect_counts_and_metadata_paths(visible_files)
    matrix_path, gene_annotation_path = _detect_matrix_and_gene_annotation_paths(visible_files)
    counts_paths = [counts_path] if counts_path is not None else []
    metadata_paths = [metadata_path] if metadata_path is not None else []
    result_paths = [
        path
        for path in visible_paths
        if path.suffix.lower() in {".csv", ".tsv", ".txt", ".tab", ".xlsx", ".xls"}
        and any(token in path.name.lower() for token in ("deseq", "deg", "result"))
    ]

    looks_like_deseq2 = any(
        token in question_text
        for token in (
            "deseq",
            "deseq2",
            "differential expression",
            "differential gene expression",
            "differentially expressed",
            "rna-seq",
            "rna seq",
            "counts",
            "metadata",
        )
    )
    looks_like_result_table = any(token in question_text for token in ("result table", "deg", "enrichment"))
    looks_like_matrix_correlation = any(
        token in question_text for token in ("pearson", "spearman", "correlation", "correlate", "gene length")
    )
    looks_like_matrix_stats = any(
        token in question_text
        for token in (
            "skew",
            "wilson",
            "confidence interval",
            "matrix",
            "anova",
            "f-statistic",
            "f statistic",
            "median log2",
            "fold change distribution",
            "distribution of log2",
            "pearson",
            "spearman",
            "correlation",
        )
    )

    first_sheet_hint = ""
    if workbook_paths:
        workbook_name = workbook_paths[0].name
        for hint in schema_hints:
            if workbook_name in hint and "first sheet" in hint:
                tail = hint.split("first sheet", 1)[1].strip().rstrip(";")
                if tail:
                    first_sheet_hint = f" (schema hint: first sheet {tail})"
                break

    asks_for_cell_type_deg_counts = (
        any(token in question_text for token in ("cell type", "immune cell type"))
        and (
            "differentially expressed genes" in question_text
            or "significant deg" in question_text
            or "significantly expressed genes" in question_text
            or " deg " in f" {question_text} "
        )
        and any(token in question_text for token in ("highest number", "most", "largest number", "lowest number", "fewest"))
    )
    looks_like_cell_type_deg_workbook = any(
        token in path.name.lower()
        for path in workbook_paths
        for token in ("by_cell_type", "cell_type", "cell-type")
    )

    if workbook_paths and asks_for_cell_type_deg_counts and (
        looks_like_cell_type_deg_workbook or "single-cell" in categories_text or "single cell" in categories_text
    ):
        workbook_path = workbook_paths[0]
        return [
            "### Suggested First Move",
            "- This looks like a single-cell DEG-count question over a cell-type workbook.",
            (
                f"- Start with `gleskit_analyze_single_cell_rnaseq` using "
                f"`analysis=cell_type_deg_counts`, `deg_table_path={workbook_path}`, and `padj_threshold=0.05`."
            ),
            "- If the workbook stores one cell type per sheet, omit `cell_type_column` and let the tool use each sheet name as the cell type label.",
            "- If the first gene-id column is blank or named `Unnamed: 0`, omit `gene_column` and let the tool use the first column automatically.",
            "- Finalize from `question_ready.top_cell_type` or the first row of `cell_type_counts`; do not loop sheet-by-sheet with `gleskit_analyze_rnaseq_result`.",
        ]

    if metadata_paths and counts_paths and looks_like_deseq2:
        count_path = counts_paths[0]
        workbook_path = metadata_paths[0]
        enrichment_background_path = _detect_explicit_enrichment_background_path(
            visible_files,
            question_text_raw,
        )
        exact_go_library = "GO_Biological_Process_2021" if "go_biological_process_2021" in question_text else ""
        exact_reactome_library = "Reactome_2022" if "reactome_2022" in question_text else ""
        named_library_match = re.search(
            r"\b([A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+)\s+(?:gene\s+set\s+)?library\b",
            question_text_raw,
            flags=re.IGNORECASE,
        )
        exact_named_enrichr_library = (
            named_library_match.group(1).replace("-", "_")
            if named_library_match and "gseapy" in question_text
            else ""
        )
        exact_enrichr_library = exact_go_library or exact_reactome_library or exact_named_enrichr_library
        explicit_p_value = bool(re.search(r"\bp\s*[<≤]\s*0?\.\d+", question_text)) and "padj" not in question_text
        explicit_padj = "padj" in question_text or "adjusted p" in question_text or "adjusted-p" in question_text
        base_mean_match = re.search(r"basemean\s*[><=]\s*([0-9]+(?:\.[0-9]+)?)", question_text)
        lfc_match = re.search(
            r"(?:absolute\s+)?(?:log2\s*)?(?:fold\s*change|lfc|fc)\s*[><=]\s*([0-9]+(?:\.[0-9]+)?)",
            question_text,
        )
        hint_lines = [
            "### Suggested First Move",
            "- This looks like a raw counts + sample metadata RNA-seq question.",
            (
                f"- Start with `gleskit_analyze_deseq2_counts` using `counts_path={count_path}` "
                f"and `sample_metadata_path={workbook_path}`{first_sheet_hint}."
            ),
            "- Let the tool open the metadata file directly; do not read workbook or TSV contents manually when the tool can use the path.",
        ]
        if explicit_p_value:
            hint_lines.append('- Use `significance_mode="p_value"` and `p_value_threshold=0.05`; do not substitute padj.')
        elif explicit_padj:
            hint_lines.append('- Use `significance_mode="padj"` and the explicit adjusted-p threshold.')
        else:
            hint_lines.append('- No significance metric is specified; use the tool default `significance_mode="padj"`.')
        if base_mean_match:
            hint_lines.append(f"- Apply the requested result filter with `base_mean_threshold={float(base_mean_match.group(1)):g}`.")
        if lfc_match:
            hint_lines.append(
                f"- Apply the requested absolute LFC cutoff with `log2_fold_change_threshold={float(lfc_match.group(1)):g}`."
            )
        elif exact_enrichr_library:
            hint_lines.append(
                "- No LFC cutoff is stated; use `log2_fold_change_threshold=0.0` rather than inventing an absolute-LFC filter."
            )
        if focus_genes:
            hint_lines.append(
                f"- Because this asks about a specific gene, pass `focus_genes={focus_genes}` so the DESeq2 call can surface the gene-level metric directly."
            )
        if exact_enrichr_library:
            source_label = "downregulated" if "downregulated" in question_text else "upregulated" if "upregulated" in question_text else "all-significant"
            source_path_name = "downregulated_genes_path" if source_label == "downregulated" else "upregulated_genes_path" if source_label == "upregulated" else "all_significant_genes_path"
            hint_lines.append(
                f"- After DESeq2, use the {source_label} gene-list handoff (`derived_inputs.{source_path_name}`), then call `gleskit_enrich_gene_set` with `genes_path` from `derived_inputs`, `library=\"{exact_enrichr_library}\"`, and `backend=\"enrichr\"`."
            )
        elif "reactome" in question_text:
            hint_lines.append(
                "- After DESeq2 returns its saved gene-list handoff, call `gleskit_enrich_gene_set` directly with `genes_path` from `derived_inputs` and `database=REAC`; do not use broad RNA-seq orchestration for the enrichment step."
            )
        elif "enrichgo" in question_text or "go enrichment" in question_text or "go process" in question_text:
            enrichment_args = ["`genes_path` from `derived_inputs`", "`database=GO_BP`"]
            if "enrichgo" in question_text:
                enrichment_args.append('`backend="clusterprofiler"`')
            similarity_match = re.search(r"similarity\s*(?:>|>=|above|over)\s*(0?\.\d+)", question_text)
            if similarity_match and any(token in question_text for token in ("remove", "removing", "simplif")):
                enrichment_args.append(f"`simplify_go_similarity={float(similarity_match.group(1)):g}`")
            if enrichment_background_path is not None:
                enrichment_args.append(f"`background_genes_path={enrichment_background_path}`")
                hint_lines.append(
                    "- The question explicitly names a complete gene-reference background. Preserve that reference "
                    "for enrichment; do not replace it with the DESeq2 tested-gene handoff."
                )
            hint_lines.append(
                "- After DESeq2 returns its saved gene-list handoff, call `gleskit_enrich_gene_set` directly with "
                + ", ".join(enrichment_args)
                + "."
            )
        if exact_enrichr_library:
            hint_lines.append("- The returned payload must identify `backend=gseapy_enrichr`; do not describe a clusterProfiler result as gseapy.")
        return hint_lines

    if matrix_path is not None and gene_annotation_path is not None and looks_like_matrix_correlation:
        matrix_df = _load_small_table(matrix_path)
        annotation_df = _load_small_table(gene_annotation_path)
        row_id_hint = (
            _safe_row_id_hint(matrix_df.columns[0])
            if matrix_df is not None and not matrix_df.empty
            else ""
        )
        annotation_key_hint = (
            _infer_preview_overlap_column(matrix_df, annotation_df)
            if matrix_df is not None and not matrix_df.empty and annotation_df is not None and not annotation_df.empty
            else ""
        )
        annotation_value_hint = ""
        if annotation_df is not None and not annotation_df.empty:
            if "length" in question_text:
                annotation_value_hint = _find_column_by_keywords(annotation_df, ("length",))
            if not annotation_value_hint:
                annotation_value_hint = _find_column_by_keywords(annotation_df, ("value", "score", "mean"))
        filter_column, filter_value = (
            _find_question_value_match(annotation_df, question_text)
            if annotation_df is not None and not annotation_df.empty
            else ("", "")
        )
        sample_metadata_path = _detect_sample_metadata_path(visible_files)
        metadata_key_hint = ""
        metadata_group_hint = ""
        metadata_group_value = ""
        if sample_metadata_path is not None:
            sample_df = _load_small_table(sample_metadata_path)
            if sample_df is not None and not sample_df.empty and matrix_df is not None and not matrix_df.empty:
                sample_labels = [str(column) for column in matrix_df.columns[1:]]
                metadata_key_hint = _infer_preview_overlap_column_from_labels(
                    sample_labels,
                    sample_df,
                    preferred_tokens=("sample", "sampleid", "name", "id"),
                )
                metadata_group_hint, metadata_group_value = _find_question_value_match(sample_df, question_text)
        hint_lines = [
            "### Suggested First Move",
            "- This looks like a matrix-to-annotation correlation question.",
            (
                f"- Start with `gleskit_run_expression_statistics` using `analysis=matrix_annotation_correlation`, "
                f"`source_path={matrix_path}`, `annotation_path={gene_annotation_path}`, "
                "`analysis_axis=columns`, `target_axis=features`, and `summary_stat=mean`."
            ),
        ]
        if row_id_hint:
            hint_lines.append(f"- Use `row_id_column={row_id_hint}`.")
        else:
            hint_lines.append("- Omit `row_id_column` and let the tool use the first blank gene-id column automatically.")
        if annotation_key_hint:
            hint_lines.append(f"- Use `annotation_key_column={annotation_key_hint}`.")
        if annotation_value_hint:
            hint_lines.append(f"- Use `annotation_value_column={annotation_value_hint}`.")
        if filter_column and filter_value:
            hint_lines.append(f"- Apply annotation filter `{filter_column}={filter_value}`.")
        if sample_metadata_path is not None and metadata_group_hint and metadata_group_value:
            hint_lines.append(
                f"- Restrict the sample subset with `metadata_path={sample_metadata_path}`, "
                f"`metadata_group_column={metadata_group_hint}`, and `metadata_group_value={metadata_group_value}`."
            )
            if metadata_key_hint:
                hint_lines.append(f"- Use `metadata_key_column={metadata_key_hint}`.")
        hint_lines.append("- Keep the workflow path-driven. Do not open the matrix or annotation tables manually.")
        return hint_lines

    sample_metadata_path = _detect_sample_metadata_path(visible_files)
    if matrix_path is not None and sample_metadata_path is not None and looks_like_matrix_stats:
        matrix_df = _load_small_table(matrix_path)
        sample_df = _load_small_table(sample_metadata_path)
        annotation_df = _load_small_table(gene_annotation_path) if gene_annotation_path is not None else None
        row_id_hint = (
            _safe_row_id_hint(matrix_df.columns[0])
            if matrix_df is not None and not matrix_df.empty
            else ""
        )
        metadata_key_hint = ""
        if sample_df is not None and not sample_df.empty and matrix_df is not None and not matrix_df.empty:
            sample_labels = [str(column) for column in matrix_df.columns[1:]]
            metadata_key_hint = _infer_preview_overlap_column_from_labels(
                sample_labels,
                sample_df,
                preferred_tokens=("sample", "sampleid", "name", "id"),
            )
        group_column, matched_groups = (
            _find_question_group_pair(sample_df, question_text)
            if sample_df is not None and not sample_df.empty
            else ("", [])
        )
        if not group_column and sample_df is not None and not sample_df.empty:
            if any(token in question_text for token in ("cell type", "celltype", "immune cell", "pbmc")):
                group_column = _find_column_by_keywords(sample_df, ("celltype", "cell_type"))
        feature_annotation_key_column = ""
        feature_annotation_group_column = ""
        feature_annotation_group_value = ""
        if annotation_df is not None and not annotation_df.empty and matrix_df is not None and not matrix_df.empty:
            feature_annotation_key_column = _infer_preview_overlap_column(matrix_df, annotation_df)
            if "mirna" in question_text:
                candidate = _find_column_by_keywords(annotation_df, ("biotype", "type", "class"))
                if candidate:
                    feature_annotation_group_column = candidate
                    feature_annotation_group_value = "miRNA"
            if not feature_annotation_group_value:
                feature_annotation_group_column, feature_annotation_group_value = _find_question_value_match(annotation_df, question_text)

        pairwise_lfc_anova = "anova" in question_text and "log2 fold change" in question_text
        if any(token in question_text for token in ("anova", "f-statistic", "f statistic")):
            hint_lines = [
                "### Suggested First Move",
                "- This is a matrix ANOVA question, not a DESeq2 contrast question.",
                (
                    f"- Start with `gleskit_run_expression_statistics` using `analysis=matrix_group_test`, "
                    f"`source_path={matrix_path}`, and `metadata_path={sample_metadata_path}`."
                ),
                "- Choose the observation unit from the question; never silently flatten all feature-by-sample values.",
            ]
            if pairwise_lfc_anova:
                hint_lines.append(
                    "- Compare the pairwise log2 fold-change distributions across the selected groups. Use `test=anova`, `observation_unit=pairwise_log2_fold_change`, and `pseudocount=1.0`; do not use flattened raw expression values."
                )
            else:
                hint_lines.append(
                    "- Use `observation_unit=featurewise` for per-feature tests, or `flattened_values` only when the question explicitly says to pool every matrix entry."
                )
            if row_id_hint:
                hint_lines.append(f"- Use `row_id_column={row_id_hint}`.")
            else:
                hint_lines.append("- Omit `row_id_column`; the tool will resolve the blank first identifier column.")
            if metadata_key_hint:
                hint_lines.append(f"- Use `metadata_key_column={metadata_key_hint}`.")
            if group_column:
                hint_lines.append(f"- Use `metadata_group_column={group_column}`.")
            if len(matched_groups) >= 2:
                hint_lines.append(f"- Restrict to the question's groups with `metadata_group_values={matched_groups!r}`.")
            if gene_annotation_path is not None and feature_annotation_group_column and feature_annotation_group_value:
                hint_lines.append(
                    f"- Filter features with `feature_annotation_path={gene_annotation_path}`, `feature_annotation_group_column={feature_annotation_group_column}`, and `feature_annotation_group_value={feature_annotation_group_value}`."
                )
                if feature_annotation_key_column:
                    hint_lines.append(f"- Use `feature_annotation_key_column={feature_annotation_key_column}`.")
            hint_lines.append("- Do not use DESeq2 for this matrix ANOVA.")
            hint_lines.append("- Keep the workflow path-driven. Do not open the matrix or annotation tables manually.")
            return hint_lines

        if any(token in question_text for token in ("fold change distribution", "distribution of log2")):
            hint_lines = [
                "### Suggested First Move",
                "- This is an all-pairwise matrix log2 fold-change distribution question, not a DESeq2 contrast question.",
                (
                    f"- Start with `gleskit_run_expression_statistics` using `analysis=matrix_log2_fold_change_distribution`, "
                    f"`source_path={matrix_path}`, and `metadata_path={sample_metadata_path}`."
                ),
                "- Use `analysis_axis=columns` and include every group named in the question via `metadata_group_values`.",
            ]
            if row_id_hint:
                hint_lines.append(f"- Use `row_id_column={row_id_hint}`.")
            else:
                hint_lines.append("- Omit `row_id_column`; the tool will resolve the blank first identifier column.")
            if metadata_key_hint:
                hint_lines.append(f"- Use `metadata_key_column={metadata_key_hint}`.")
            if group_column:
                hint_lines.append(f"- Use `metadata_group_column={group_column}`.")
            if len(matched_groups) >= 2:
                hint_lines.append(f"- Use `metadata_group_values={matched_groups!r}`.")
            if gene_annotation_path is not None and feature_annotation_group_column and feature_annotation_group_value:
                hint_lines.append(
                    f"- Filter features with `feature_annotation_path={gene_annotation_path}`, `feature_annotation_group_column={feature_annotation_group_column}`, and `feature_annotation_group_value={feature_annotation_group_value}`."
                )
                if feature_annotation_key_column:
                    hint_lines.append(f"- Use `feature_annotation_key_column={feature_annotation_key_column}`.")
            hint_lines.append("- Do not use DESeq2 for this distribution calculation.")
            hint_lines.append("- Keep the workflow path-driven. Do not open the matrix or annotation tables manually.")
            return hint_lines

        if any(token in question_text for token in ("median log2", "log2 fold change", "fold change")):
            hint_lines = [
                "### Suggested First Move",
                "- This looks like a matrix fold-change summary question, not a DESeq2 contrast question.",
                (
                    f"- Start with `gleskit_run_expression_statistics` using `analysis=matrix_group_fold_change_summary`, "
                    f"`source_path={matrix_path}`, and `metadata_path={sample_metadata_path}`."
                ),
                "- Use `analysis_axis=columns`.",
            ]
            if row_id_hint:
                hint_lines.append(f"- Use `row_id_column={row_id_hint}`.")
            else:
                hint_lines.append("- Omit `row_id_column` and let the tool use the first blank gene-id column automatically.")
            if metadata_key_hint:
                hint_lines.append(f"- Use `metadata_key_column={metadata_key_hint}`.")
            if group_column and len(matched_groups) >= 2:
                hint_lines.append(
                    f"- Use `metadata_group_column={group_column}`, `group_a={matched_groups[0]}`, and `group_b={matched_groups[1]}`."
                )
            if gene_annotation_path is not None and feature_annotation_group_column and feature_annotation_group_value:
                hint_lines.append(
                    f"- Filter features with `feature_annotation_path={gene_annotation_path}`, "
                    f"`feature_annotation_group_column={feature_annotation_group_column}`, and "
                    f"`feature_annotation_group_value={feature_annotation_group_value}`."
                )
                if feature_annotation_key_column:
                    hint_lines.append(f"- Use `feature_annotation_key_column={feature_annotation_key_column}`.")
            hint_lines.append("- Set `summary_stat=median` and finalize from the returned `summary_value`.")
            hint_lines.append("- Keep the workflow path-driven. Do not open the matrix or metadata tables manually.")
            return hint_lines

        if any(token in question_text for token in ("wilson", "confidence interval", "proportion of differentially expressed genes")):
            hint_lines = [
                "### Suggested First Move",
                "- This looks like a matrix/workbook group-testing question, not a DESeq2 contrast question.",
                (
                    f"- Start with `gleskit_run_expression_statistics` using `analysis=matrix_featurewise_group_test`, "
                    f"`source_path={matrix_path}`, and `metadata_path={sample_metadata_path}`."
                ),
                "- Use `analysis_axis=columns` and a featurewise test such as `test=welch` unless the schema hints indicate otherwise.",
            ]
            if row_id_hint:
                hint_lines.append(f"- Use `row_id_column={row_id_hint}`.")
            else:
                hint_lines.append("- Omit `row_id_column` and let the tool use the first blank gene-id column automatically.")
            if metadata_key_hint:
                hint_lines.append(f"- Use `metadata_key_column={metadata_key_hint}`.")
            if group_column and len(matched_groups) >= 2:
                hint_lines.append(
                    f"- Use `metadata_group_column={group_column}`, `group_a={matched_groups[0]}`, and `group_b={matched_groups[1]}`."
                )
            hint_lines.append(
                "- Then compute the Wilson interval from the returned significant-feature count and tested-feature count with `gleskit_run_expression_statistics(analysis=proportion_interval, method=wilson, confidence_level=0.95)`."
            )
            hint_lines.append("- Keep the workflow path-driven. Do not open the matrix or metadata tables manually.")
            return hint_lines

    if workbook_paths and looks_like_matrix_stats:
        workbook_path = workbook_paths[0]
        matrix_like, metadata_like, _first_sheet_summary, _metadata_level_hints = _workbook_role_hints(workbook_path)
        matrix_sheet = matrix_like[0] if matrix_like else ""
        metadata_sheet = metadata_like[0] if metadata_like else ""
        matrix_sample_labels = _infer_workbook_matrix_sample_labels(workbook_path, matrix_sheet)
        metadata_key_hint = ""
        metadata_group_hint = ""
        matched_groups: list[str] = []
        if metadata_sheet:
            try:
                metadata_preview = pd.read_excel(workbook_path, sheet_name=metadata_sheet, dtype=str)
            except Exception:
                metadata_preview = None
            if metadata_preview is not None and not metadata_preview.empty:
                if matrix_sample_labels:
                    metadata_key_hint = _infer_preview_overlap_column_from_labels(
                        matrix_sample_labels,
                        metadata_preview,
                        preferred_tokens=("sample", "sampleid", "code", "name", "deseq"),
                    )
                if not metadata_key_hint:
                    metadata_key_hint = _find_column_by_keywords(
                        metadata_preview,
                        ("sample", "sampleid", "code", "name", "deseq"),
                    )
                metadata_group_hint, matched_groups = _find_question_group_pair(metadata_preview, question_text)
        hint_lines = [
            "### Suggested First Move",
            "- This workbook input is ambiguous enough that you should inspect it before choosing a statistics or DESeq2 route.",
            f"- Start with `gleskit_inspect_tabular_data` using `path={workbook_path}`.",
            "- Use the returned `sheet_roles`, `candidate_sample_column`, `candidate_group_columns`, and `candidate_analysis_families` to decide the next tool call.",
            "- Keep the workflow path-driven. Do not read the workbook manually when a path-aware tool can inspect or open it directly.",
        ]
        if matrix_sheet:
            hint_lines.append(f"- Expected matrix-like sheet candidate: `{matrix_sheet}`.")
        if metadata_sheet:
            hint_lines.append(f"- Expected metadata-like sheet candidate: `{metadata_sheet}`.")
        if metadata_key_hint:
            hint_lines.append(f"- Likely sample key column after inspection: `{metadata_key_hint}`.")
        if metadata_group_hint and len(matched_groups) >= 2:
            hint_lines.append(
                f"- Likely comparison after inspection: `metadata_group_column={metadata_group_hint}`, "
                f"`group_a={matched_groups[0]}`, and `group_b={matched_groups[1]}`."
            )
        if any(token in question_text for token in ("wilson", "confidence interval", "proportion of differentially expressed genes")):
            hint_lines.append(
                "- If inspection shows a direct matrix-statistics route is appropriate, use `gleskit_run_expression_statistics` "
                "with `analysis=matrix_featurewise_group_test`, then compute the Wilson interval from the returned "
                "significant-feature count and tested-feature count with `analysis=proportion_interval`."
            )
        elif any(token in question_text for token in ("median log2", "log2 fold change", "fold change")):
            hint_lines.append(
                "- If inspection shows a direct matrix-statistics route is appropriate, use "
                "`gleskit_run_expression_statistics` with `analysis=matrix_group_fold_change_summary` and finalize from the "
                "returned `summary_value`."
            )
        return hint_lines

    if result_paths and looks_like_result_table:
        result_path = result_paths[0]
        result_sheet_hint = ""
        if is_excel_workbook(result_path):
            for hint in schema_hints:
                if result_path.name in hint and "first sheet" in hint:
                    tail = hint.split("first sheet", 1)[1].strip().rstrip(";")
                    if tail:
                        result_sheet_hint = f" Consider `sheet_name` based on {tail}."
                    break
        return [
            "### Suggested First Move",
            "- This looks like an RNA-seq result-table question.",
            f"- Start with `gleskit_analyze_rnaseq_result` using `result_path={result_path}`.{result_sheet_hint}",
            "- If the question targets one gene or one metric, use the focus-gene path instead of reopening the whole result table.",
            "- If the tool returns `derived_inputs`, use those compact paths for follow-up steps.",
        ]

    return []


def build_question_prompt(
    *,
    template_text: str,
    row: dict[str, Any],
    staged_bundle: dict[str, Any],
    run_id: str,
    question_root: Path,
) -> str:
    visible_files = list(staged_bundle.get("prompt_visible_files") or [item["path"] for item in staged_bundle["files"]["visible"]])
    audit_files = [item["path"] for item in staged_bundle["files"]["audit"]]
    schema_hints = build_schema_hints(visible_files)
    first_move_hint = build_first_move_hint(row, visible_files, schema_hints)
    deseq2_hints = build_deseq2_execution_hints(row, visible_files)
    matrix_correlation_hints = build_matrix_correlation_execution_hints(row, visible_files)
    prompt_sections = [
        template_text.strip(),
        "",
        "## Evaluation Question",
        f"- run_id: {run_id}",
        f"- question_id: {row['question_id']}",
        f"- short_id: {row.get('short_id')}",
        f"- scope: {row.get('scope')}",
        f"- eval_mode: {row.get('eval_mode')}",
        f"- species: {row.get('species')}",
        f"- numeric: {row.get('numeric')}",
        f"- capsule_uuid: {row.get('capsule_uuid')}",
        f"- paper: {row.get('paper')}",
        f"- categories: {row.get('categories')}",
        "",
        "### Question",
        str(row.get("question") or "").strip(),
        "",
        "### Local Inputs",
        f"Episode root: {question_root}",
        f"Capsule bundle root: {staged_bundle['capsule_root']}",
        "Visible data files to use:",
        file_list_for_prompt(visible_files),
        "Schema hints:",
        "\n".join(schema_hints) if schema_hints else "- none",
        *first_move_hint,
        *deseq2_hints,
        *matrix_correlation_hints,
        "Audit-only files that must not be used for the answer:",
        file_list_for_prompt(audit_files),
        "",
        "### Execution Notes",
        "- Use the local files and GLESKit MCP tools to answer the question.",
        "- Do not inspect the audit notebook files.",
        "- Do not read full tables, workbooks, or hidden tool-output files when a GLESKit tool can work from the path directly.",
        "- Treat exact visible condition and group labels as hard constraints; do not paraphrase or guess them.",
        "- If a tool output already contains the requested statistic, pathway, count, or comparison result, stop and finalize immediately.",
        "- Do not use literature or paper search unless the question explicitly asks for literature-backed support.",
        "- Do not reveal hidden reasoning.",
        "- Return only the final JSON object.",
    ]
    return "\n".join(prompt_sections).strip() + "\n"


def opencode_db_path() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "opencode" / "opencode.db"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".local" / "share" / "opencode" / "opencode.db"
    return DEFAULT_OPENCODE_DB


def _sqlite_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def find_session_id_by_title(db_path: Path, title: str) -> str | None:
    if not db_path.exists():
        return None
    with _sqlite_connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM session WHERE title = ? ORDER BY time_created DESC LIMIT 1",
            (title,),
        ).fetchone()
    return str(row["id"]) if row is not None else None


def session_exists(db_path: Path, session_id: str) -> bool:
    if not db_path.exists():
        return False
    with _sqlite_connect(db_path) as conn:
        row = conn.execute("SELECT id FROM session WHERE id = ? LIMIT 1", (session_id,)).fetchone()
    return row is not None


def load_opencode_session_export(db_path: Path, session_id: str) -> dict[str, Any]:
    with _sqlite_connect(db_path) as conn:
        session_row = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        if session_row is None:
            raise KeyError(f"OpenCode session not found: {session_id}")
        message_rows = conn.execute(
            "SELECT * FROM message WHERE session_id = ? ORDER BY time_created ASC, id ASC",
            (session_id,),
        ).fetchall()
        part_rows = conn.execute(
            "SELECT * FROM part WHERE session_id = ? ORDER BY time_created ASC, id ASC",
            (session_id,),
        ).fetchall()
        input_rows = []
        try:
            input_rows = conn.execute(
                "SELECT * FROM session_input WHERE session_id = ? ORDER BY time_created ASC, id ASC",
                (session_id,),
            ).fetchall()
        except Exception:
            input_rows = []
        event_rows = []
        try:
            event_rows = conn.execute(
                "SELECT * FROM event WHERE aggregate_id = ? ORDER BY seq ASC, id ASC",
                (session_id,),
            ).fetchall()
        except Exception:
            event_rows = []

    parts_by_message: dict[str, list[dict[str, Any]]] = {}
    for part_row in part_rows:
        payload = json.loads(part_row["data"])
        parts_by_message.setdefault(str(part_row["message_id"]), []).append(
            {
                "id": part_row["id"],
                "message_id": part_row["message_id"],
                "session_id": part_row["session_id"],
                "time_created": part_row["time_created"],
                "time_updated": part_row["time_updated"],
                "data": payload,
            }
        )

    messages: list[dict[str, Any]] = []
    for message_row in message_rows:
        payload = json.loads(message_row["data"])
        message_id = str(message_row["id"])
        messages.append(
            {
                "id": message_id,
                "session_id": message_row["session_id"],
                "time_created": message_row["time_created"],
                "time_updated": message_row["time_updated"],
                "data": payload,
                "parts": parts_by_message.get(message_id, []),
            }
        )

    export = {
        "session": dict(session_row),
        "messages": messages,
        "parts": [
            {
                "id": row["id"],
                "message_id": row["message_id"],
                "session_id": row["session_id"],
                "time_created": row["time_created"],
                "time_updated": row["time_updated"],
                "data": json.loads(row["data"]),
            }
            for row in part_rows
        ],
        "session_inputs": [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "type": row["type"],
                "time_created": row["time_created"],
                "time_updated": row["time_updated"],
                "data": row["data"],
            }
            for row in input_rows
        ],
        "events": [
            {
                "id": row["id"],
                "aggregate_id": row["aggregate_id"],
                "seq": row["seq"],
                "type": row["type"],
                "data": json.loads(row["data"]),
            }
            for row in event_rows
        ],
    }
    return export


def load_opencode_session_stats(db_path: Path, session_id: str) -> dict[str, Any]:
    if not db_path.exists():
        return {
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

    with _sqlite_connect(db_path) as conn:
        session_row = conn.execute("SELECT id FROM session WHERE id = ? LIMIT 1", (session_id,)).fetchone()
        if session_row is None:
            return {
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
        message_rows = conn.execute("SELECT data, time_updated FROM message WHERE session_id = ?", (session_id,)).fetchall()
        part_rows = conn.execute("SELECT data, time_updated FROM part WHERE session_id = ?", (session_id,)).fetchall()

    assistant_count = 0
    for row in message_rows:
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        if str(payload.get("role") or "") == "assistant":
            assistant_count += 1

    tool_count = 0
    active_tool_count = 0
    for row in part_rows:
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        if payload.get("type") == "tool":
            tool_count += 1
            state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
            if str(state.get("status") or "").lower() in {"pending", "running", "waiting", "in_progress"}:
                active_tool_count += 1

    pending_provider_times: list[int] = []
    for row in message_rows:
        try:
            payload = json.loads(row["data"])
        except Exception:
            continue
        if str(payload.get("role") or "").lower() != "assistant":
            continue
        message_time = payload.get("time") if isinstance(payload.get("time"), dict) else {}
        created = message_time.get("created")
        completed = message_time.get("completed")
        if created is not None and completed is None:
            try:
                pending_provider_times.append(int(created))
            except (TypeError, ValueError):
                continue

    last_message_updated = max((row["time_updated"] for row in message_rows), default=None)
    last_part_updated = max((row["time_updated"] for row in part_rows), default=None)
    return {
        "session_exists": True,
        "message_count": len(message_rows),
        "assistant_message_count": assistant_count,
        "part_count": len(part_rows),
        "tool_part_count": tool_count,
        "active_tool_count": active_tool_count,
        "pending_provider_count": len(pending_provider_times),
        "pending_provider_started_at": min(pending_provider_times) if pending_provider_times else None,
        "last_message_updated": last_message_updated,
        "last_part_updated": last_part_updated,
    }


def opencode_activity_kind(stats: dict[str, Any]) -> str:
    """Classify the current OpenCode work so watchdogs do not treat live calls as idle."""
    if int(stats.get("active_tool_count") or 0) > 0:
        return "mcp_tool"
    if int(stats.get("pending_provider_count") or 0) > 0:
        return "provider"
    return "idle"


def _iter_message_visible_text(message: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for part in message.get("parts") or []:
        payload = part.get("data") or {}
        if payload.get("type") != "text":
            continue
        text = str(payload.get("text") or "").strip()
        if text:
            texts.append(text)
    return texts


def build_visible_transcript(session_export: dict[str, Any]) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = []
    for message in session_export.get("messages") or []:
        role = str((message.get("data") or {}).get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        texts = _iter_message_visible_text(message)
        if not texts:
            continue
        transcript.append(
            {
                "message_id": message["id"],
                "role": role,
                "time_created": message.get("time_created"),
                "text": "\n".join(texts).strip(),
            }
        )
    return transcript


def build_tool_trace(session_export: dict[str, Any]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for part in session_export.get("parts") or []:
        payload = part.get("data") or {}
        if payload.get("type") != "tool":
            continue
        state = payload.get("state") or {}
        trace.append(
            {
                "part_id": part["id"],
                "message_id": part["message_id"],
                "time_created": part.get("time_created"),
                "tool": payload.get("tool"),
                "call_id": payload.get("callID"),
                "status": state.get("status"),
                "input": state.get("input") or {},
                "output": state.get("output"),
                "metadata": payload.get("metadata") or {},
            }
        )
    return trace


def _message_text_for_contract(message: dict[str, Any]) -> str:
    texts = _iter_message_visible_text(message)
    return "\n".join(texts).strip()


def extract_final_assistant_text(session_export: dict[str, Any]) -> str:
    messages = [message for message in session_export.get("messages") or [] if str((message.get("data") or {}).get("role") or "") == "assistant"]
    for message in reversed(messages):
        text = _message_text_for_contract(message)
        if text:
            return text
    return ""


def _try_parse_json_object(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    fence_matches = list(re.finditer(r"```(?:json)?\s*(.*?)```", stripped, flags=re.DOTALL | re.IGNORECASE))
    for match in reversed(fence_matches):
        candidates.append(match.group(1).strip())
    brace_index = stripped.find("{")
    if brace_index != -1:
        candidates.append(stripped[brace_index:].strip())
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            if candidate.startswith("{"):
                try:
                    obj, _ = json.JSONDecoder().raw_decode(candidate)
                    return obj
                except Exception:
                    pass
    return None


def parse_final_answer_contract(text: str) -> dict[str, Any]:
    raw_text = str(text or "").strip()
    parsed = _try_parse_json_object(raw_text)
    if not isinstance(parsed, dict):
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        answer = lines[0] if lines else None
        evidence = lines[1:] if len(lines) > 1 else []
        confidence = None
        abstain = not bool(answer)
        return {
            "raw_text": raw_text,
            "parsed": False,
            "answer": answer,
            "evidence": evidence,
            "confidence": confidence,
            "abstain": abstain,
            "parse_error": "Could not parse JSON object from final assistant text.",
        }

    answer = parsed.get("answer")
    evidence = parsed.get("evidence")
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []
    confidence = parsed.get("confidence")
    abstain = bool(parsed.get("abstain", False))
    return {
        "raw_text": raw_text,
        "parsed": True,
        "answer": answer,
        "evidence": evidence,
        "confidence": confidence,
        "abstain": abstain,
        "parse_error": None,
    }


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


_TEXT_TOKEN_ALIASES = {
    "mono": "monocyte",
    "monocytes": "monocyte",
}


def _canonical_text_answer(text: Any) -> str:
    without_parenthetical = re.sub(r"\([^)]*\)", " ", str(text or ""))
    normalized = re.sub(r"[^a-z0-9]+", " ", _normalize_text(without_parenthetical))
    tokens = [_TEXT_TOKEN_ALIASES.get(token, token) for token in normalized.split() if token]
    return " ".join(tokens)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _canonical_decimal_text(value: Any) -> str | None:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None
    normalized = decimal_value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized.quantize(Decimal(1)), "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def _parse_interval(text: Any) -> tuple[Decimal, Decimal] | None:
    if isinstance(text, (list, tuple)) and len(text) == 2:
        left = _to_decimal(text[0])
        right = _to_decimal(text[1])
        if left is not None and right is not None:
            return left, right
    if not isinstance(text, str):
        return None
    match = re.search(
        r"[\(\[]\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*[\)\]]",
        text.strip(),
    )
    if not match:
        return None
    left = _to_decimal(match.group(1))
    right = _to_decimal(match.group(2))
    if left is None or right is None:
        return None
    return left, right


def _answer_value_from_contract(contract: dict[str, Any]) -> Any:
    answer = contract.get("answer")
    if isinstance(answer, str):
        stripped = answer.strip()
        if stripped:
            interval = _parse_interval(stripped)
            if interval is not None:
                return interval
            decimal_value = _to_decimal(stripped)
            if decimal_value is not None:
                return decimal_value
            return stripped
    if isinstance(answer, (int, float, Decimal)):
        return _to_decimal(answer)
    if isinstance(answer, list) and len(answer) == 2:
        interval = _parse_interval(answer)
        if interval is not None:
            return interval
    return answer


def score_bixbench_answer(
    row: dict[str, Any],
    final_answer: dict[str, Any],
) -> dict[str, Any]:
    ideal = str(row.get("ideal") or "").strip()
    eval_mode = str(row.get("eval_mode") or "").strip()
    answer_value = _answer_value_from_contract(final_answer)
    abstain = bool(final_answer.get("abstain"))

    score = 0.0
    correct = False
    match_reason = "unparsed"
    manual_review_required = False

    ideal_interval = _parse_interval(ideal)
    predicted_interval = answer_value if isinstance(answer_value, tuple) and len(answer_value) == 2 else None
    predicted_decimal = answer_value if isinstance(answer_value, Decimal) else _to_decimal(answer_value)

    if abstain:
        match_reason = "abstained"
        manual_review_required = eval_mode == "llm_verifier"
    elif eval_mode == "range_verifier" and ideal_interval is not None:
        if predicted_decimal is not None:
            low, high = ideal_interval
            correct = low <= predicted_decimal <= high
            match_reason = "numeric_within_ideal_range" if correct else "numeric_outside_ideal_range"
        elif predicted_interval is not None:
            low, high = ideal_interval
            pred_low, pred_high = predicted_interval
            correct = not (pred_high < low or pred_low > high)
            match_reason = "interval_overlaps_ideal_range" if correct else "interval_outside_ideal_range"
        else:
            correct = _normalize_text(str(answer_value)) == _normalize_text(ideal)
            match_reason = "textual_fallback_match" if correct else "textual_fallback_mismatch"
    else:
        ideal_decimal = _to_decimal(ideal)
        canonical_ideal = _canonical_decimal_text(ideal)
        canonical_answer = _canonical_decimal_text(answer_value)
        if ideal_decimal is not None and canonical_answer is not None:
            correct = canonical_ideal == canonical_answer
            match_reason = "numeric_exact_match" if correct else "numeric_mismatch"
        elif predicted_interval is not None and ideal_interval is not None:
            correct = predicted_interval == ideal_interval
            match_reason = "interval_exact_match" if correct else "interval_mismatch"
        else:
            correct = _canonical_text_answer(answer_value) == _canonical_text_answer(ideal)
            match_reason = "text_exact_match" if correct else "text_mismatch"
        if eval_mode == "llm_verifier" and not correct:
            manual_review_required = True

    score = 1.0 if correct else 0.0
    return {
        "eval_mode": eval_mode,
        "ideal": ideal,
        "predicted": answer_value,
        "predicted_text": str(answer_value),
        "abstain": abstain,
        "score": score,
        "correct": correct,
        "match_reason": match_reason,
        "manual_review_required": manual_review_required,
        "answer_present": answer_value is not None and str(answer_value).strip() != "",
    }


def summarize_rows(rows: list[dict[str, Any]], group_keys: list[str]) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        key = tuple(row.get(field) for field in group_keys)
        buckets.setdefault(key, []).append(row)
    summaries: list[dict[str, Any]] = []
    for key, bucket in sorted(buckets.items(), key=lambda item: tuple("" if value is None else str(value) for value in item[0])):
        answered_bucket = [row for row in bucket if bool(row.get("answer_present", True))]
        summary = {field: value for field, value in zip(group_keys, key)}
        summary["question_count"] = len(bucket)
        summary["answered_count"] = len(answered_bucket)
        summary["unanswered_count"] = len(bucket) - len(answered_bucket)
        summary["scored_question_count"] = len(answered_bucket)
        summary["accuracy"] = (
            round(mean(1.0 if row.get("correct") else 0.0 for row in answered_bucket), 3)
            if answered_bucket
            else None
        )
        summary["mean_tool_calls"] = round(mean(float(row.get("tool_calls") or 0) for row in bucket), 3)
        summary["mean_turns"] = round(mean(float(row.get("turns") or 0) for row in bucket), 3)
        summary["mean_duration_seconds"] = round(mean(float(row.get("duration_seconds") or 0.0) for row in bucket), 3)
        summary["manual_review_rate"] = (
            round(mean(1.0 if row.get("manual_review_required") else 0.0 for row in answered_bucket), 3)
            if answered_bucket
            else None
        )
        summaries.append(summary)
    return summaries


def build_portal_svg_bar_chart(title: str, rows: list[dict[str, Any]], *, value_key: str, label_key: str) -> str:
    if not rows:
        return f"<section><h2>{html.escape(title)}</h2><p>No data.</p></section>"
    width = 760
    bar_height = 24
    row_gap = 12
    label_width = 220
    value_width = width - label_width - 60
    max_value = max(float(row.get(value_key) or 0.0) for row in rows) or 1.0
    svg_height = 30 + len(rows) * (bar_height + row_gap)
    bars = []
    for index, row in enumerate(rows):
        label = html.escape(str(row.get(label_key) or ""))
        value = float(row.get(value_key) or 0.0)
        bar_width = int((value / max_value) * value_width)
        y = 30 + index * (bar_height + row_gap)
        bars.append(
            f'<text x="0" y="{y + 17}" font-size="14" fill="#d7dde8">{label}</text>'
            f'<rect x="{label_width}" y="{y}" width="{bar_width}" height="{bar_height}" rx="8" fill="#4ea1ff" />'
            f'<text x="{label_width + bar_width + 8}" y="{y + 17}" font-size="13" fill="#d7dde8">{value:.3f}</text>'
        )
    return (
        f"<section class=\"chart\"><h2>{html.escape(title)}</h2>"
        f'<svg viewBox="0 0 {width} {svg_height}" width="100%" height="{svg_height}">'
        + "".join(bars)
        + "</svg></section>"
    )


def render_html_page(title: str, body: str) -> str:
    return textwrap.dedent(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>{html.escape(title)}</title>
          <style>
            :root {{
              color-scheme: dark;
              --bg: #0b1020;
              --panel: #121a31;
              --panel-soft: #171f39;
              --text: #edf2ff;
              --muted: #b6c0da;
              --line: #27314d;
              --accent: #62c0ff;
              --good: #64d98f;
              --bad: #ff8f8f;
            }}
            * {{ box-sizing: border-box; }}
            body {{
              margin: 0;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
              background: radial-gradient(circle at top, #152241 0, var(--bg) 58%);
              color: var(--text);
              line-height: 1.5;
            }}
            a {{ color: var(--accent); text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            main {{ max-width: 1300px; margin: 0 auto; padding: 28px 20px 64px; }}
            header {{ margin-bottom: 24px; }}
            h1, h2, h3 {{ line-height: 1.15; }}
            h1 {{ font-size: 2rem; margin: 0 0 8px; }}
            h2 {{ font-size: 1.25rem; margin: 0 0 12px; }}
            h3 {{ font-size: 1rem; margin: 0 0 8px; color: var(--muted); }}
            .meta {{ color: var(--muted); }}
            .grid {{ display: grid; gap: 16px; }}
            .stack {{ display: grid; gap: 18px; }}
            .cards {{ grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin: 18px 0; }}
            .card, section {{
              background: rgba(18, 26, 49, 0.92);
              border: 1px solid var(--line);
              border-radius: 18px;
              padding: 16px 18px;
              box-shadow: 0 18px 55px rgba(0, 0, 0, 0.18);
            }}
            .card strong {{ display: block; font-size: 1.5rem; margin-top: 8px; }}
            .hero {{
              display: grid;
              gap: 10px;
            }}
            .hero h1 {{
              margin-bottom: 0;
            }}
            .hero .meta {{
              max-width: 74ch;
            }}
            .pill {{
              display: inline-flex;
              align-items: center;
              gap: 6px;
              padding: 4px 10px;
              border-radius: 999px;
              border: 1px solid var(--line);
              background: rgba(255, 255, 255, 0.04);
              color: var(--text);
              font-size: 0.72rem;
              font-weight: 700;
              letter-spacing: 0.04em;
              text-transform: uppercase;
              white-space: nowrap;
            }}
            .pill.good {{
              background: rgba(100, 217, 143, 0.12);
              border-color: rgba(100, 217, 143, 0.3);
              color: #9af0b6;
            }}
            .pill.bad {{
              background: rgba(255, 143, 143, 0.12);
              border-color: rgba(255, 143, 143, 0.3);
              color: #ffb0b0;
            }}
            .pill.warn {{
              background: rgba(255, 197, 90, 0.12);
              border-color: rgba(255, 197, 90, 0.3);
              color: #ffd98b;
            }}
            .pill.neutral {{
              background: rgba(255, 255, 255, 0.04);
              color: var(--muted);
            }}
            .question-card {{
              display: grid;
              gap: 14px;
              background: rgba(255, 255, 255, 0.02);
              border: 1px solid var(--line);
              border-radius: 18px;
              padding: 18px;
            }}
            .question-head {{
              display: grid;
              grid-template-columns: minmax(0, 1fr) minmax(280px, 560px);
              align-items: start;
              gap: 16px;
            }}
            .question-head > div:first-child {{
              min-width: 0;
            }}
            .question-head h3 {{
              margin: 0;
              font-size: 1.08rem;
            }}
            .question-meta {{
              display: flex;
              flex-wrap: wrap;
              gap: 8px;
              margin-top: 10px;
            }}
            .comparison-grid {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
              gap: 14px;
            }}
            .comparison-grid .wide {{
              grid-column: 1 / -1;
            }}
            .judge-panel {{
              display: grid;
              gap: 14px;
            }}
            .judge-final-decision {{
              border-color: rgba(122, 162, 255, 0.35);
              background: rgba(52, 74, 140, 0.18);
            }}
            .judge-overview-grid {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
              gap: 12px;
            }}
            .judge-grid {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
              gap: 14px;
            }}
            .judge-section {{
              background: rgba(255, 255, 255, 0.025);
              border: 1px solid var(--line);
              border-radius: 14px;
              padding: 14px;
              display: grid;
              gap: 12px;
            }}
            .judge-section h4 {{
              margin: 0;
              font-size: 1rem;
              color: var(--text);
            }}
            .judge-subgrid {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
              gap: 12px;
            }}
            .judge-provenance-list {{
              display: grid;
              gap: 12px;
            }}
            .judge-provenance-item {{
              padding: 0;
              overflow: hidden;
            }}
            .judge-provenance-item > summary {{
              padding: 12px 14px;
              font-weight: 700;
            }}
            .judge-provenance-grid {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
              gap: 12px;
              padding: 0 0 4px;
            }}
            .judge-provenance-grid .wide {{
              grid-column: 1 / -1;
            }}
            .judge-detail {{
              background: rgba(255, 255, 255, 0.03);
              border: 1px solid var(--line);
              border-radius: 12px;
              padding: 12px;
              display: grid;
              gap: 8px;
            }}
            .judge-detail.wide {{
              grid-column: 1 / -1;
            }}
            .judge-detail .full-text {{
              font-size: 0.96rem;
            }}
            .judge-empty {{
              margin: 0;
              color: var(--muted);
            }}
            .bullet-list {{
              margin: 0;
              padding-left: 18px;
              white-space: normal;
            }}
            .bullet-list li {{
              margin: 0 0 8px;
            }}
            .block {{
              background: var(--panel-soft);
              border: 1px solid var(--line);
              border-radius: 14px;
              padding: 14px;
            }}
            .block h3 {{
              margin-top: 0;
            }}
            .block-label {{
              color: var(--muted);
              font-size: 0.76rem;
              text-transform: uppercase;
              letter-spacing: 0.08em;
              margin-bottom: 10px;
            }}
            .full-text {{
              white-space: pre-wrap;
              word-break: break-word;
              overflow-wrap: anywhere;
              font-size: 0.98rem;
              line-height: 1.65;
            }}
            .kpi-row {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
              gap: 12px;
              align-self: start;
              width: min(100%, 560px);
            }}
            .kpi {{
              background: var(--panel-soft);
              border: 1px solid var(--line);
              border-radius: 14px;
              padding: 12px 14px;
            }}
            .kpi .label {{
              display: block;
              color: var(--muted);
              font-size: 0.76rem;
              text-transform: uppercase;
              letter-spacing: 0.04em;
            }}
            .kpi .value {{
              display: block;
              margin-top: 6px;
              font-size: 1.35rem;
              font-weight: 700;
            }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{
              text-align: left;
              vertical-align: top;
              padding: 10px 8px;
              border-bottom: 1px solid var(--line);
              font-size: 0.94rem;
            }}
            th {{ color: var(--muted); font-weight: 600; }}
            pre {{
              margin: 0;
              background: var(--panel-soft);
              border: 1px solid var(--line);
              border-radius: 14px;
              padding: 14px;
              overflow: auto;
              white-space: pre-wrap;
              word-break: break-word;
            }}
            .code-block {{
              white-space: pre;
              overflow: auto;
              max-height: 420px;
              background: #0e1530;
              border: 1px solid var(--line);
              border-radius: 14px;
              padding: 14px;
              font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
              font-size: 0.88rem;
              line-height: 1.6;
              tab-size: 2;
            }}
            details {{
              background: var(--panel-soft);
              border: 1px solid var(--line);
              border-radius: 14px;
              padding: 10px 14px;
            }}
            summary {{ cursor: pointer; color: var(--accent); }}
            details.question-accordion {{
              padding: 0;
              overflow: hidden;
            }}
            details.question-accordion > summary {{
              list-style: none;
              padding: 16px 18px;
              color: inherit;
            }}
            details.question-accordion > summary::-webkit-details-marker {{
              display: none;
            }}
            details.question-accordion[open] > summary {{
              border-bottom: 1px solid var(--line);
              background: rgba(255, 255, 255, 0.02);
            }}
            .question-summary {{
              display: grid;
              grid-template-columns: minmax(0, 1fr) minmax(240px, 420px);
              gap: 16px;
              align-items: start;
            }}
            .question-summary-main {{
              min-width: 0;
              display: grid;
              gap: 10px;
            }}
            .question-summary-top {{
              display: flex;
              flex-wrap: wrap;
              align-items: center;
              gap: 10px;
            }}
            .question-link {{
              font-size: 1rem;
              font-weight: 700;
            }}
            .question-preview {{
              color: var(--muted);
              white-space: pre-wrap;
              word-break: break-word;
              overflow-wrap: anywhere;
            }}
            .question-response-preview {{
              display: grid;
              gap: 4px;
            }}
            .question-accordion-body {{
              padding: 16px 18px 18px;
              display: grid;
              gap: 16px;
            }}
            .question-accordion-body .stack {{
              gap: 14px;
            }}
            .question-links {{
              display: flex;
              flex-wrap: wrap;
              gap: 12px;
            }}
            .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
            .ok {{ color: var(--good); }}
            .bad {{ color: var(--bad); }}
            .chart svg {{ display: block; margin-top: 8px; }}
            .question-list li {{ margin: 0 0 8px; }}
            @media (max-width: 960px) {{
              .question-head {{
                grid-template-columns: 1fr;
              }}
              .question-summary {{
                grid-template-columns: 1fr;
              }}
              .kpi-row {{
                width: 100%;
              }}
            }}
          </style>
        </head>
        <body>
        <main>
        {body}
        </main>
        </body>
        </html>
        """
    ).strip()


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


def build_failure_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("stop_reason") or row.get("status") or "unknown").strip() or "unknown"
        buckets[reason] = buckets.get(reason, 0) + 1
    return [
        {"stop_reason": reason, "count": count, "share": round(count / len(rows), 3) if rows else 0.0}
        for reason, count in sorted(buckets.items(), key=lambda item: (-item[1], item[0]))
    ]


def make_summary_rows(question_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for question in question_rows:
        judge_payload = question.get("judge") if isinstance(question.get("judge"), dict) else {}
        judge = judge_payload.get("judge") if isinstance(judge_payload.get("judge"), dict) else {}
        raw_score_band = (
            question.get("judge_score_band")
            if isinstance(question.get("judge_score_band"), dict)
            else judge_payload.get("judge_score_band")
            if isinstance(judge_payload.get("judge_score_band"), dict)
            else None
        )
        judge_score_value = question.get("judge_score", judge.get("judge_score"))
        score_band = raw_score_band or judge_score_band(
            None if judge_score_value is None else float(judge_score_value)
        )
        rows.append(
            {
                "question_id": question["question_id"],
                "short_id": question.get("short_id"),
                "scope": question.get("scope"),
                "numeric": question.get("numeric"),
                "species": question.get("species"),
                "eval_mode": question.get("eval_mode"),
                "skills_mode": question.get("skills_mode"),
                "capsule_uuid": question.get("capsule_uuid"),
                "episode_id": question.get("episode_id") or question.get("question_id"),
                "score": question.get("score", 0.0),
                "correct": question.get("correct", False),
                "manual_review_required": question.get("manual_review_required", False),
                "status": question.get("status"),
                "stop_reason": question.get("stop_reason"),
                "session_id": question.get("session_id"),
                "session_title": question.get("session_title"),
                "turns": question.get("turns", 0),
                "tool_calls": question.get("tool_calls", 0),
                "duration_seconds": question.get("duration_seconds", 0.0),
                "total_duration_seconds": question.get("total_duration_seconds", question.get("duration_seconds", 0.0)),
                "attempt_count": question.get("attempt_count", 1),
                "retry_count": question.get("retry_count", 0),
                "prompt_sha256": question.get("prompt_sha256"),
                "bundle_sha256": question.get("bundle_sha256"),
                "question": question.get("question"),
                "answer": question.get("answer"),
                "answer_present": question.get("answer_present", False),
                "answer_source": question.get("answer_source"),
                "agent_answer": question.get("agent_answer"),
                "effective_answer": question.get("effective_answer"),
                "answer_discrepancy": question.get("answer_discrepancy", False),
                "ideal": question.get("ideal"),
                "confidence": question.get("confidence"),
                "abstain": question.get("abstain"),
                "agent_only_score": question.get("agent_only_score"),
                "judge_score": judge_score_value,
                "judge_score_band": score_band,
                "judge_score_band_key": (score_band or {}).get("key"),
                "judge_score_band_label": (score_band or {}).get("label"),
                "judge_score_band_range": (score_band or {}).get("range_label"),
                "committee_category": question.get("committee_category", judge.get("committee_category")),
                "committee_level_mean_final": question.get("committee_level_mean_final", judge.get("committee_level_mean_final")),
                "review_required": question.get("review_required", judge.get("review_required")),
                "judge_arbiter_raw_score": question.get("judge_arbiter_raw_score", judge.get("arbiter_raw_score")),
                "judge_verdict": question.get("judge_verdict", judge.get("judge_verdict")),
                "judge_agreement_with_benchmark": question.get(
                    "judge_agreement_with_benchmark", judge.get("judge_agreement_with_benchmark")
                ),
                "judge_short_review": question.get("judge_short_review", judge.get("short_review")),
                "judge_rationale": question.get("judge_rationale", judge.get("rationale")),
                "judge_disagreement_flags": question.get("judge_disagreement_flags", judge.get("disagreement_flags")),
                "judge_stats_category": question.get("judge_stats_category", judge.get("stats_category")),
                "judge_biology_category": question.get("judge_biology_category", judge.get("biology_category")),
                "judge_evidence_category": question.get("judge_evidence_category", judge.get("evidence_category")),
            }
        )
    return rows
