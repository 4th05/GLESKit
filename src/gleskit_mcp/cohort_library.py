from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import math
import re
from typing import Any

import pandas as pd

from gleskit_mcp.tabular import load_table


@dataclass(slots=True)
class CohortPreparationResult:
    counts: pd.DataFrame
    metadata: pd.DataFrame
    summary: dict[str, Any]
    profile: dict[str, Any]
    warnings: list[str]


@dataclass(slots=True)
class TableInspectionResult:
    data_type: str
    role: str
    summary: dict[str, Any]
    technical: dict[str, Any]
    inspection: dict[str, Any]
    warnings: list[str]


def detect_delimiter(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".tsv", ".tab", ".txt"}:
        return "\t"
    if suffix == ".csv":
        return ","
    sample = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=["\t", ",", ";"])
        return dialect.delimiter
    except csv.Error:
        return "\t"


def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    loaded = load_table(path=str(path), dtype=str, keep_default_na=False)
    frame = loaded.frame.copy()
    if frame.empty:
        raise ValueError(f"Input file is empty: {path}")
    frame = frame.fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def inspect_tabular_file(
    path: Path,
    *,
    role_hint: str = "",
    provenance: dict[str, Any] | None = None,
) -> TableInspectionResult:
    frame = read_table(path)
    return inspect_tabular_frame(
        frame,
        source_path=str(path),
        role_hint=role_hint,
        provenance=provenance,
    )


def inspect_tabular_frame(
    frame: pd.DataFrame,
    *,
    source_path: str = "",
    role_hint: str = "",
    provenance: dict[str, Any] | None = None,
) -> TableInspectionResult:
    source_path = str(source_path or "").strip()
    provenance = dict(provenance or {})
    normalized = frame.copy()
    normalized.columns = [str(column).strip() for column in normalized.columns]
    normalized = normalized.fillna("")

    row_count, column_count = int(normalized.shape[0]), int(normalized.shape[1])
    if row_count == 0 or column_count == 0:
        raise ValueError("Input table is empty.")

    columns = [str(column).strip() for column in normalized.columns]
    body = normalized.iloc[:, 1:] if column_count > 1 else normalized.iloc[:, :0]
    body_values = body.to_numpy().ravel().tolist() if not body.empty else []
    body_nonempty = [str(value).strip() for value in body_values if str(value).strip()]
    body_numeric_ratio = _numeric_ratio(body_nonempty)
    row_ids = [str(value).strip() for value in normalized.iloc[:, 0].tolist()]
    first_column = columns[0]

    inspector = _infer_table_kind(
        normalized,
        role_hint=role_hint,
        provenance=provenance,
        body_numeric_ratio=body_numeric_ratio,
        row_ids=row_ids,
        columns=columns,
        source_path=source_path,
    )
    data_type = inspector["data_type"]
    role = inspector["role"]
    warnings = list(inspector.get("warnings", []))

    summary = {
        "data_type": data_type,
        "role": role,
        "sample_count": int(inspector.get("sample_count", 0)),
        "gene_count": int(inspector.get("gene_count", 0)),
        "has_metadata": bool(inspector.get("has_metadata", False)),
        "validation_status": inspector.get("validation_status", "ok"),
        "one_line_summary": inspector.get("one_line_summary", ""),
    }
    technical = {
        "path": source_path,
        "file_name": Path(source_path).name if source_path else "",
        "format": inspector.get("format", "tabular"),
        "row_count": row_count,
        "column_count": column_count,
        "first_column": first_column,
        "columns": columns,
        "source_path": source_path,
        "role_hint": role_hint,
        "provenance": provenance,
    }
    inspection = {
        "shape": {"rows": row_count, "columns": column_count},
        "data_type": data_type,
        "role": role,
        "orientation": inspector.get("orientation", "unknown"),
        "row_identifier_column": inspector.get("row_identifier_column") or first_column,
        "column_names": columns,
        "sample_count": int(inspector.get("sample_count", 0)),
        "gene_count": int(inspector.get("gene_count", 0)),
        "candidate_sample_column": inspector.get("candidate_sample_column", ""),
        "candidate_group_columns": list(inspector.get("candidate_group_columns", [])),
        "candidate_covariates": list(inspector.get("candidate_covariates", [])),
        "candidate_group_levels": dict(inspector.get("candidate_group_levels", {})),
        "candidate_sample_preview": list(inspector.get("candidate_sample_preview", [])),
        "matrix_sample_preview": list(inspector.get("matrix_sample_preview", [])),
        "matrix_sample_columns": list(inspector.get("matrix_sample_columns", [])),
        "candidate_sample_group_map": dict(inspector.get("candidate_sample_group_map") or {}),
        "detected_columns": dict(inspector.get("detected_columns", {})),
        "validation": {
            "status": inspector.get("validation_status", "ok"),
            "warnings": warnings,
        },
        "next_step_hints": list(inspector.get("next_step_hints", [])),
        "recommended_arguments": dict(inspector.get("recommended_arguments") or {}),
    }
    return TableInspectionResult(
        data_type=data_type,
        role=role,
        summary=summary,
        technical=technical,
        inspection=inspection,
        warnings=warnings,
    )


def prepare_cohort_inputs(
    *,
    counts_path: Path,
    metadata_path: Path,
    transpose: bool = False,
    sample_column: str = "",
    group_column: str = "",
    min_count: int = 10,
    sample_limit: int = 0,
) -> CohortPreparationResult:
    counts_frame = read_table(counts_path)
    metadata_frame = read_table(metadata_path)
    canonical_counts, counts_summary = _normalize_counts(counts_frame, transpose=transpose)
    resolved_sample_column = _resolve_sample_column(metadata_frame, sample_column)
    aligned_metadata, metadata_profile = _align_and_profile_metadata(
        metadata_frame,
        sample_column=resolved_sample_column,
        sample_ids=list(canonical_counts.columns),
        group_column=group_column,
    )
    if int(sample_limit or 0) > 0:
        limit = min(int(sample_limit), int(canonical_counts.shape[1]))
        selected_samples = list(canonical_counts.columns[:limit])
        canonical_counts = canonical_counts.loc[:, selected_samples].copy()
        aligned_metadata = aligned_metadata.set_index(resolved_sample_column).loc[selected_samples].reset_index()
        metadata_profile["sample_count"] = int(limit)
    filtered_counts, filtering_summary, warnings = _prefilter_counts(
        canonical_counts,
        aligned_metadata,
        sample_column=resolved_sample_column,
        group_column=str(metadata_profile.get("recommended_group_column") or group_column or ""),
        min_count=min_count,
    )

    profile = {
        **metadata_profile,
        "sample_column": resolved_sample_column,
        "orientation": "genes_rows_samples_columns",
        "row_count": int(counts_summary["raw_gene_count"]),
        "column_count": int(counts_summary["raw_sample_count"]),
        "filtered_gene_count": int(filtering_summary["filtered_gene_count"]),
        "min_count": int(min_count),
    }
    summary = {
        "raw_gene_count": int(counts_summary["raw_gene_count"]),
        "raw_sample_count": int(counts_summary["raw_sample_count"]),
        "filtered_gene_count": int(filtering_summary["filtered_gene_count"]),
        "sample_count": int(filtering_summary["sample_count"]),
        "group_column": profile.get("recommended_group_column") or group_column or "",
        "design_formula": profile.get("recommended_design_formula") or "~ 1",
    }
    return CohortPreparationResult(
        counts=filtered_counts,
        metadata=aligned_metadata,
        summary=summary,
        profile=profile,
        warnings=warnings,
    )


def prepare_cohort_inputs_from_frames(
    *,
    counts_frame: pd.DataFrame,
    metadata_frame: pd.DataFrame,
    transpose: bool = False,
    sample_column: str = "",
    group_column: str = "",
    min_count: int = 10,
    sample_limit: int = 0,
) -> CohortPreparationResult:
    canonical_counts, counts_summary = _normalize_counts(counts_frame.copy(), transpose=transpose)
    resolved_sample_column = _resolve_sample_column(metadata_frame.copy(), sample_column)
    aligned_metadata, metadata_profile = _align_and_profile_metadata(
        metadata_frame.copy(),
        sample_column=resolved_sample_column,
        sample_ids=list(canonical_counts.columns),
        group_column=group_column,
    )
    if int(sample_limit or 0) > 0:
        limit = min(int(sample_limit), int(canonical_counts.shape[1]))
        selected_samples = list(canonical_counts.columns[:limit])
        canonical_counts = canonical_counts.loc[:, selected_samples].copy()
        aligned_metadata = aligned_metadata.set_index(resolved_sample_column).loc[selected_samples].reset_index()
        metadata_profile["sample_count"] = int(limit)
    filtered_counts, filtering_summary, warnings = _prefilter_counts(
        canonical_counts,
        aligned_metadata,
        sample_column=resolved_sample_column,
        group_column=str(metadata_profile.get("recommended_group_column") or group_column or ""),
        min_count=min_count,
    )

    profile = {
        **metadata_profile,
        "sample_column": resolved_sample_column,
        "orientation": "genes_rows_samples_columns",
        "row_count": int(counts_summary["raw_gene_count"]),
        "column_count": int(counts_summary["raw_sample_count"]),
        "filtered_gene_count": int(filtering_summary["filtered_gene_count"]),
        "min_count": int(min_count),
    }
    summary = {
        "raw_gene_count": int(counts_summary["raw_gene_count"]),
        "raw_sample_count": int(counts_summary["raw_sample_count"]),
        "filtered_gene_count": int(filtering_summary["filtered_gene_count"]),
        "sample_count": int(filtering_summary["sample_count"]),
        "group_column": profile.get("recommended_group_column") or group_column or "",
        "design_formula": profile.get("recommended_design_formula") or "~ 1",
    }
    return CohortPreparationResult(
        counts=filtered_counts,
        metadata=aligned_metadata,
        summary=summary,
        profile=profile,
        warnings=warnings,
    )


def _normalize_counts(frame: pd.DataFrame, *, transpose: bool) -> tuple[pd.DataFrame, dict[str, Any]]:
    if frame.shape[1] < 2:
        raise ValueError("Counts matrix must contain one gene/sample identifier column plus at least one value column.")

    first_column = frame.columns[0]
    row_ids = frame.iloc[:, 0].astype(str).str.strip().tolist()
    column_ids = [str(column).strip() for column in frame.columns[1:]]
    if transpose:
        sample_ids = row_ids
        gene_ids = column_ids
        values = frame.iloc[:, 1:]
    else:
        gene_ids = row_ids
        sample_ids = column_ids
        values = frame.iloc[:, 1:]

    if not all(gene_ids):
        raise ValueError("Counts matrix contains empty gene identifiers.")
    if not all(sample_ids):
        raise ValueError("Counts matrix contains empty sample identifiers.")
    _require_unique(gene_ids, label="gene")
    _require_unique(sample_ids, label="sample")

    numeric = values.apply(lambda column: column.map(_parse_integer_cell))
    matrix = numeric.astype("int64")
    if transpose:
        matrix = matrix.transpose()
    matrix.index = gene_ids
    matrix.columns = sample_ids
    if (matrix < 0).any().any():
        raise ValueError("Counts matrix contains negative values.")
    all_zero = matrix.sum(axis=1) == 0
    if bool(all_zero.any()):
        matrix = matrix.loc[~all_zero].copy()
    return matrix, {
        "raw_gene_count": len(gene_ids),
        "raw_sample_count": len(sample_ids),
        "orientation": "samples_rows_genes_columns" if transpose else "genes_rows_samples_columns",
        "source_column": str(first_column),
    }


def _resolve_sample_column(frame: pd.DataFrame, sample_column: str) -> str:
    token = str(sample_column or "").strip()
    if token:
        if token not in frame.columns:
            raise ValueError(f"Metadata file does not contain sample column '{token}'.")
        return token
    candidates = ["sampleid", "sample", "barcode", "samplebarcode", "submitterid", "id"]
    normalized_columns = {
        "".join(character for character in str(column).strip().lower() if character.isalnum()): str(column)
        for column in frame.columns
    }
    for candidate in candidates:
        if candidate in normalized_columns:
            return normalized_columns[candidate]
    return str(frame.columns[0])


def _align_and_profile_metadata(
    frame: pd.DataFrame,
    *,
    sample_column: str,
    sample_ids: list[str],
    group_column: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if sample_column not in frame.columns:
        raise ValueError(f"Metadata file does not contain sample column '{sample_column}'.")
    metadata = frame.copy()
    metadata[sample_column] = metadata[sample_column].astype(str).str.strip()
    if metadata[sample_column].eq("").any():
        raise ValueError("Metadata contains empty sample identifiers.")
    _require_unique(metadata[sample_column].tolist(), label="sample")
    metadata_ids = metadata[sample_column].tolist()
    metadata_map = { _canonical_sample_token(sample_id): str(sample_id) for sample_id in metadata_ids if _canonical_sample_token(sample_id) }
    canonical_sample_ids = [_canonical_sample_token(sample_id) for sample_id in sample_ids]
    matched_ids: list[str] = []
    for token, original in zip(canonical_sample_ids, sample_ids, strict=False):
        target = metadata_map.get(token)
        if target is None:
            raise ValueError("Counts and metadata sample identifiers do not match exactly.")
        matched_ids.append(target)
    if len(set(matched_ids)) != len(sample_ids):
        raise ValueError("Counts and metadata sample identifiers do not match exactly.")
    metadata = metadata.set_index(sample_column).loc[matched_ids].reset_index()

    profile = _build_metadata_profile(metadata, sample_column=sample_column, group_column=group_column)
    return metadata, profile


def _build_metadata_profile(
    metadata: pd.DataFrame,
    *,
    sample_column: str,
    group_column: str,
) -> dict[str, Any]:
    columns = [str(column) for column in metadata.columns]
    candidate_group_columns: list[str] = []
    candidate_covariates: list[str] = []
    for column in columns:
        if column == sample_column:
            continue
        values = [str(value).strip() for value in metadata[column].tolist() if str(value).strip()]
        if not values:
            continue
        unique_values = list(dict.fromkeys(values))
        unique_count = len(unique_values)
        numeric_like = all(_is_integer_like(value) or _is_float_like(value) for value in values)
        if 2 <= unique_count <= 10 and not numeric_like:
            candidate_group_columns.append(column)
        elif unique_count <= 20:
            candidate_covariates.append(column)

    recommended_group_column = str(group_column or "").strip()
    if not recommended_group_column:
        for column in candidate_group_columns:
            recommended_group_column = column
            break

    recommended_levels: list[str] = []
    if recommended_group_column and recommended_group_column in metadata.columns:
        values = [str(value).strip() for value in metadata[recommended_group_column].tolist() if str(value).strip()]
        recommended_levels = list(dict.fromkeys(values))
    recommended_contrast = {
        "condition_a": recommended_levels[0] if recommended_levels else "",
        "condition_b": recommended_levels[1] if len(recommended_levels) > 1 else "",
    }
    formula_terms: list[str] = []
    for column in candidate_covariates[:2]:
        if column and column not in formula_terms and column != recommended_group_column:
            formula_terms.append(column)
    if recommended_group_column:
        formula_terms.append(recommended_group_column)
    recommended_design_formula = "~ 1"
    if formula_terms:
        recommended_design_formula = "~ " + " + ".join(formula_terms)

    return {
        "sample_column": sample_column,
        "candidate_group_columns": candidate_group_columns,
        "candidate_covariates": candidate_covariates,
        "recommended_group_column": recommended_group_column,
        "recommended_contrast": recommended_contrast,
        "recommended_design_formula": recommended_design_formula,
        "sample_count": int(metadata.shape[0]),
    }


def _prefilter_counts(
    counts: pd.DataFrame,
    metadata: pd.DataFrame,
    *,
    sample_column: str,
    group_column: str,
    min_count: int,
) -> tuple[pd.DataFrame, dict[str, Any], list[str]]:
    warnings: list[str] = []
    filtered = counts.copy()
    min_group_size = 1
    if group_column and group_column in metadata.columns:
        group_sizes = metadata.groupby(group_column)[sample_column].nunique(dropna=True)
        if not group_sizes.empty:
            min_group_size = max(1, int(group_sizes.min()))
    threshold = max(1, int(min_count))
    keep_mask = (filtered >= threshold).sum(axis=1) >= min_group_size
    filtered = filtered.loc[keep_mask].copy()
    if filtered.empty:
        raise ValueError("No genes remain after minimum-count prefiltering.")
    if min_group_size > 1:
        warnings.append(f"Applied min_count={threshold} filter across at least {min_group_size} sample(s) per group.")
    else:
        warnings.append(f"Applied min_count={threshold} filter across at least one sample.")
    return filtered, {
        "filtered_gene_count": int(filtered.shape[0]),
        "sample_count": int(filtered.shape[1]),
        "min_group_size": int(min_group_size),
    }, warnings


def _parse_integer_cell(value: Any) -> int:
    token = str(value).strip()
    if token == "":
        raise ValueError("Counts matrix contains missing values.")
    if token.startswith("+"):
        token = token[1:]
    if token.isdigit() or (token.startswith("-") and token[1:].isdigit()):
        parsed = int(token)
    else:
        try:
            parsed_float = float(token)
        except ValueError as err:
            raise ValueError(f"Counts matrix contains non-integer value '{value}'.") from err
        if not math.isfinite(parsed_float):
            raise ValueError(f"Counts matrix contains non-integer value '{value}'.")
        parsed = int(round(parsed_float))
        if abs(parsed_float - parsed) > 1e-8:
            raise ValueError(f"Counts matrix contains non-integer value '{value}'.")
    if parsed < 0:
        raise ValueError("Counts matrix contains negative values.")
    return parsed


def _require_unique(values: list[str], *, label: str) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        token = str(value).strip()
        if token in seen and token not in duplicates:
            duplicates.append(token)
        seen.add(token)
    if duplicates:
        raise ValueError(f"Duplicate {label} identifiers are not allowed: {', '.join(duplicates[:5])}")


def _is_integer_like(value: str) -> bool:
    token = str(value).strip()
    if token.startswith("+"):
        token = token[1:]
    return token.isdigit() or (token.startswith("-") and token[1:].isdigit())


def _is_float_like(value: str) -> bool:
    token = str(value).strip()
    try:
        float(token)
    except ValueError:
        return False
    return "." in token or "e" in token.lower()


def _canonical_sample_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    return "".join(ch for ch in token if ch.isalnum())


def _infer_table_kind(
    frame: pd.DataFrame,
    *,
    role_hint: str,
    provenance: dict[str, Any],
    body_numeric_ratio: float,
    row_ids: list[str],
    columns: list[str],
    source_path: str,
) -> dict[str, Any]:
    role_hint_token = str(role_hint or "").strip().lower()
    provenance_role = str(provenance.get("cohort_role") or "").strip().lower()
    explicit_role = provenance_role or role_hint_token
    column_tokens = [str(column).strip().lower() for column in columns]
    first_column = columns[0] if columns else ""
    identifier_column = _infer_matrix_identifier_column(frame, columns)
    matrix_columns = _infer_matrix_sample_columns(frame, columns, identifier_column)
    detected_columns: dict[str, list[str]] = {}
    warnings: list[str] = []

    if explicit_role in {"counts", "metadata"}:
        data_type = "raw_counts" if explicit_role == "counts" else "metadata"
        role = explicit_role
    elif _looks_like_enrichment_table(column_tokens):
        data_type = "enrichment_result"
        role = "enrichment"
    elif _looks_like_mixed_count_result_table(frame, columns, body_numeric_ratio):
        data_type = "raw_counts"
        role = "counts"
    elif _looks_like_results_table(column_tokens):
        data_type = "deseq2_results"
        role = "results"
    elif _looks_like_gene_list_table(frame):
        data_type = "gene_list"
        role = "genes"
    elif _looks_like_ranked_signature_table(frame, column_tokens):
        data_type = "ranked_signature"
        role = "signature"
    elif _looks_like_continuous_matrix(frame, matrix_columns, body_numeric_ratio, source_path):
        data_type = "continuous_matrix"
        role = "continuous_matrix"
    elif body_numeric_ratio >= 0.75 and frame.shape[1] >= 2:
        data_type = "raw_counts"
        role = "counts"
    elif _looks_like_metadata_table(frame, column_tokens):
        data_type = "metadata"
        role = "metadata"
    else:
        data_type = "generic_table"
        role = "table"

    # A metadata sheet often starts with an internal row index. Resolve a
    # semantic sample-ID column before falling back to that first column.
    candidate_sample_column = _resolve_sample_column(frame, "") if data_type == "metadata" else ""
    candidate_group_columns: list[str] = []
    candidate_covariates: list[str] = []
    candidate_group_levels: dict[str, list[str]] = {}
    candidate_sample_preview: list[str] = []
    matrix_sample_preview: list[str] = []
    candidate_sample_group_map: dict[str, str] = {}
    sample_count = 0
    gene_count = 0
    orientation = "unknown"
    has_metadata = False
    validation_status = "ok"
    next_step_hints: list[str] = []
    recommended_arguments: dict[str, Any] = {}

    if data_type in {"raw_counts", "continuous_matrix"}:
        selected_row_ids = [str(value).strip() for value in frame[identifier_column].tolist()] if identifier_column in frame.columns else row_ids
        orientation, sample_count, gene_count = _infer_counts_orientation(
            frame,
            row_ids=selected_row_ids,
            column_ids=matrix_columns,
        )
        # Prefix-number column labels are stronger orientation evidence than
        # miRNA identifiers, which often contain digits and hyphens that look
        # superficially like sample names to generic heuristics.
        inferred_group_map = _infer_sample_groups_from_labels(matrix_columns)
        if data_type == "continuous_matrix" and inferred_group_map:
            orientation = "genes_rows_samples_columns"
            sample_count = len(matrix_columns)
            gene_count = int(frame.shape[0])
        row_like_samples = orientation == "samples_rows_genes_columns"
        if row_like_samples:
            warnings.append("Counts appear to be samples-by-genes; transpose=true will be needed for DESeq2.")
            next_step_hints.append("Pass transpose=true to align to genes-rows, samples-columns for DESeq2.")
        else:
            next_step_hints.append("Counts already match the genes-rows, samples-columns convention.")
        has_metadata = bool(provenance.get("cohort_id")) or role_hint_token == "counts"
        if data_type == "continuous_matrix":
            next_step_hints = [
                "Use run_expression_statistics with matrix_featurewise_group_test or matrix_group_test; do not use DESeq2 for continuous/qPCR values."
            ]
            candidate_sample_group_map = inferred_group_map
            recommended_arguments = {
                "analysis": "matrix_featurewise_group_test",
                "analysis_axis": "columns",
                "sample_group_map": candidate_sample_group_map,
                "group_levels": list(dict.fromkeys(candidate_sample_group_map.values())),
            }
            if candidate_sample_group_map:
                next_step_hints.append(
                    "Sample groups were inferred from prefix-number labels; verify the returned sample_group_map before testing."
                )
            missing_value_count = sum(
                1
                for value in frame[matrix_columns].to_numpy().ravel().tolist()
                if str(value).strip().casefold() in {"", "na", "nan", "n/a", "null"}
            )
            if missing_value_count:
                warnings.append(
                    f"Continuous/qPCR matrix contains {missing_value_count} missing measurement(s). "
                    "Use missing_value_policy='omit' for featurewise tests unless the study protocol requires complete features."
                )
                next_step_hints.append(
                    "For featurewise qPCR/Ct tests with missing measurements, pass missing_value_policy='omit'; "
                    "the tool will report valid group counts per feature."
                )
                recommended_arguments["missing_value_policy"] = "omit"
    elif data_type == "metadata":
        sample_count = int(frame.shape[0])
        gene_count = 0
        candidate_sample_column = _resolve_sample_column(frame, candidate_sample_column)
        if candidate_sample_column and candidate_sample_column in frame.columns:
            candidate_sample_preview = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in frame[candidate_sample_column].tolist()
                    if str(value).strip()
                )
            )[:10]
        candidate_group_columns, candidate_covariates = _metadata_column_roles(frame, sample_column=candidate_sample_column)
        for column in candidate_group_columns:
            values = [
                str(value).strip()
                for value in frame[column].tolist()
                if str(value).strip()
            ]
            candidate_group_levels[str(column)] = list(dict.fromkeys(values))[:10]
        has_metadata = True
        next_step_hints.append("Use the metadata table to choose the sample column and DESeq2 design formula.")
        if candidate_group_columns:
            next_step_hints.append("Pick one of the group columns as the primary condition column.")
    elif data_type == "deseq2_results":
        sample_count = 0
        gene_count = int(frame.shape[0])
        next_step_hints.append("Pass the DESeq2 result table to analyze_rnaseq_result for interpretation and enrichment.")
        candidate_group_columns = []
        candidate_covariates = []
    elif data_type == "enrichment_result":
        sample_count = 0
        gene_count = int(frame.shape[0])
        next_step_hints.append("Use the enrichment result to interpret pathways or filter/significance summaries.")
    elif data_type == "gene_list":
        sample_count = 0
        gene_count = int(frame.shape[0])
        next_step_hints.append("Pass the gene list to enrichment or pathway analysis tools.")
    elif data_type == "ranked_signature":
        sample_count = 0
        gene_count = int(frame.shape[0])
        next_step_hints.append("Pass the ranked signature to ranked GSEA or related enrichment workflows.")
    else:
        sample_count = int(frame.shape[1] - 1 if frame.shape[1] > 1 else 0)
        gene_count = int(frame.shape[0])
        if body_numeric_ratio < 0.25:
            validation_status = "warning"
            warnings.append("This table does not look like a raw counts matrix or a metadata table.")

    if data_type == "metadata":
        detected_columns["sample_column"] = [candidate_sample_column] if candidate_sample_column else []
        if candidate_group_columns:
            detected_columns["group_columns"] = candidate_group_columns
        if candidate_covariates:
            detected_columns["covariates"] = candidate_covariates
    elif data_type in {"raw_counts", "continuous_matrix"}:
        detected_columns["identifier_column"] = [str(identifier_column)] if identifier_column else []
        detected_columns["sample_columns"] = [str(column) for column in matrix_columns]
        matrix_sample_preview = [str(column) for column in matrix_columns[:10]]
    elif data_type in {"deseq2_results", "ranked_signature", "enrichment_result"}:
        detected_columns["gene_column"] = [first_column] if first_column else []
        detected_columns["value_columns"] = [str(column) for column in columns[1:]]

    if data_type == "raw_counts":
        summary_text = f"{gene_count:,} genes × {sample_count:,} samples raw counts matrix"
    elif data_type == "continuous_matrix":
        summary_text = f"{gene_count:,} features × {sample_count:,} samples continuous/qPCR matrix"
    elif data_type == "metadata":
        summary_text = f"Metadata table for {sample_count:,} samples"
    elif data_type == "deseq2_results":
        summary_text = f"DESeq2 results table with {gene_count:,} ranked genes"
    elif data_type == "enrichment_result":
        summary_text = f"Enrichment results table with {gene_count:,} rows"
    elif data_type == "gene_list":
        summary_text = f"Gene list with {gene_count:,} genes"
    elif data_type == "ranked_signature":
        summary_text = f"Ranked signature with {gene_count:,} genes"
    else:
        summary_text = f"Tabular dataset with {int(frame.shape[0]):,} rows and {int(frame.shape[1]):,} columns"

    if provenance.get("cohort_id"):
        cohort_role = str(provenance.get("cohort_role") or role).strip()
        if cohort_role:
            summary_text = f"{cohort_role.capitalize()} for cohort {str(provenance.get('cohort_id') or '').strip()}"
            if sample_count and gene_count:
                summary_text += f" · {gene_count:,} genes × {sample_count:,} samples"

    return {
        "data_type": data_type,
        "role": role,
        "row_identifier_column": identifier_column if data_type in {"raw_counts", "continuous_matrix"} else first_column,
        "sample_count": sample_count,
        "gene_count": gene_count,
        "orientation": orientation,
        "candidate_sample_column": candidate_sample_column,
        "candidate_group_columns": candidate_group_columns,
        "candidate_covariates": candidate_covariates,
        "candidate_group_levels": candidate_group_levels,
        "candidate_sample_preview": candidate_sample_preview,
        "matrix_sample_preview": matrix_sample_preview,
        # The compact MCP response applies a strict size limit, but retaining
        # the complete schema here lets it safely expose every sample for
        # small matrices that need inline metadata construction.
        "matrix_sample_columns": [str(column) for column in matrix_columns],
        "candidate_sample_group_map": candidate_sample_group_map,
        "detected_columns": detected_columns,
        "has_metadata": has_metadata,
        "validation_status": validation_status,
        "one_line_summary": summary_text,
        "warnings": warnings,
        "next_step_hints": next_step_hints,
        "recommended_arguments": recommended_arguments,
    }


def _infer_counts_orientation(
    frame: pd.DataFrame,
    *,
    row_ids: list[str],
    column_ids: list[str],
) -> tuple[str, int, int]:
    row_ids = [str(value).strip() for value in row_ids]
    column_ids = [str(column).strip() for column in column_ids]
    row_sample_score = _score_sample_like(row_ids)
    col_sample_score = _score_sample_like(column_ids)
    row_gene_score = _score_gene_like(row_ids)
    col_gene_score = _score_gene_like(column_ids)
    if row_sample_score > col_sample_score and col_gene_score > row_gene_score:
        return "samples_rows_genes_columns", int(frame.shape[0]), int(max(0, len(column_ids)))
    return "genes_rows_samples_columns", int(len(column_ids)), int(frame.shape[0])


def _metadata_column_roles(frame: pd.DataFrame, *, sample_column: str) -> tuple[list[str], list[str]]:
    candidate_group_columns: list[str] = []
    candidate_covariates: list[str] = []
    for column in frame.columns:
        if column == sample_column:
            continue
        values = [str(value).strip() for value in frame[column].tolist() if str(value).strip()]
        if not values:
            continue
        unique_values = list(dict.fromkeys(values))
        unique_count = len(unique_values)
        numeric_like = all(_is_integer_like(value) or _is_float_like(value) for value in values)
        if 2 <= unique_count <= 10 and not numeric_like:
            candidate_group_columns.append(str(column))
        elif unique_count <= 20:
            candidate_covariates.append(str(column))
    return candidate_group_columns, candidate_covariates


def _looks_like_results_table(column_tokens: list[str]) -> bool:
    markers = {
        "log2foldchange",
        "avglog2fc",
        "avglogfc",
        "padj",
        "pvaladj",
        "pvalue",
        "stat",
        "basemean",
        "lfcse",
    }
    normalized = {token.replace(" ", "").replace("-", "").replace("_", "") for token in column_tokens}
    return len(markers.intersection(normalized)) >= 2


def _looks_like_mixed_count_result_table(
    frame: pd.DataFrame,
    columns: list[str],
    body_numeric_ratio: float,
) -> bool:
    """Recognize workbooks that append DE result columns to a raw-count matrix."""
    normalized = {
        str(column).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        for column in columns
    }
    if "geneid" not in normalized or len(columns) < 12 or body_numeric_ratio < 0.65:
        return False
    return _looks_like_results_table(list(normalized))


def _infer_matrix_identifier_column(frame: pd.DataFrame, columns: list[str]) -> str:
    if not columns:
        return ""
    normalized = {
        str(column).strip().lower().replace(" ", "").replace("-", "").replace("_", ""): str(column)
        for column in columns
    }
    for token in ("geneid", "gene", "featureid", "feature", "transcriptid", "id"):
        if token in normalized:
            return normalized[token]
    candidate_columns = columns[: min(3, len(columns))]
    scored = [
        (
            _score_gene_like([str(value).strip() for value in frame[column].tolist() if str(value).strip()]),
            str(column),
        )
        for column in candidate_columns
    ]
    best_score, best_column = max(scored, key=lambda item: item[0]) if scored else (0, str(columns[0]))
    return best_column if best_score > 0 else str(columns[0])


def _infer_matrix_sample_columns(frame: pd.DataFrame, columns: list[str], identifier_column: str) -> list[str]:
    if not columns or identifier_column not in columns:
        return [str(column) for column in columns[1:]]
    start = columns.index(identifier_column) + 1
    summary_markers = {
        "max", "median", "basemean", "basemeana", "basemeanb", "foldchange",
        "log2foldchange", "pval", "pvalue", "padj", "deg", "stat", "lfcse",
    }
    selected: list[str] = []
    for column in columns[start:]:
        token = str(column).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        if token in summary_markers or token.startswith("deseq") or token.startswith("comparison"):
            break
        values = [str(value).strip() for value in frame[column].tolist() if str(value).strip()]
        if values and _numeric_ratio(values) < 0.65:
            break
        selected.append(str(column))
    return selected or [str(column) for column in columns[start:]]


def _looks_like_enrichment_table(column_tokens: list[str]) -> bool:
    markers = {
        "term",
        "termid",
        "pathway",
        "description",
        "fdr",
        "qvalue",
        "nes",
        "leadingedge",
        "source",
        "intersection",
    }
    normalized = {token.replace(" ", "").replace("-", "").replace("_", "") for token in column_tokens}
    return "term" in normalized and bool(markers.intersection(normalized))


def _looks_like_gene_list_table(frame: pd.DataFrame) -> bool:
    if frame.shape[1] != 1:
        return False
    values = [str(value).strip() for value in frame.iloc[:, 0].tolist() if str(value).strip()]
    return bool(values) and len(dict.fromkeys(values)) == len(values)


def _looks_like_ranked_signature_table(frame: pd.DataFrame, column_tokens: list[str]) -> bool:
    if frame.shape[1] != 2:
        return False
    score_column = column_tokens[1] if len(column_tokens) > 1 else ""
    if score_column in {"count", "counts", "rawcount", "rawcounts", "expression", "abundance", "readcount", "reads"}:
        return False
    if score_column in {"score", "stat", "log2foldchange", "logfc", "foldchange", "effect"}:
        return True
    values = [str(value).strip() for value in frame.iloc[:, 1].tolist() if str(value).strip()]
    return bool(values) and all(_is_integer_like(value) or _is_float_like(value) for value in values)


def _looks_like_metadata_table(frame: pd.DataFrame, column_tokens: list[str]) -> bool:
    if frame.shape[1] < 2:
        return False
    if any(token in {"sample", "sampleid", "sample_id", "submitterid", "submitter_id", "barcode"} for token in column_tokens):
        return True
    body = frame.iloc[:, 1:]
    nonnumeric_ratio = 1.0 - _numeric_ratio([str(value).strip() for value in body.to_numpy().ravel().tolist() if str(value).strip()])
    return nonnumeric_ratio >= 0.6


def _infer_sample_groups_from_labels(labels: list[str]) -> dict[str, str]:
    groups: dict[str, str] = {}
    for label in labels:
        cleaned = str(label).strip()
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)[_-]?[0-9]+", cleaned)
        if not match:
            return {}
        groups[cleaned] = match.group(1)
    return groups if len(groups) == len(labels) and len(set(groups.values())) >= 2 else {}


def _looks_like_continuous_matrix(
    frame: pd.DataFrame,
    columns: list[str],
    body_numeric_ratio: float,
    source_path: str,
) -> bool:
    if frame.shape[1] < 3 or body_numeric_ratio < 0.75:
        return False
    sample_groups = _infer_sample_groups_from_labels(columns[1:])
    if not sample_groups:
        return False
    numeric_values = [
        str(value).strip()
        for value in frame.iloc[:, 1:].to_numpy().ravel().tolist()
        if str(value).strip()
    ]
    decimal_ratio = sum("." in value or "e" in value.lower() for value in numeric_values) / max(1, len(numeric_values))
    path_token = str(source_path or "").lower()
    return decimal_ratio >= 0.05 or any(token in path_token for token in ("qpcr", "mirna", "ct_value", "ct-values"))


def _score_sample_like(values: list[str]) -> float:
    score = 0.0
    for value in values:
        token = str(value).strip().lower()
        if not token:
            continue
        if re.search(r"(tcga|gtex|gsm|srr|sample|barcode|patient)", token):
            score += 3.0
        if any(ch.isdigit() for ch in token):
            score += 1.0
        if "-" in token or "_" in token:
            score += 0.5
        if 4 <= len(token) <= 24:
            score += 0.5
    return score


def _score_gene_like(values: list[str]) -> float:
    score = 0.0
    for value in values:
        token = str(value).strip()
        lowered = token.lower()
        if not token:
            continue
        if lowered.startswith("ensg") or lowered.startswith("entrez"):
            score += 4.0
        if token.isupper() and any(ch.isalpha() for ch in token):
            score += 1.5
        if re.fullmatch(r"[A-Z0-9\-]+", token):
            score += 1.0
        if 2 <= len(token) <= 15:
            score += 0.5
    return score


def _numeric_ratio(values: list[str]) -> float:
    if not values:
        return 0.0
    numeric = 0
    for value in values:
        token = str(value).strip()
        if not token:
            continue
        if _is_integer_like(token) or _is_float_like(token):
            numeric += 1
    return numeric / max(1, len(values))
