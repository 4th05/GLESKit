from __future__ import annotations

import math
import re
from statistics import NormalDist
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats

from gleskit_mcp.analysis.expression_matrix import ExpressionMatrixAnalysisBackend, LoadedMatrix
from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.tabular import load_table
from gleskit_mcp.utils import clean_text, unique_preserve_order


class ExpressionStatisticsBackend:
    def __init__(self, expression_matrix_backend: ExpressionMatrixAnalysisBackend | None = None) -> None:
        self.expression_matrix_backend = expression_matrix_backend or ExpressionMatrixAnalysisBackend()

    def _resolve_matrix_row_id_column(
        self,
        *,
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
    ) -> tuple[str, list[str]]:
        source = self.expression_matrix_backend._load_matrix_source(
            matrix_path=matrix_path,
            table_text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
        )
        frame = source.frame.copy()
        if frame.empty:
            raise ProviderError("Expression matrix analysis requires a non-empty table.")

        resolved = self._resolve_column(frame, row_id_column, required=False, label="row id")
        if resolved:
            notes: list[str] = []
            if source.file_format == "excel" and int(source.header_row or 1) != max(1, int(header_row or 1)):
                notes.append(
                    f"Auto-detected matrix header row {int(source.header_row or 1)} for sheet {clean_text(source.sheet_name)!r}."
                )
            return resolved, notes

        inferred = self.expression_matrix_backend._infer_row_id_column(frame)
        first_column = str(frame.columns[0])
        requested = clean_text(row_id_column)
        if not requested:
            notes: list[str] = []
            if source.file_format == "excel" and int(source.header_row or 1) != max(1, int(header_row or 1)):
                notes.append(
                    f"Auto-detected matrix header row {int(source.header_row or 1)} for sheet {clean_text(source.sheet_name)!r}."
                )
            if inferred != first_column:
                notes.append(f"Used inferred matrix row id column {inferred!r} instead of the leading column {first_column!r}.")
            return inferred, notes

        if self._requested_blank_header_alias(requested) and self._column_can_be_identifier(frame[first_column]):
            return (
                first_column,
                [f"Requested row id column {requested!r} refers to a blank first header; used first matrix column {first_column!r} instead."],
            )

        if self._requested_identifier_like(requested) and self._column_can_be_identifier(frame[first_column]):
            return (
                first_column,
                [f"Requested row id column {requested!r} was not present; used first matrix column {first_column!r} instead."],
            )

        available = self._available_columns_preview(frame)
        raise ProviderError(
            f"Matrix row id column {requested!r} was not found. Available columns include: {available}."
        )

    def _resolve_overlap_key_column(
        self,
        frame: pd.DataFrame,
        requested: str,
        *,
        labels: list[str],
        label: str,
        preferred_tokens: tuple[str, ...] = (),
    ) -> tuple[str, list[str]]:
        resolved = self._resolve_column(frame, requested, required=False, label=label)
        if resolved:
            return resolved, []

        inferred = self._infer_overlap_column(frame, labels=labels, preferred_tokens=preferred_tokens)
        if inferred:
            return inferred, [f"Inferred {label} column {inferred!r} from label overlap."]

        available = self._available_columns_preview(frame)
        raise ProviderError(
            f"Expression statistics requires a valid {label} column. Available columns include: {available}."
        )

    def _resolve_best_overlap_key_column(
        self,
        frame: pd.DataFrame,
        requested: str,
        *,
        labels: list[str],
        label: str,
        preferred_tokens: tuple[str, ...] = (),
    ) -> tuple[str, list[str]]:
        resolved, notes = self._resolve_overlap_key_column(
            frame,
            requested,
            labels=labels,
            label=label,
            preferred_tokens=preferred_tokens,
        )
        best = self._infer_overlap_column(frame, labels=labels, preferred_tokens=preferred_tokens)
        if not best or best == resolved:
            return resolved, notes

        resolved_overlap = self._column_overlap_count(frame[resolved], labels)
        best_overlap = self._column_overlap_count(frame[best], labels)
        if best_overlap >= 2 and best_overlap > resolved_overlap:
            requested_token = clean_text(requested)
            if requested_token:
                notes.append(
                    f"Requested {label} column {resolved!r} matched {resolved_overlap} label(s); used {best!r} instead because it matched {best_overlap}."
                )
            else:
                notes.append(f"Used inferred {label} column {best!r}; it matched {best_overlap} visible label(s).")
            return best, notes
        return resolved, notes

    def _resolve_group_column_with_inference(
        self,
        frame: pd.DataFrame,
        requested: str,
        *,
        label: str,
        requested_values: list[str],
        exclude_columns: list[str] | None = None,
        required: bool,
    ) -> tuple[str, list[str]]:
        notes: list[str] = []
        resolved = self._resolve_column(frame, requested, required=False, label=label)
        inferred = self._infer_group_column(
            frame,
            requested_values=requested_values,
            exclude_columns=exclude_columns or [],
        )
        if resolved and inferred and inferred != resolved:
            resolved_score = self._group_column_match_score(frame[resolved], requested_values)
            inferred_score = self._group_column_match_score(frame[inferred], requested_values)
            if inferred_score > resolved_score:
                notes.append(
                    f"Requested {label} column {resolved!r} did not match the requested group labels well; used {inferred!r} instead."
                )
                return inferred, notes
        if resolved:
            return resolved, notes
        if inferred:
            notes.append(f"Inferred {label} column {inferred!r} from requested group labels.")
            return inferred, notes
        if required:
            available = self._available_columns_preview(frame)
            raise ProviderError(
                f"Expression statistics requires a valid {label} column. Available columns include: {available}."
            )
        return "", notes

    def matrix_annotation_correlation(
        self,
        *,
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: Literal["rows", "columns"] = "columns",
        target_axis: Literal["features", "observations"] = "features",
        summary_stat: Literal["mean", "median"] = "mean",
        annotation_path: str = "",
        annotation_table_text: str = "",
        annotation_delimiter: str = "",
        annotation_sheet_name: str = "",
        annotation_header_row: int = 1,
        annotation_skip_rows: int = 0,
        annotation_key_column: str = "",
        annotation_value_column: str = "",
        annotation_group_column: str = "",
        annotation_group_value: str = "",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        metadata_key_column: str = "",
        metadata_group_column: str = "",
        metadata_group_value: str = "",
        method: Literal["pearson", "spearman"] = "pearson",
    ) -> dict[str, Any]:
        resolved_row_id_column, resolution_notes = self._resolve_matrix_row_id_column(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
        )
        matrix = self.expression_matrix_backend.load_matrix(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=resolved_row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
        )
        prepared = self.expression_matrix_backend._observations(matrix, analysis_axis=analysis_axis)
        metadata_input: dict[str, Any] = {
            "metadata_path": None,
            "metadata_source_kind": None,
            "metadata_available_sheets": [],
            "metadata_key_column": None,
            "metadata_group_column": None,
            "metadata_group_value": None,
            "matched_sample_count": None,
            "selected_sample_count": None,
            "available_groups": [],
        }
        if metadata_path or metadata_table_text or metadata_group_column or metadata_group_value:
            metadata = load_table(
                path=metadata_path,
                text=metadata_table_text,
                delimiter=metadata_delimiter,
                sheet_name=metadata_sheet_name,
                header_row=metadata_header_row,
                skip_rows=metadata_skip_rows,
                dtype=str,
                keep_default_na=False,
            )
            metadata_key, key_notes = self._resolve_best_overlap_key_column(
                metadata.frame,
                metadata_key_column,
                labels=list(prepared.labels),
                label="metadata key",
                preferred_tokens=("sample", "sampleid", "sample_number", "name", "id", "code"),
            )
            resolution_notes.extend(key_notes)
            resolved_metadata_group_column, group_notes = self._resolve_group_column_with_inference(
                metadata.frame,
                metadata_group_column,
                label="metadata group",
                requested_values=[metadata_group_value] if metadata_group_value else [],
                exclude_columns=[metadata_key],
                required=True,
            )
            resolution_notes.extend(group_notes)
            metadata_frame = metadata.frame[[metadata_key, resolved_metadata_group_column]].copy()
            metadata_frame[metadata_key] = metadata_frame[metadata_key].map(clean_text)
            metadata_frame[resolved_metadata_group_column] = metadata_frame[resolved_metadata_group_column].map(clean_text)
            metadata_frame = metadata_frame[
                (metadata_frame[metadata_key].astype(str).str.len() > 0)
                & (metadata_frame[resolved_metadata_group_column].astype(str).str.len() > 0)
            ].copy()
            if metadata_frame.empty:
                raise ProviderError("Expression correlation requires non-empty sample metadata with sample ids and groups.")

            metadata_map: dict[str, str] = {}
            for sample_id, group_value in zip(
                metadata_frame[metadata_key],
                metadata_frame[resolved_metadata_group_column],
                strict=False,
            ):
                token = self._canonical_token(sample_id)
                if not token:
                    continue
                previous = metadata_map.get(token)
                if previous is not None and previous != group_value:
                    raise ProviderError(f"Sample metadata contains conflicting group labels for sample '{sample_id}'.")
                metadata_map[token] = group_value

            sample_indices: list[int] = []
            group_labels: list[str] = []
            for index, label in enumerate(prepared.labels):
                token = self._canonical_token(label)
                if token not in metadata_map:
                    continue
                sample_indices.append(index)
                group_labels.append(metadata_map[token])
            if not sample_indices:
                raise ProviderError("Expression correlation could not align any samples between the matrix and metadata.")

            available_groups = unique_preserve_order(group_labels)
            resolved_metadata_group_value = self._resolve_group_value(metadata_group_value, available_groups)
            if metadata_group_value and not resolved_metadata_group_value:
                raise ProviderError(
                    "Expression correlation sample metadata filter did not match any visible group. "
                    f"metadata_group_column={resolved_metadata_group_column!r}, "
                    f"metadata_group_value={clean_text(metadata_group_value)!r}. "
                    f"Available groups: {', '.join(available_groups) or 'none'}."
                )

            selected_positions = list(range(len(sample_indices)))
            if resolved_metadata_group_value:
                selected_positions = [
                    offset
                    for offset, group_value in enumerate(group_labels)
                    if group_value == resolved_metadata_group_value
                ]
            if not selected_positions:
                raise ProviderError("Expression correlation sample metadata filter removed all aligned samples.")

            prepared_values = np.asarray(prepared.values[sample_indices, :], dtype=float)[selected_positions, :]
            prepared = type(prepared)(
                values=prepared_values,
                labels=[prepared.labels[sample_indices[offset]] for offset in selected_positions],
                feature_labels=list(prepared.feature_labels),
            )
            metadata_input = {
                "metadata_path": metadata.path,
                "metadata_source_kind": metadata.source_kind,
                "metadata_available_sheets": metadata.available_sheets or [],
                "metadata_key_column": metadata_key,
                "metadata_group_column": resolved_metadata_group_column,
                "metadata_group_value": resolved_metadata_group_value or None,
                "matched_sample_count": len(sample_indices),
                "selected_sample_count": len(selected_positions),
                "available_groups": available_groups,
            }

        if target_axis == "features":
            labels = list(prepared.feature_labels)
            values = np.median(prepared.values, axis=0) if summary_stat == "median" else np.mean(prepared.values, axis=0)
        else:
            labels = list(prepared.labels)
            values = np.median(prepared.values, axis=1) if summary_stat == "median" else np.mean(prepared.values, axis=1)
        values = np.asarray(values, dtype=float)
        annotation = load_table(
            path=annotation_path,
            text=annotation_table_text,
            delimiter=annotation_delimiter,
            sheet_name=annotation_sheet_name,
            header_row=annotation_header_row,
            skip_rows=annotation_skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        key_column, key_notes = self._resolve_overlap_key_column(
            annotation.frame,
            annotation_key_column,
            labels=labels,
            label="annotation key",
            preferred_tokens=("gene", "geneid", "symbol", "ensembl", "feature", "id"),
        )
        resolution_notes.extend(key_notes)
        value_column = self._resolve_column(annotation.frame, annotation_value_column, required=True, label="annotation value")
        filter_column = clean_text(annotation_group_column)
        filter_value = clean_text(annotation_group_value)
        series = annotation.frame.copy()
        resolved_filter_column = ""
        matched_annotation_rows: int | None = None
        if filter_value:
            if not filter_column:
                raise ProviderError(
                    "Expression correlation received an annotation subgroup value but no annotation_group_column."
                )
            resolved_filter_column = self._resolve_column(
                series,
                filter_column,
                required=True,
                label="annotation group",
            )
            group_values = series[resolved_filter_column].map(clean_text)
            series = series.loc[group_values == filter_value].copy()
            matched_annotation_rows = len(series.index)
            if matched_annotation_rows < 1:
                available_values = unique_preserve_order(
                    clean_text(value)
                    for value in annotation.frame[resolved_filter_column].tolist()
                    if clean_text(value)
                )
                preview = ", ".join(available_values[:8]) or "none"
                raise ProviderError(
                    "Expression correlation annotation subgroup filter matched no rows. "
                    f"annotation_group_column={resolved_filter_column!r}, annotation_group_value={filter_value!r}. "
                    f"Available values include: {preview}."
                )
        series = series[[key_column, value_column]].copy()
        series[key_column] = series[key_column].map(lambda value: clean_text(value))
        series[value_column] = pd.to_numeric(series[value_column], errors="coerce")
        series = series.dropna(subset=[value_column])
        mapping = {
            clean_text(key): float(value)
            for key, value in zip(series[key_column], series[value_column], strict=False)
            if clean_text(key)
        }
        shared = [label for label in labels if label in mapping]
        if len(shared) < 3:
            raise ProviderError("Expression correlation requires at least 3 shared labels between the matrix summary and annotation table.")
        primary = np.asarray([values[labels.index(label)] for label in shared], dtype=float)
        secondary = np.asarray([mapping[label] for label in shared], dtype=float)
        correlation, p_value = self._correlate(primary, secondary, method=method)
        rows = [
            {
                "label": label,
                "matrix_value": float(primary[index]),
                "annotation_value": float(secondary[index]),
            }
            for index, label in enumerate(shared)
        ]
        filter_summary = ""
        if resolved_filter_column and filter_value:
            filter_summary = f" after filtering {resolved_filter_column}={filter_value}"
        return {
            "summary": (
                f"Computed {method} correlation={float(correlation):.4f} "
                f"(p={float(p_value):.4g}) across {len(shared)} aligned {target_axis}{filter_summary}."
            ),
            "provider": "expression_statistics",
            "analysis": "correlation",
            "mode": "matrix_annotation",
            "input": {
                "matrix_path": matrix.path,
                "matrix_source_kind": matrix.source_kind,
                "matrix_available_sheets": matrix.available_sheets or [],
                "annotation_path": annotation.path,
                "annotation_source_kind": annotation.source_kind,
                "annotation_available_sheets": annotation.available_sheets or [],
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
                "annotation_header_row": int(annotation_header_row),
                "annotation_skip_rows": int(annotation_skip_rows),
                "analysis_axis": analysis_axis,
                "target_axis": target_axis,
                "summary_stat": summary_stat,
                "method": method,
                "row_id_column": matrix.row_id_column,
                "annotation_key_column": key_column,
                "annotation_value_column": value_column,
                "annotation_group_column": resolved_filter_column or None,
                "annotation_group_value": filter_value or None,
                "matched_annotation_rows": matched_annotation_rows,
                **metadata_input,
                "resolution_notes": resolution_notes,
            },
            "result": {
                "correlation": float(correlation),
                "p_value": float(p_value),
                "label_count": len(shared),
            },
            "aligned_rows": rows,
            "resolution_notes": resolution_notes,
        }

    def distribution_summary(
        self,
        *,
        source_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: Literal["rows", "columns"] = "columns",
        value_column: str = "",
        scope: Literal["all_values", "feature_means", "observation_means", "column"] = "all_values",
    ) -> dict[str, Any]:
        values: np.ndarray
        input_payload: dict[str, Any]
        if scope == "column":
            table = load_table(
                path=source_path,
                text=table_text,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                dtype=str,
                keep_default_na=False,
            )
            resolved_value_column = self._resolve_column(table.frame, value_column, required=True, label="value")
            values = pd.to_numeric(table.frame[resolved_value_column], errors="coerce").dropna().to_numpy(dtype=float)
            input_payload = {
                "source_path": table.path,
                "source_kind": table.source_kind,
                "available_sheets": table.available_sheets or [],
                "scope": scope,
                "value_column": resolved_value_column,
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
            }
        else:
            matrix = self.expression_matrix_backend.load_matrix(
                matrix_path=source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=analysis_axis,
                # Pooled distributions do not join or report feature labels, so
                # duplicated symbols must not block a valid value-level summary.
                require_unique_row_ids=False,
            )
            prepared = self.expression_matrix_backend._observations(matrix, analysis_axis=analysis_axis)
            if scope == "all_values":
                values = prepared.values.reshape(-1)
            elif scope == "feature_means":
                values = np.mean(prepared.values, axis=0)
            else:
                values = np.mean(prepared.values, axis=1)
            input_payload = {
                "source_path": matrix.path,
                "source_kind": matrix.source_kind,
                "available_sheets": matrix.available_sheets or [],
                "analysis_axis": analysis_axis,
                "scope": scope,
                "row_count": int(matrix.values.shape[0]),
                "column_count": int(matrix.values.shape[1]),
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
            }

        values = np.asarray(values, dtype=float)
        if values.size < 3:
            raise ProviderError("Distribution summary requires at least 3 numeric values.")
        skewness = float(stats.skew(values, bias=False, nan_policy="omit"))
        return {
            "summary": f"Computed distribution summary over {values.size} numeric value(s).",
            "provider": "expression_statistics",
            "analysis": "distribution",
            "mode": "skewness",
            "input": input_payload,
            "result": {
                "value_count": int(values.size),
                "minimum": float(np.min(values)),
                "maximum": float(np.max(values)),
                "mean": float(np.mean(values)),
                "median": float(np.median(values)),
                "standard_deviation": float(np.std(values, ddof=1)) if values.size > 1 else 0.0,
                "skewness": skewness,
                "skewness_label": self._skewness_label(skewness),
            },
        }

    def group_test_from_table(
        self,
        *,
        source_path: str = "",
        table_text: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        value_column: str = "",
        group_column: str = "",
        key_column: str = "",
        annotation_path: str = "",
        annotation_table_text: str = "",
        annotation_delimiter: str = "",
        annotation_sheet_name: str = "",
        annotation_header_row: int = 1,
        annotation_skip_rows: int = 0,
        annotation_key_column: str = "",
        annotation_group_column: str = "",
        test: Literal["t", "welch", "anova"] = "welch",
    ) -> dict[str, Any]:
        table = load_table(
            path=source_path,
            text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        resolved_value_column = self._resolve_column(table.frame, value_column, required=True, label="value")
        resolved_group_column = clean_text(group_column)
        working = table.frame.copy()
        if not resolved_group_column:
            annotation = load_table(
                path=annotation_path,
                text=annotation_table_text,
                delimiter=annotation_delimiter,
                sheet_name=annotation_sheet_name,
                header_row=annotation_header_row,
                skip_rows=annotation_skip_rows,
                dtype=str,
                keep_default_na=False,
            )
            left_key = self._resolve_column(working, key_column, required=True, label="key")
            right_key = self._resolve_column(annotation.frame, annotation_key_column, required=True, label="annotation key")
            right_group = self._resolve_column(annotation.frame, annotation_group_column, required=True, label="annotation group")
            joined = working.merge(
                annotation.frame[[right_key, right_group]],
                left_on=left_key,
                right_on=right_key,
                how="left",
            )
            resolved_group_column = right_group
            working = joined
        else:
            resolved_group_column = self._resolve_column(working, resolved_group_column, required=True, label="group")

        numeric_values = pd.to_numeric(working[resolved_value_column], errors="coerce")
        groups = working[resolved_group_column].map(clean_text)
        clean = pd.DataFrame(
            {
                "value": numeric_values,
                "group": groups,
            }
        ).dropna(subset=["value"])
        clean = clean[clean["group"].astype(str).str.len() > 0]
        if clean.empty:
            raise ProviderError("Group test requires numeric values and non-empty group labels.")
        grouped = {
            label: rows["value"].to_numpy(dtype=float)
            for label, rows in clean.groupby("group")
        }
        if len(grouped) < 2:
            raise ProviderError("Group test requires at least 2 groups.")
        if test in {"t", "welch"} and len(grouped) != 2:
            raise ProviderError(f"{test} test requires exactly 2 groups.")

        if test == "t":
            labels = sorted(grouped)
            statistic, p_value = stats.ttest_ind(grouped[labels[0]], grouped[labels[1]], equal_var=True, nan_policy="omit")
        elif test == "welch":
            labels = sorted(grouped)
            statistic, p_value = stats.ttest_ind(grouped[labels[0]], grouped[labels[1]], equal_var=False, nan_policy="omit")
        else:
            labels = sorted(grouped)
            statistic, p_value = stats.f_oneway(*(grouped[label] for label in labels))

        group_summaries = [
            {
                "group": label,
                "count": int(grouped[label].size),
                "mean": float(np.mean(grouped[label])),
                "median": float(np.median(grouped[label])),
                "standard_deviation": float(np.std(grouped[label], ddof=1)) if grouped[label].size > 1 else 0.0,
            }
            for label in labels
        ]
        return {
            "summary": f"Computed {test} group test across {len(grouped)} group(s).",
            "provider": "expression_statistics",
            "analysis": "group_test",
            "mode": test,
            "input": {
                "source_path": table.path,
                "source_kind": table.source_kind,
                "available_sheets": table.available_sheets or [],
                "value_column": resolved_value_column,
                "group_column": resolved_group_column,
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
            },
            "result": {
                "group_count": len(grouped),
                "statistic_name": "F" if test == "anova" else "t",
                "statistic": float(statistic),
                "p_value": float(p_value),
            },
            "groups": group_summaries,
        }

    def featurewise_group_test(
        self,
        *,
        source_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        group_column: str = "",
        key_column: str = "",
        pair_column: str = "",
        annotation_path: str = "",
        annotation_table_text: str = "",
        annotation_delimiter: str = "",
        annotation_sheet_name: str = "",
        annotation_header_row: int = 1,
        annotation_skip_rows: int = 0,
        annotation_key_column: str = "",
        annotation_group_column: str = "",
        test: Literal["t", "welch", "anova", "mannwhitney", "kruskal", "paired_t", "paired_wilcoxon"] = "welch",
        alpha: float = 0.05,
    ) -> dict[str, Any]:
        table = load_table(
            path=source_path,
            text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        working = table.frame.copy()
        resolved_row_id = self._resolve_column(working, row_id_column or str(working.columns[0]), required=True, label="row id")
        resolved_group_column = clean_text(group_column)
        resolved_pair_column = clean_text(pair_column)
        annotation_available_sheets: list[str] = []
        if not resolved_group_column:
            annotation = load_table(
                path=annotation_path,
                text=annotation_table_text,
                delimiter=annotation_delimiter,
                sheet_name=annotation_sheet_name,
                header_row=annotation_header_row,
                skip_rows=annotation_skip_rows,
                dtype=str,
                keep_default_na=False,
            )
            left_key = self._resolve_column(working, key_column, required=True, label="key")
            right_key = self._resolve_column(annotation.frame, annotation_key_column, required=True, label="annotation key")
            right_group = self._resolve_column(annotation.frame, annotation_group_column, required=True, label="annotation group")
            working = working.merge(
                annotation.frame[[right_key, right_group]],
                left_on=left_key,
                right_on=right_key,
                how="left",
            )
            resolved_group_column = right_group
            annotation_available_sheets = annotation.available_sheets or []
        else:
            resolved_group_column = self._resolve_column(working, resolved_group_column, required=True, label="group")
        if resolved_pair_column and resolved_pair_column not in working.columns:
            raise ProviderError(f"Featurewise group test pair column '{resolved_pair_column}' was not found.")

        feature_columns = [
            column
            for column in working.columns
            if column
            and column not in {resolved_row_id, resolved_group_column, resolved_pair_column, annotation_key_column}
        ]
        if not feature_columns:
            raise ProviderError("Featurewise group test requires at least one numeric feature column.")

        group_series = working[resolved_group_column].map(clean_text)
        selected_columns = [resolved_row_id, resolved_group_column]
        if resolved_pair_column:
            selected_columns.append(resolved_pair_column)
        selected_columns.extend(feature_columns)
        clean = working.loc[group_series.astype(str).str.len() > 0, selected_columns].copy()
        clean[resolved_group_column] = clean[resolved_group_column].map(clean_text)
        if resolved_pair_column:
            clean[resolved_pair_column] = clean[resolved_pair_column].map(clean_text)
        if clean.empty:
            raise ProviderError("Featurewise group test requires non-empty group labels.")

        grouped = {
            label: rows.index.to_list()
            for label, rows in clean.groupby(resolved_group_column, sort=True)
        }
        if len(grouped) < 2:
            raise ProviderError("Featurewise group test requires at least 2 groups.")
        cleaned_test = clean_text(test).lower() or "welch"
        supported_tests = {"t", "welch", "anova", "mannwhitney", "kruskal", "paired_t", "paired_wilcoxon"}
        if cleaned_test not in supported_tests:
            raise ProviderError(
                "Featurewise group test supports test='t', 'welch', 'anova', 'mannwhitney', 'kruskal', "
                "'paired_t', or 'paired_wilcoxon'."
            )
        effective_test = cleaned_test
        if cleaned_test in {"t", "welch"} and len(grouped) > 2:
            effective_test = "anova"
        if cleaned_test in {"mannwhitney", "paired_t", "paired_wilcoxon"} and len(grouped) != 2:
            raise ProviderError(f"{cleaned_test} test requires exactly 2 groups.")
        if cleaned_test.startswith("paired") and not resolved_pair_column:
            raise ProviderError("Paired featurewise group tests require pair_column.")

        feature_rows: list[dict[str, Any]] = []
        for feature in feature_columns:
            numeric = pd.to_numeric(clean[feature], errors="coerce")
            pair_series = clean[resolved_pair_column] if resolved_pair_column else pd.Series([None] * len(clean), index=clean.index)
            feature_df = pd.DataFrame(
                {
                    "value": numeric,
                    "group": clean[resolved_group_column],
                    "pair": pair_series,
                }
            ).dropna(subset=["value"])
            feature_df = feature_df[feature_df["group"].astype(str).str.len() > 0]
            if feature_df.empty:
                continue

            group_values = {
                label: rows["value"].to_numpy(dtype=float)
                for label, rows in feature_df.groupby("group", sort=True)
            }
            group_means = {label: float(np.mean(values)) for label, values in group_values.items()}
            group_medians = {label: float(np.median(values)) for label, values in group_values.items()}
            group_counts = {label: int(values.size) for label, values in group_values.items()}

            labels = sorted(group_values)
            statistic_name = "t"
            effect_size = 0.0
            p_value = 1.0
            statistic = 0.0

            if effective_test.startswith("paired"):
                pair_df = feature_df.dropna(subset=["pair"]).copy()
                pivot = pair_df.pivot_table(index="pair", columns="group", values="value", aggfunc="first")
                pivot = pivot.dropna()
                if pivot.shape[0] < 2 or pivot.shape[1] != 2:
                    p_value = 1.0
                    statistic = 0.0
                else:
                    pair_labels = list(pivot.columns[:2])
                    left = pivot[pair_labels[0]].to_numpy(dtype=float)
                    right = pivot[pair_labels[1]].to_numpy(dtype=float)
                    diff = left - right
                    if effective_test == "paired_wilcoxon":
                        statistic, p_value = stats.wilcoxon(left, right, alternative="two-sided")
                        statistic_name = "W"
                    else:
                        statistic, p_value = stats.ttest_rel(left, right, nan_policy="omit")
                        statistic_name = "t"
                    effect_size = _paired_effect_size(diff)
                    labels = pair_labels
                    group_counts = {pair_labels[0]: int(left.size), pair_labels[1]: int(right.size)}
                    group_means = {pair_labels[0]: float(np.mean(left)), pair_labels[1]: float(np.mean(right))}
                    group_medians = {pair_labels[0]: float(np.median(left)), pair_labels[1]: float(np.median(right))}
            elif len(group_values) == 2:
                left, right = group_values[labels[0]], group_values[labels[1]]
                if effective_test == "mannwhitney":
                    statistic, p_value = stats.mannwhitneyu(left, right, alternative="two-sided")
                    statistic_name = "U"
                    effect_size = _cohen_d(left, right)
                elif effective_test == "kruskal":
                    statistic, p_value = stats.kruskal(left, right)
                    statistic_name = "H"
                    effect_size = _epsilon_squared(statistic, sum(group_counts.values()), len(labels))
                elif effective_test == "anova":
                    statistic, p_value = stats.f_oneway(*(group_values[label] for label in labels))
                    statistic_name = "F"
                    effect_size = _eta_squared(group_values)
                elif effective_test == "t":
                    statistic, p_value = stats.ttest_ind(left, right, equal_var=True, nan_policy="omit")
                    statistic_name = "t"
                    effect_size = _cohen_d(left, right)
                else:
                    statistic, p_value = stats.ttest_ind(left, right, equal_var=False, nan_policy="omit")
                    statistic_name = "t"
                    effect_size = _cohen_d(left, right)
            else:
                if effective_test == "kruskal":
                    statistic, p_value = stats.kruskal(*(group_values[label] for label in labels))
                    statistic_name = "H"
                    effect_size = _epsilon_squared(statistic, sum(group_counts.values()), len(labels))
                else:
                    statistic, p_value = stats.f_oneway(*(group_values[label] for label in labels))
                    statistic_name = "F"
                    effect_size = _eta_squared(group_values)

            feature_rows.append(
                {
                    "feature": feature,
                    "group_count": len(labels),
                    "groups": labels,
                    "group_counts": group_counts,
                    "group_means": group_means,
                    "group_medians": group_medians,
                    "statistic_name": statistic_name,
                    "statistic": float(statistic),
                    "p_value": float(p_value) if np.isfinite(float(p_value)) else 1.0,
                    "effect_size": float(effect_size),
                }
            )

        if not feature_rows:
            raise ProviderError("Featurewise group test did not find any usable feature columns.")

        p_values = np.asarray([float(row["p_value"]) for row in feature_rows], dtype=float)
        padj_bh = self._adjust_p_values(p_values, method="bh")
        padj_by = self._adjust_p_values(p_values, method="by")
        padj_bonf = self._adjust_p_values(p_values, method="bonferroni")
        for row, bh, by, bonf in zip(feature_rows, padj_bh, padj_by, padj_bonf, strict=False):
            row["padj_bh"] = float(bh)
            row["padj_by"] = float(by)
            row["padj_bonferroni"] = float(bonf)
            row["significant_raw"] = bool(float(row["p_value"]) <= alpha)
            row["significant_bh"] = bool(float(bh) <= alpha)
            row["significant_by"] = bool(float(by) <= alpha)
            row["significant_bonferroni"] = bool(float(bonf) <= alpha)

        feature_rows.sort(key=lambda row: (row["padj_bh"], row["p_value"], row["feature"]))
        significant_bh = [row for row in feature_rows if row["significant_bh"]]
        significant_by = [row for row in feature_rows if row["significant_by"]]
        significant_bonf = [row for row in feature_rows if row["significant_bonferroni"]]
        significant_raw = [row for row in feature_rows if row["significant_raw"]]
        significant_all_corrections = [
            row
            for row in feature_rows
            if row["significant_bh"] and row["significant_by"] and row["significant_bonferroni"]
        ]
        return {
            "summary": (
                f"Computed featurewise group test across {len(feature_rows)} feature(s) and {len(grouped)} group(s)."
            ),
            "provider": "expression_statistics",
            "analysis": "featurewise_group_test",
            "mode": effective_test,
            "input": {
                "source_path": table.path,
                "source_kind": table.source_kind,
                "available_sheets": table.available_sheets or [],
                "row_id_column": resolved_row_id,
                "group_column": resolved_group_column,
                "pair_column": resolved_pair_column or None,
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
                "annotation_header_row": int(annotation_header_row),
                "annotation_skip_rows": int(annotation_skip_rows),
                "annotation_available_sheets": annotation_available_sheets,
                "feature_count": len(feature_rows),
                "group_count": len(grouped),
                "test": cleaned_test,
                "effective_test": effective_test,
                "alpha": float(alpha),
            },
            "result": {
                "feature_count": len(feature_rows),
                "group_count": len(grouped),
                "significant_feature_count_bh": len(significant_bh),
                "significant_feature_count_by": len(significant_by),
                "significant_feature_count_bonferroni": len(significant_bonf),
            },
            "features": feature_rows,
            "significant_features": {
                "bh": [row["feature"] for row in significant_bh],
                "by": [row["feature"] for row in significant_by],
                "bonferroni": [row["feature"] for row in significant_bonf],
            },
        }

    def matrix_featurewise_group_test(
        self,
        *,
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: Literal["rows", "columns"] = "columns",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        metadata_key_column: str = "",
        metadata_group_column: str = "",
        sample_group_map: dict[str, str] | None = None,
        group_a: str = "",
        group_b: str = "",
        feature_annotation_path: str = "",
        feature_annotation_table_text: str = "",
        feature_annotation_delimiter: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_skip_rows: int = 0,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "",
        feature_annotation_group_value: str = "",
        test: Literal["t", "welch", "mannwhitney"] = "welch",
        value_transform: Literal["none", "log2p1", "log2"] = "none",
        missing_value_policy: Literal["error", "omit", "drop_feature"] = "error",
        alpha: float = 0.05,
    ) -> dict[str, Any]:
        dataset = self._prepare_matrix_group_dataset(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            metadata_path=metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            metadata_key_column=metadata_key_column,
            metadata_group_column=metadata_group_column,
            sample_group_map=sample_group_map,
            group_a=group_a,
            group_b=group_b,
            feature_annotation_path=feature_annotation_path,
            feature_annotation_table_text=feature_annotation_table_text,
            feature_annotation_delimiter=feature_annotation_delimiter,
            feature_annotation_sheet_name=feature_annotation_sheet_name,
            feature_annotation_header_row=feature_annotation_header_row,
            feature_annotation_skip_rows=feature_annotation_skip_rows,
            feature_annotation_key_column=feature_annotation_key_column,
            feature_annotation_group_column=feature_annotation_group_column,
            feature_annotation_group_value=feature_annotation_group_value,
            missing_value_policy=missing_value_policy,
        )
        effective_test = clean_text(test).lower() or "welch"
        if effective_test not in {"t", "welch", "mannwhitney"}:
            raise ProviderError("matrix_featurewise_group_test supports test='t', 'welch', or 'mannwhitney'.")

        values = dataset["values"]
        transform = clean_text(value_transform).lower() or "none"
        if transform not in {"none", "log2p1", "log2"}:
            raise ProviderError("value_transform must be 'none', 'log2p1', or 'log2'.")
        values = np.asarray(values, dtype=float)
        if transform == "log2p1":
            if np.nanmin(values) < -1:
                raise ProviderError("log2p1 requires matrix values >= -1.")
            values = np.log2(values + 1.0)
        elif transform == "log2":
            if np.nanmin(values) <= 0:
                raise ProviderError("log2 requires matrix values greater than zero.")
            values = np.log2(values)
        feature_labels = dataset["feature_labels"]
        group_labels = dataset["group_labels"]
        group_a_label = str(dataset["group_a"])
        group_b_label = str(dataset["group_b"])
        left_mask = group_labels == group_a_label
        right_mask = group_labels == group_b_label

        feature_rows: list[dict[str, Any]] = []
        for feature_index, feature in enumerate(feature_labels):
            left = values[left_mask, feature_index]
            right = values[right_mask, feature_index]
            left = left[np.isfinite(left)]
            right = right[np.isfinite(right)]
            if left.size < 2 or right.size < 2:
                continue
            if effective_test == "mannwhitney":
                statistic, p_value = stats.mannwhitneyu(left, right, alternative="two-sided")
                statistic_name = "U"
            elif effective_test == "t":
                statistic, p_value = stats.ttest_ind(left, right, equal_var=True, nan_policy="omit")
                statistic_name = "t"
            else:
                statistic, p_value = stats.ttest_ind(left, right, equal_var=False, nan_policy="omit")
                statistic_name = "t"
            feature_rows.append(
                {
                    "feature": feature,
                    "group_counts": {group_a_label: int(left.size), group_b_label: int(right.size)},
                    "group_means": {group_a_label: float(np.mean(left)), group_b_label: float(np.mean(right))},
                    "group_medians": {group_a_label: float(np.median(left)), group_b_label: float(np.median(right))},
                    "statistic_name": statistic_name,
                    "statistic": float(statistic),
                    "p_value": float(p_value) if np.isfinite(float(p_value)) else 1.0,
                    "effect_size": _cohen_d(left, right),
                }
            )

        if not feature_rows:
            raise ProviderError("matrix_featurewise_group_test did not find any testable features.")

        p_values = np.asarray([float(row["p_value"]) for row in feature_rows], dtype=float)
        padj_bh = self._adjust_p_values(p_values, method="bh")
        padj_by = self._adjust_p_values(p_values, method="by")
        padj_bonf = self._adjust_p_values(p_values, method="bonferroni")
        for row, bh, by, bonf in zip(feature_rows, padj_bh, padj_by, padj_bonf, strict=False):
            row["padj_bh"] = float(bh)
            row["padj_by"] = float(by)
            row["padj_bonferroni"] = float(bonf)
            row["significant_raw"] = bool(float(row["p_value"]) <= alpha)
            row["significant_bh"] = bool(float(bh) <= alpha)
            row["significant_by"] = bool(float(by) <= alpha)
            row["significant_bonferroni"] = bool(float(bonf) <= alpha)

        feature_rows.sort(key=lambda row: (row["padj_bh"], row["p_value"], row["feature"]))
        significant_bh = [row for row in feature_rows if row["significant_bh"]]
        significant_by = [row for row in feature_rows if row["significant_by"]]
        significant_bonf = [row for row in feature_rows if row["significant_bonferroni"]]
        significant_raw = [row for row in feature_rows if row["significant_raw"]]
        significant_all_corrections = [
            row
            for row in feature_rows
            if row["significant_bh"] and row["significant_by"] and row["significant_bonferroni"]
        ]
        return {
            "summary": (
                f"Computed matrix featurewise group test across {len(feature_rows)} feature(s) for "
                f"{group_a_label} vs {group_b_label}."
            ),
            "provider": "expression_statistics",
            "analysis": "matrix_featurewise_group_test",
            "mode": effective_test,
            "input": {**dataset["input"], "value_transform": transform},
            "result": {
                "feature_count": len(feature_labels),
                "tested_feature_count": len(feature_rows),
                "significant_feature_count_raw": len(significant_raw),
                "significant_feature_percentage_raw": float(100.0 * len(significant_raw) / len(feature_rows)),
                "significant_feature_count_bh": len(significant_bh),
                "significant_feature_count_by": len(significant_by),
                "significant_feature_count_bonferroni": len(significant_bonf),
                "significant_feature_count_all_corrections": len(significant_all_corrections),
            },
            "features": feature_rows,
            "significant_features": {
                "raw": [row["feature"] for row in significant_raw],
                "bh": [row["feature"] for row in significant_bh],
                "by": [row["feature"] for row in significant_by],
                "bonferroni": [row["feature"] for row in significant_bonf],
                "all_corrections": [row["feature"] for row in significant_all_corrections],
            },
        }

    def matrix_group_fold_change_summary(
        self,
        *,
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: Literal["rows", "columns"] = "columns",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        metadata_key_column: str = "",
        metadata_group_column: str = "",
        group_a: str = "",
        group_b: str = "",
        feature_annotation_path: str = "",
        feature_annotation_table_text: str = "",
        feature_annotation_delimiter: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_skip_rows: int = 0,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "",
        feature_annotation_group_value: str = "",
        summary_stat: Literal["mean", "median", "q25", "q75", "q10", "q90"] = "median",
        pseudocount: float = 1.0,
    ) -> dict[str, Any]:
        dataset = self._prepare_matrix_group_dataset(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            metadata_path=metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            metadata_key_column=metadata_key_column,
            metadata_group_column=metadata_group_column,
            group_a=group_a,
            group_b=group_b,
            feature_annotation_path=feature_annotation_path,
            feature_annotation_table_text=feature_annotation_table_text,
            feature_annotation_delimiter=feature_annotation_delimiter,
            feature_annotation_sheet_name=feature_annotation_sheet_name,
            feature_annotation_header_row=feature_annotation_header_row,
            feature_annotation_skip_rows=feature_annotation_skip_rows,
            feature_annotation_key_column=feature_annotation_key_column,
            feature_annotation_group_column=feature_annotation_group_column,
            feature_annotation_group_value=feature_annotation_group_value,
        )
        values = dataset["values"]
        group_labels = dataset["group_labels"]
        group_a_label = str(dataset["group_a"])
        group_b_label = str(dataset["group_b"])
        left_mask = group_labels == group_a_label
        right_mask = group_labels == group_b_label
        if np.min(values) < 0:
            raise ProviderError("matrix_group_fold_change_summary requires non-negative matrix values to compute log2 fold changes.")
        left_means = np.mean(values[left_mask, :], axis=0)
        right_means = np.mean(values[right_mask, :], axis=0)
        log2_fold_changes = np.log2((left_means + float(pseudocount)) / (right_means + float(pseudocount)))
        if log2_fold_changes.size < 1:
            raise ProviderError("matrix_group_fold_change_summary did not produce any fold-change values.")

        summary_token = clean_text(summary_stat).lower() or "median"
        summary_lookup = {
            "mean": float(np.mean(log2_fold_changes)),
            "median": float(np.median(log2_fold_changes)),
            "q25": float(np.quantile(log2_fold_changes, 0.25)),
            "q75": float(np.quantile(log2_fold_changes, 0.75)),
            "q10": float(np.quantile(log2_fold_changes, 0.10)),
            "q90": float(np.quantile(log2_fold_changes, 0.90)),
        }
        if summary_token not in summary_lookup:
            raise ProviderError("matrix_group_fold_change_summary supports summary_stat='mean', 'median', 'q25', 'q75', 'q10', or 'q90'.")

        feature_rows = [
            {
                "feature": feature,
                "group_means": {
                    group_a_label: float(left_means[index]),
                    group_b_label: float(right_means[index]),
                },
                "log2_fold_change": float(log2_fold_changes[index]),
            }
            for index, feature in enumerate(dataset["feature_labels"])
        ]
        feature_rows.sort(key=lambda row: abs(float(row["log2_fold_change"])), reverse=True)
        return {
            "summary": (
                f"Computed per-feature log2 fold changes for {group_a_label} vs {group_b_label} across "
                f"{len(dataset['feature_labels'])} feature(s)."
            ),
            "provider": "expression_statistics",
            "analysis": "matrix_group_fold_change_summary",
            "mode": "log2_fold_change_distribution",
            "input": {
                **dataset["input"],
                "summary_stat": summary_token,
                "pseudocount": float(pseudocount),
            },
            "result": {
                "feature_count": len(dataset["feature_labels"]),
                "tested_feature_count": len(dataset["feature_labels"]),
                "summary_stat": summary_token,
                "summary_value": summary_lookup[summary_token],
                "mean": summary_lookup["mean"],
                "median": summary_lookup["median"],
                "q25": summary_lookup["q25"],
                "q75": summary_lookup["q75"],
                "q10": summary_lookup["q10"],
                "q90": summary_lookup["q90"],
            },
            "features": feature_rows,
        }

    def matrix_group_test(
        self,
        *,
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: Literal["rows", "columns"] = "columns",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        metadata_key_column: str = "",
        metadata_group_column: str = "",
        metadata_group_values: list[str] | None = None,
        sample_group_map: dict[str, str] | None = None,
        feature_annotation_path: str = "",
        feature_annotation_table_text: str = "",
        feature_annotation_delimiter: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_skip_rows: int = 0,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "",
        feature_annotation_group_value: str = "",
        test: Literal["t", "welch", "anova", "mannwhitney", "kruskal"] = "anova",
        observation_unit: Literal["flattened_values", "featurewise", "pairwise_log2_fold_change"] = "flattened_values",
        value_transform: Literal["none", "log2p1", "log2"] = "none",
        pseudocount: float = 1.0,
        alpha: float = 0.05,
    ) -> dict[str, Any]:
        dataset = self._prepare_matrix_group_dataset(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            metadata_path=metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            metadata_key_column=metadata_key_column,
            metadata_group_column=metadata_group_column,
            sample_group_map=sample_group_map,
            group_a="",
            group_b="",
            metadata_group_values=metadata_group_values,
            allow_multiple_groups=True,
            feature_annotation_path=feature_annotation_path,
            feature_annotation_table_text=feature_annotation_table_text,
            feature_annotation_delimiter=feature_annotation_delimiter,
            feature_annotation_sheet_name=feature_annotation_sheet_name,
            feature_annotation_header_row=feature_annotation_header_row,
            feature_annotation_skip_rows=feature_annotation_skip_rows,
            feature_annotation_key_column=feature_annotation_key_column,
            feature_annotation_group_column=feature_annotation_group_column,
            feature_annotation_group_value=feature_annotation_group_value,
        )
        effective_test = clean_text(test).lower() or "anova"
        if effective_test not in {"t", "welch", "anova", "mannwhitney", "kruskal"}:
            raise ProviderError("matrix_group_test supports t, welch, anova, mannwhitney, or kruskal.")
        unit = clean_text(observation_unit).lower() or "flattened_values"
        transform = clean_text(value_transform).lower() or "none"
        if unit not in {"flattened_values", "featurewise", "pairwise_log2_fold_change"}:
            raise ProviderError(
                "observation_unit must be 'flattened_values', 'featurewise', or 'pairwise_log2_fold_change'."
            )
        if transform not in {"none", "log2p1", "log2"}:
            raise ProviderError("value_transform must be 'none', 'log2p1', or 'log2'.")
        if float(pseudocount) <= 0:
            raise ProviderError("pseudocount must be greater than zero.")
        values = np.asarray(dataset["values"], dtype=float)
        if transform == "log2p1":
            if np.min(values) < -1:
                raise ProviderError("log2p1 requires matrix values >= -1.")
            values = np.log2(values + 1.0)
        elif transform == "log2":
            if np.min(values) <= 0:
                raise ProviderError("log2 requires matrix values greater than zero.")
            values = np.log2(values)
        groups = list(dataset["input"].get("selected_groups") or [])
        group_labels = np.asarray(dataset["group_labels"], dtype=object)
        grouped = [values[group_labels == label] for label in groups]
        if any(group.size == 0 for group in grouped):
            raise ProviderError("Matrix group test found an empty selected group.")
        if effective_test in {"t", "welch", "mannwhitney"} and len(groups) != 2:
            raise ProviderError(f"{effective_test} requires exactly 2 groups; use ANOVA or Kruskal for 3 or more groups.")

        def run_test(samples: list[np.ndarray]) -> tuple[str, float, float]:
            flattened = [sample.reshape(-1) for sample in samples]
            if effective_test == "t":
                statistic, p_value = stats.ttest_ind(flattened[0], flattened[1], equal_var=True, nan_policy="omit")
                return "t", float(statistic), float(p_value)
            if effective_test == "welch":
                statistic, p_value = stats.ttest_ind(flattened[0], flattened[1], equal_var=False, nan_policy="omit")
                return "t", float(statistic), float(p_value)
            if effective_test == "mannwhitney":
                statistic, p_value = stats.mannwhitneyu(flattened[0], flattened[1], alternative="two-sided")
                return "U", float(statistic), float(p_value)
            if effective_test == "kruskal":
                statistic, p_value = stats.kruskal(*flattened)
                return "H", float(statistic), float(p_value)
            statistic, p_value = stats.f_oneway(*flattened)
            return "F", float(statistic), float(p_value)

        input_block = {
            **dataset["input"],
            "observation_unit": unit,
            "value_transform": transform,
            "test": effective_test,
        }
        if unit == "pairwise_log2_fold_change":
            if transform != "none":
                raise ProviderError("pairwise_log2_fold_change uses log2 ratios and does not accept value_transform.")
            if np.min(values) < 0:
                raise ProviderError("pairwise_log2_fold_change requires non-negative matrix values.")
            pairwise_samples: list[np.ndarray] = []
            pair_labels: list[str] = []
            group_means = {label: np.mean(values[group_labels == label, :], axis=0) for label in groups}
            for left_index, left_group in enumerate(groups):
                for right_group in groups[left_index + 1 :]:
                    pairwise_samples.append(
                        np.log2(
                            (group_means[left_group] + float(pseudocount))
                            / (group_means[right_group] + float(pseudocount))
                        )
                    )
                    pair_labels.append(f"{left_group} vs {right_group}")
            if len(pairwise_samples) < 2:
                raise ProviderError("pairwise_log2_fold_change requires at least two selected groups.")
            statistic_name, statistic, p_value = run_test(pairwise_samples)
            input_block["pseudocount"] = float(pseudocount)
            return {
                "summary": (
                    f"Computed {effective_test} across {len(pairwise_samples)} pairwise log2 fold-change groups."
                ),
                "provider": "expression_statistics",
                "analysis": "matrix_group_test",
                "mode": effective_test,
                "input": input_block,
                "result": {
                    "group_count": len(pairwise_samples),
                    "pair_count": len(pairwise_samples),
                    "observation_count": int(sum(sample.size for sample in pairwise_samples)),
                    "statistic_name": statistic_name,
                    "statistic": statistic,
                    "p_value": p_value,
                },
                "groups": [
                    {
                        "group": label,
                        "count": int(sample.size),
                        "mean": float(np.mean(sample)),
                        "median": float(np.median(sample)),
                        "standard_deviation": float(np.std(sample, ddof=1)) if sample.size > 1 else 0.0,
                    }
                    for label, sample in zip(pair_labels, pairwise_samples, strict=False)
                ],
            }
        if unit == "flattened_values":
            statistic_name, statistic, p_value = run_test(grouped)
            group_summary = [
                {
                    "group": label,
                    "count": int(sample.size),
                    "mean": float(np.mean(sample)),
                    "median": float(np.median(sample)),
                    "standard_deviation": float(np.std(sample, ddof=1)) if sample.size > 1 else 0.0,
                }
                for label, sample in zip(groups, (group.reshape(-1) for group in grouped), strict=False)
            ]
            return {
                "summary": f"Computed {effective_test} across {len(groups)} groups using flattened matrix values.",
                "provider": "expression_statistics",
                "analysis": "matrix_group_test",
                "mode": effective_test,
                "input": input_block,
                "result": {
                    "group_count": len(groups),
                    "observation_count": int(sum(sample.size for sample in grouped)),
                    "statistic_name": statistic_name,
                    "statistic": statistic,
                    "p_value": p_value,
                },
                "groups": group_summary,
            }

        feature_rows: list[dict[str, Any]] = []
        for index, feature in enumerate(dataset["feature_labels"]):
            samples = [values[group_labels == label, index] for label in groups]
            if any(sample.size == 0 for sample in samples):
                continue
            statistic_name, statistic, p_value = run_test(samples)
            feature_rows.append(
                {
                    "feature": feature,
                    "group_counts": {label: int(sample.size) for label, sample in zip(groups, samples, strict=False)},
                    "group_means": {label: float(np.mean(sample)) for label, sample in zip(groups, samples, strict=False)},
                    "statistic_name": statistic_name,
                    "statistic": statistic,
                    "p_value": p_value if np.isfinite(p_value) else 1.0,
                }
            )
        if not feature_rows:
            raise ProviderError("matrix_group_test did not find any testable features.")
        p_values = np.asarray([row["p_value"] for row in feature_rows], dtype=float)
        padj = self._adjust_p_values(p_values, method="bh")
        for row, adjusted in zip(feature_rows, padj, strict=False):
            row["padj_bh"] = float(adjusted)
            row["significant_bh"] = bool(float(adjusted) <= alpha)
        feature_rows.sort(key=lambda row: (row["padj_bh"], row["p_value"], row["feature"]))
        return {
            "summary": f"Computed featurewise {effective_test} across {len(feature_rows)} feature(s) and {len(groups)} group(s).",
            "provider": "expression_statistics",
            "analysis": "matrix_group_test",
            "mode": effective_test,
            "input": input_block,
            "result": {
                "feature_count": len(dataset["feature_labels"]),
                "tested_feature_count": len(feature_rows),
                "significant_feature_count_bh": sum(bool(row["significant_bh"]) for row in feature_rows),
            },
            "features": feature_rows,
        }

    def matrix_log2_fold_change_distribution(
        self,
        *,
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: Literal["rows", "columns"] = "columns",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        metadata_key_column: str = "",
        metadata_group_column: str = "",
        metadata_group_values: list[str] | None = None,
        feature_annotation_path: str = "",
        feature_annotation_table_text: str = "",
        feature_annotation_delimiter: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_skip_rows: int = 0,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "",
        feature_annotation_group_value: str = "",
        pseudocount: float = 1.0,
    ) -> dict[str, Any]:
        dataset = self._prepare_matrix_group_dataset(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            metadata_path=metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            metadata_key_column=metadata_key_column,
            metadata_group_column=metadata_group_column,
            group_a="",
            group_b="",
            metadata_group_values=metadata_group_values,
            allow_multiple_groups=True,
            feature_annotation_path=feature_annotation_path,
            feature_annotation_table_text=feature_annotation_table_text,
            feature_annotation_delimiter=feature_annotation_delimiter,
            feature_annotation_sheet_name=feature_annotation_sheet_name,
            feature_annotation_header_row=feature_annotation_header_row,
            feature_annotation_skip_rows=feature_annotation_skip_rows,
            feature_annotation_key_column=feature_annotation_key_column,
            feature_annotation_group_column=feature_annotation_group_column,
            feature_annotation_group_value=feature_annotation_group_value,
        )
        values = np.asarray(dataset["values"], dtype=float)
        if np.min(values) < 0 or float(pseudocount) <= 0:
            raise ProviderError("matrix_log2_fold_change_distribution requires non-negative values and pseudocount > 0.")
        groups = list(dataset["input"].get("selected_groups") or [])
        labels = np.asarray(dataset["group_labels"], dtype=object)
        means = {group: np.mean(values[labels == group, :], axis=0) for group in groups}
        distributions: list[np.ndarray] = []
        pair_count = 0
        for left_index, left_group in enumerate(groups):
            for right_group in groups[left_index + 1 :]:
                distributions.append(np.log2((means[left_group] + float(pseudocount)) / (means[right_group] + float(pseudocount))))
                pair_count += 1
        changes = np.concatenate(distributions) if distributions else np.asarray([], dtype=float)
        if changes.size == 0:
            raise ProviderError("matrix_log2_fold_change_distribution produced no pairwise values.")
        skewness = float(stats.skew(changes, bias=False)) if changes.size > 2 else 0.0
        if not np.isfinite(skewness):
            skewness = 0.0
        if abs(skewness) <= 0.3:
            distribution_label = "Normal"
        elif skewness > 0:
            distribution_label = "Right-skewed"
        else:
            distribution_label = "Left-skewed"
        return {
            "summary": f"Computed {changes.size} pairwise log2 fold changes across {pair_count} group pair(s).",
            "provider": "expression_statistics",
            "analysis": "matrix_log2_fold_change_distribution",
            "mode": "all_pairwise",
            "input": {**dataset["input"], "pseudocount": float(pseudocount)},
            "result": {
                "feature_count": int(values.shape[1]),
                "pair_count": pair_count,
                "value_count": int(changes.size),
                "mean": float(np.mean(changes)),
                "median": float(np.median(changes)),
                "standard_deviation": float(np.std(changes, ddof=1)) if changes.size > 1 else 0.0,
                "skewness": skewness,
                "distribution_label": distribution_label,
            },
        }

    def proportion_interval(
        self,
        *,
        success_count: int,
        trial_count: int,
        method: Literal["wilson"] = "wilson",
        alpha: float = 0.05,
    ) -> dict[str, Any]:
        if method != "wilson":
            raise ProviderError("Only Wilson proportion intervals are supported.")
        trials = int(trial_count)
        successes = int(success_count)
        if trials <= 0:
            raise ProviderError("Wilson interval requires trial_count > 0.")
        if successes < 0 or successes > trials:
            raise ProviderError("Wilson interval requires 0 <= success_count <= trial_count.")
        z = NormalDist().inv_cdf(1.0 - alpha / 2.0)
        p_hat = successes / trials
        denominator = 1.0 + z**2 / trials
        center = (p_hat + z**2 / (2 * trials)) / denominator
        margin = (z / denominator) * math.sqrt((p_hat * (1.0 - p_hat) / trials) + (z**2 / (4 * trials**2)))
        return {
            "summary": f"Computed Wilson interval for {successes}/{trials}.",
            "provider": "expression_statistics",
            "analysis": "proportion_interval",
            "mode": method,
            "input": {
                "success_count": successes,
                "trial_count": trials,
                "alpha": float(alpha),
            },
            "result": {
                "proportion": float(p_hat),
                "confidence_level": float(1.0 - alpha),
                "lower_bound": float(max(0.0, center - margin)),
                "upper_bound": float(min(1.0, center + margin)),
            },
        }

    def exact_binomial_test(
        self,
        *,
        success_count: int,
        trial_count: int,
        null_probability: float = 0.5,
        alpha: float = 0.05,
    ) -> dict[str, Any]:
        successes = int(success_count)
        trials = int(trial_count)
        probability = float(null_probability)
        if trials <= 0:
            raise ProviderError("Exact binomial test requires trial_count > 0.")
        if successes < 0 or successes > trials:
            raise ProviderError("Exact binomial test requires 0 <= success_count <= trial_count.")
        if not (0.0 <= probability <= 1.0):
            raise ProviderError("Exact binomial test requires 0 <= null_probability <= 1.")

        result = stats.binomtest(successes, trials, p=probability, alternative="two-sided")
        try:
            ci = result.proportion_ci(confidence_level=1.0 - alpha, method="exact")
            lower_bound = float(ci.low)
            upper_bound = float(ci.high)
        except Exception:  # pragma: no cover - SciPy compatibility fallback
            interval = self.proportion_interval(
                success_count=successes,
                trial_count=trials,
                alpha=alpha,
            )
            lower_bound = float(interval["result"]["lower_bound"])
            upper_bound = float(interval["result"]["upper_bound"])

        observed_rate = successes / trials
        return {
            "summary": f"Computed an exact two-sided binomial test for {successes}/{trials}.",
            "provider": "expression_statistics",
            "analysis": "exact_binomial_test",
            "mode": "two-sided",
            "input": {
                "success_count": successes,
                "trial_count": trials,
                "null_probability": probability,
                "alpha": float(alpha),
            },
            "result": {
                "success_count": successes,
                "trial_count": trials,
                "null_probability": probability,
                "observed_rate": float(observed_rate),
                "p_value": float(result.pvalue),
                "confidence_interval": {
                    "confidence_level": float(1.0 - alpha),
                    "lower_bound": lower_bound,
                    "upper_bound": upper_bound,
                },
            },
        }

    def multiple_testing_summary(
        self,
        *,
        source_path: str = "",
        table_text: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        p_value_column: str = "",
        alpha: float = 0.05,
        methods: list[str] | None = None,
    ) -> dict[str, Any]:
        table = load_table(
            path=source_path,
            text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        resolved_column = self._resolve_column(table.frame, p_value_column, required=True, label="p-value")
        raw_series = pd.to_numeric(table.frame[resolved_column], errors="coerce")
        raw_values = raw_series.dropna().to_numpy(dtype=float)
        if raw_values.size == 0:
            raise ProviderError("Multiple-testing summary requires at least one usable p-value.")
        active_methods = self._normalize_methods(methods or ["bh", "by", "bonferroni"])
        adjusted_by_method = {
            method: self._adjust_p_values(raw_values, method=method)
            for method in active_methods
        }
        row_preview = []
        significant_under_all = 0
        for index, p_value in enumerate(raw_values):
            adjusted_values = {method: float(values[index]) for method, values in adjusted_by_method.items()}
            if adjusted_values and all(value <= alpha for value in adjusted_values.values()):
                significant_under_all += 1
            row_preview.append(
                {
                    "row_index": index + 1,
                    "p_value": float(p_value),
                    "adjusted_p_values": adjusted_values,
                    "significant_methods": [
                        method for method, value in adjusted_values.items() if value <= alpha
                    ],
                }
            )
        method_rows = []
        for token, adjusted in adjusted_by_method.items():
            method_rows.append(
                {
                    "method": token,
                    "significant_count": int(np.sum(adjusted <= alpha)),
                    "adjusted_p_values_preview": [float(value) for value in adjusted[: min(10, adjusted.size)]],
                }
            )
        return {
            "summary": f"Compared {len(active_methods)} multiple-testing correction method(s) across {raw_values.size} p-value(s).",
            "provider": "expression_statistics",
            "analysis": "multiple_testing_summary",
            "input": {
                "source_path": table.path,
                "source_kind": table.source_kind,
                "available_sheets": table.available_sheets or [],
                "p_value_column": resolved_column,
                "alpha": float(alpha),
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
            },
            "result": {
                "value_count": int(raw_values.size),
                "significant_under_all_methods_count": int(significant_under_all),
                "method_count": len(active_methods),
            },
            "methods": method_rows,
            "rows": row_preview,
        }

    def threshold_count(
        self,
        *,
        source_path: str = "",
        table_text: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        value_column: str = "",
        threshold: float = 0.0,
        operator: str = "<",
    ) -> dict[str, Any]:
        """Count finite values satisfying a scalar threshold in a result column."""
        table = load_table(
            path=source_path,
            text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        resolved_column = self._resolve_column(table.frame, value_column, required=True, label="value")
        values = pd.to_numeric(table.frame[resolved_column], errors="coerce")
        finite = values[np.isfinite(values.to_numpy(dtype=float, na_value=np.nan))]
        threshold_value = float(threshold)
        operators = {
            "<": lambda series: series < threshold_value,
            "<=": lambda series: series <= threshold_value,
            ">": lambda series: series > threshold_value,
            ">=": lambda series: series >= threshold_value,
        }
        operator_token = clean_text(operator) or "<"
        if operator_token not in operators:
            raise ProviderError("threshold operator must be one of '<', '<=', '>', or '>='.")
        mask = operators[operator_token](finite)
        count = int(mask.sum())
        tested_count = int(finite.size)
        return {
            "summary": (
                f"Counted {count} of {tested_count} finite value(s) in {resolved_column!r} "
                f"satisfying {resolved_column} {operator_token} {threshold_value}."
            ),
            "provider": "expression_statistics",
            "analysis": "threshold_count",
            "input": {
                "source_path": table.path,
                "source_kind": table.source_kind,
                "value_column": resolved_column,
                "threshold": threshold_value,
                "operator": operator_token,
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
            },
            "result": {
                "count": count,
                "tested_count": tested_count,
                "missing_count": int(values.size - tested_count),
                "fraction": float(count / tested_count) if tested_count else None,
            },
        }

    def _aggregate_matrix(
        self,
        matrix: LoadedMatrix,
        *,
        analysis_axis: Literal["rows", "columns"],
        target_axis: Literal["features", "observations"],
        summary_stat: Literal["mean", "median"],
    ) -> tuple[list[str], np.ndarray]:
        prepared = self.expression_matrix_backend._observations(matrix, analysis_axis=analysis_axis)
        if target_axis == "features":
            labels = list(prepared.feature_labels)
            values = np.median(prepared.values, axis=0) if summary_stat == "median" else np.mean(prepared.values, axis=0)
        else:
            labels = list(prepared.labels)
            values = np.median(prepared.values, axis=1) if summary_stat == "median" else np.mean(prepared.values, axis=1)
        return labels, np.asarray(values, dtype=float)

    def _prepare_matrix_group_dataset(
        self,
        *,
        matrix_path: str,
        table_text: str,
        row_id_column: str,
        delimiter: str,
        sheet_name: str,
        header_row: int,
        skip_rows: int,
        transpose: bool,
        analysis_axis: Literal["rows", "columns"],
        metadata_path: str,
        metadata_table_text: str,
        metadata_delimiter: str,
        metadata_sheet_name: str,
        metadata_header_row: int,
        metadata_skip_rows: int,
        metadata_key_column: str,
        metadata_group_column: str,
        sample_group_map: dict[str, str] | None = None,
        group_a: str,
        group_b: str,
        metadata_group_values: list[str] | None = None,
        allow_multiple_groups: bool = False,
        feature_annotation_path: str,
        feature_annotation_table_text: str,
        feature_annotation_delimiter: str,
        feature_annotation_sheet_name: str,
        feature_annotation_header_row: int,
        feature_annotation_skip_rows: int,
        feature_annotation_key_column: str,
        feature_annotation_group_column: str,
        feature_annotation_group_value: str,
        missing_value_policy: Literal["error", "omit", "drop_feature"] = "error",
    ) -> dict[str, Any]:
        resolved_row_id_column, resolution_notes = self._resolve_matrix_row_id_column(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
        )
        matrix_source = self.expression_matrix_backend._load_matrix_source(
            matrix_path=matrix_path,
            table_text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
        )
        matrix_frame = matrix_source.frame.copy()
        if matrix_frame.empty:
            raise ProviderError("Expression matrix analysis requires a non-empty table.")
        if resolved_row_id_column not in matrix_frame.columns:
            available = self._available_columns_preview(matrix_frame)
            raise ProviderError(
                f"Matrix row id column {resolved_row_id_column!r} was not found. Available columns include: {available}."
            )

        feature_labels = [
            clean_text(value) or f"row_{index + 1}"
            for index, value in enumerate(matrix_frame[resolved_row_id_column].tolist())
        ]
        if len(set(feature_labels)) != len(feature_labels):
            raise ProviderError("Matrix row identifiers must be unique.")
        matrix_value_frame = matrix_frame.drop(columns=[resolved_row_id_column], errors="ignore")
        if matrix_value_frame.empty:
            raise ProviderError("Matrix group analyses require at least one numeric sample column.")

        normalized_axis = self.expression_matrix_backend.normalize_analysis_axis(analysis_axis)
        policy = clean_text(missing_value_policy).lower() or "error"
        if policy not in {"error", "omit", "drop_feature"}:
            raise ProviderError("missing_value_policy must be 'error', 'omit', or 'drop_feature'.")
        use_metadata_subset = normalized_axis == "columns" and not transpose
        prepared_labels = list(matrix_value_frame.columns) if use_metadata_subset else []
        prepared_values: np.ndarray | None = None
        matrix_info = {
            "matrix_path": matrix_source.path,
            "matrix_source_kind": matrix_source.source_kind,
            "matrix_available_sheets": matrix_source.available_sheets or [],
            "row_id_column": resolved_row_id_column,
            "missing_value_policy": policy,
        }
        if not use_metadata_subset:
            matrix = self.expression_matrix_backend.load_matrix(
                matrix_path=matrix_path,
                table_text=table_text,
                row_id_column=resolved_row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=analysis_axis,
            )
            prepared = self.expression_matrix_backend._observations(matrix, analysis_axis=analysis_axis)
            prepared_labels = list(prepared.labels)
            feature_labels = list(prepared.feature_labels)
            prepared_values = np.asarray(prepared.values, dtype=float)
            matrix_info = {
                "matrix_path": matrix.path,
                "matrix_source_kind": matrix.source_kind,
                "matrix_available_sheets": matrix.available_sheets or [],
                "row_id_column": matrix.row_id_column,
            }
        metadata = None
        key_column = ""
        group_column = ""
        requested_group_values = [
            clean_text(value)
            for value in (metadata_group_values or [group_a, group_b])
            if clean_text(value)
        ]
        metadata_map: dict[str, str] = {}
        if metadata_path or metadata_table_text:
            metadata = load_table(
                path=metadata_path,
                text=metadata_table_text,
                delimiter=metadata_delimiter,
                sheet_name=metadata_sheet_name,
                header_row=metadata_header_row,
                skip_rows=metadata_skip_rows,
                dtype=str,
                keep_default_na=False,
            )
            key_column, key_notes = self._resolve_best_overlap_key_column(
                metadata.frame,
                metadata_key_column,
                labels=list(prepared_labels),
                label="metadata key",
                preferred_tokens=("sample", "sampleid", "sample_number", "name", "id", "code"),
            )
            resolution_notes.extend(key_notes)
            group_column, group_notes = self._resolve_group_column_with_inference(
                metadata.frame,
                metadata_group_column,
                label="metadata group",
                requested_values=requested_group_values,
                exclude_columns=[key_column],
                required=True,
            )
            resolution_notes.extend(group_notes)
            metadata_frame = metadata.frame[[key_column, group_column]].copy()
            metadata_frame[key_column] = metadata_frame[key_column].map(clean_text)
            metadata_frame[group_column] = metadata_frame[group_column].map(clean_text)
            metadata_frame = metadata_frame[
                (metadata_frame[key_column].astype(str).str.len() > 0)
                & (metadata_frame[group_column].astype(str).str.len() > 0)
            ].copy()
            if metadata_frame.empty:
                raise ProviderError("Matrix group analyses require non-empty sample metadata with sample ids and groups.")

            for sample_id, group_value in zip(metadata_frame[key_column], metadata_frame[group_column], strict=False):
                token = self._canonical_token(sample_id)
                if not token:
                    continue
                previous = metadata_map.get(token)
                if previous is not None and previous != group_value:
                    raise ProviderError(f"Sample metadata contains conflicting group labels for sample '{sample_id}'.")
                metadata_map[token] = group_value
        else:
            metadata_map = self._resolve_sample_group_map(sample_group_map, prepared_labels)
            if not metadata_map:
                metadata_map = self._infer_sample_group_map(prepared_labels)
                if metadata_map:
                    resolution_notes.append("Inferred sample groups from sample labels; verify the returned group mapping.")
            if not metadata_map:
                raise ProviderError(
                    "Matrix group analyses require metadata_path or sample_group_map. "
                    "No unambiguous sample-group pattern was found in the matrix column labels."
                )

        group_labels: list[str] = []
        sample_labels: list[str] = []
        for label in prepared_labels:
            token = self._canonical_token(label)
            if token not in metadata_map:
                continue
            sample_labels.append(label)
            group_labels.append(metadata_map[token])
        if len(sample_labels) < 2:
            raise ProviderError("Matrix group analyses require at least 2 samples aligned between the matrix and metadata.")

        if use_metadata_subset:
            numeric_frame = matrix_value_frame[sample_labels].apply(pd.to_numeric, errors="coerce")
            if numeric_frame.isna().any().any():
                bad_cells = int(numeric_frame.isna().sum().sum())
                bad_columns = numeric_frame.columns[numeric_frame.isna().any()].tolist()[:8]
                matrix_info["missing_value_count"] = bad_cells
                matrix_info["missing_value_columns_preview"] = [str(column) for column in bad_columns]
                if policy == "error":
                    raise ProviderError(
                        "Matrix group analyses found non-numeric values after restricting to metadata-aligned sample columns. "
                        f"Bad cells: {bad_cells}. Example columns: {', '.join(map(repr, bad_columns)) or 'none'}. "
                        "For continuous/qPCR matrices, use missing_value_policy='omit' or 'drop_feature' when justified."
                    )
                if policy == "drop_feature":
                    keep_features = ~numeric_frame.isna().any(axis=1)
                    numeric_frame = numeric_frame.loc[keep_features].copy()
                    feature_labels = [
                        feature
                        for feature, keep in zip(feature_labels, keep_features.tolist(), strict=False)
                        if keep
                    ]
                    matrix_info["dropped_feature_count"] = int((~keep_features).sum())
                    if numeric_frame.empty:
                        raise ProviderError("missing_value_policy='drop_feature' removed every feature from the matrix.")
            prepared_values = np.asarray(numeric_frame.to_numpy(dtype=float).T, dtype=float)

        available_groups = unique_preserve_order(group_labels)
        if allow_multiple_groups:
            if requested_group_values:
                selected_groups = [self._resolve_group_value(value, available_groups) for value in requested_group_values]
                if any(not value for value in selected_groups):
                    raise ProviderError(
                        "Matrix group analysis could not resolve all requested metadata groups. "
                        f"Available groups: {', '.join(available_groups)}."
                    )
            else:
                selected_groups = list(available_groups)
            selected_groups = unique_preserve_order(selected_groups)
            if len(selected_groups) < 2:
                raise ProviderError("Matrix group analysis requires at least 2 selected groups.")
            resolved_group_a, resolved_group_b = selected_groups[:2]
        else:
            resolved_group_a = self._resolve_group_value(group_a, available_groups)
            resolved_group_b = self._resolve_group_value(group_b, available_groups)
            if not resolved_group_a or not resolved_group_b:
                if len(available_groups) != 2:
                    raise ProviderError(
                        "Matrix group analyses require explicit group_a and group_b when the metadata contains more than 2 groups. "
                        f"Available groups: {', '.join(available_groups)}."
                    )
                resolved_group_a, resolved_group_b = available_groups[0], available_groups[1]
            if resolved_group_a == resolved_group_b:
                raise ProviderError("group_a and group_b must refer to different groups.")
            selected_groups = [resolved_group_a, resolved_group_b]

        selected_positions = [
            offset
            for offset, label in enumerate(group_labels)
            if label in set(selected_groups)
        ]
        selected_group_labels = np.asarray([group_labels[offset] for offset in selected_positions], dtype=object)
        if prepared_values is None:
            raise ProviderError("Matrix group analyses could not prepare the numeric matrix.")
        selected_values = np.asarray(prepared_values, dtype=float)[selected_positions, :]
        if selected_values.shape[0] < 2:
            raise ProviderError("Matrix group analyses require at least 2 selected samples after group filtering.")
        feature_filter_column = clean_text(feature_annotation_group_column)
        feature_filter_value = clean_text(feature_annotation_group_value)
        filtered_feature_labels = feature_labels
        filtered_values = selected_values
        feature_annotation_available_sheets: list[str] = []
        if feature_annotation_path or feature_annotation_table_text:
            feature_annotation = load_table(
                path=feature_annotation_path,
                text=feature_annotation_table_text,
                delimiter=feature_annotation_delimiter,
                sheet_name=feature_annotation_sheet_name,
                header_row=feature_annotation_header_row,
                skip_rows=feature_annotation_skip_rows,
                dtype=str,
                keep_default_na=False,
            )
            feature_annotation_available_sheets = feature_annotation.available_sheets or []
            if feature_filter_column and feature_filter_value:
                annotation_key = self._resolve_column(
                    feature_annotation.frame,
                    feature_annotation_key_column,
                    required=False,
                    label="feature annotation key",
                )
                if not annotation_key:
                    annotation_key, feature_key_notes = self._resolve_overlap_key_column(
                        feature_annotation.frame,
                        feature_annotation_key_column,
                        labels=feature_labels,
                        label="feature annotation key",
                        preferred_tokens=("gene", "geneid", "symbol", "ensembl", "feature", "id"),
                    )
                    resolution_notes.extend(feature_key_notes)
                annotation_group = self._resolve_column(
                    feature_annotation.frame,
                    feature_filter_column,
                    required=True,
                    label="feature annotation group",
                )
                annotation_frame = feature_annotation.frame[[annotation_key, annotation_group]].copy()
                annotation_frame[annotation_key] = annotation_frame[annotation_key].map(clean_text)
                annotation_frame[annotation_group] = annotation_frame[annotation_group].map(clean_text)
                allowed_tokens = {
                    self._canonical_token(key)
                    for key, value in zip(annotation_frame[annotation_key], annotation_frame[annotation_group], strict=False)
                    if clean_text(value) == feature_filter_value and self._canonical_token(key)
                }
                keep_indices = [
                    index
                    for index, label in enumerate(feature_labels)
                    if self._canonical_token(label) in allowed_tokens
                ]
                if not keep_indices:
                    raise ProviderError(
                        "Matrix group analyses feature annotation filter matched no matrix features. "
                        f"feature_annotation_group_column={annotation_group!r}, "
                        f"feature_annotation_group_value={feature_filter_value!r}."
                    )
                filtered_feature_labels = [feature_labels[index] for index in keep_indices]
                filtered_values = selected_values[:, keep_indices]

        return {
            "values": filtered_values,
            "feature_labels": filtered_feature_labels,
            "group_labels": selected_group_labels,
            "group_a": resolved_group_a,
            "group_b": resolved_group_b,
            "input": {
                "matrix_path": matrix_info["matrix_path"],
                "matrix_source_kind": matrix_info["matrix_source_kind"],
                "matrix_available_sheets": matrix_info["matrix_available_sheets"],
                "metadata_path": metadata.path if metadata is not None else None,
                "metadata_source_kind": metadata.source_kind if metadata is not None else "sample_group_map",
                "metadata_available_sheets": metadata.available_sheets or [] if metadata is not None else [],
                "analysis_axis": analysis_axis,
                "row_id_column": matrix_info["row_id_column"],
                "missing_value_policy": matrix_info["missing_value_policy"],
                "missing_value_count": int(matrix_info.get("missing_value_count") or 0),
                "dropped_feature_count": int(matrix_info.get("dropped_feature_count") or 0),
                "sample_count": int(filtered_values.shape[0]),
                "feature_count": int(filtered_values.shape[1]),
                "metadata_key_column": key_column,
                "metadata_group_column": group_column,
                "sample_group_map": {
                    str(label): str(metadata_map[self._canonical_token(label)])
                    for label in prepared_labels
                    if self._canonical_token(label) in metadata_map
                },
                "group_a": resolved_group_a,
                "group_b": resolved_group_b,
                "selected_groups": selected_groups,
                "matched_sample_count": len(sample_labels),
                "available_groups": available_groups,
                "feature_annotation_group_column": feature_filter_column or None,
                "feature_annotation_group_value": feature_filter_value or None,
                "feature_annotation_available_sheets": feature_annotation_available_sheets,
                "resolution_notes": resolution_notes,
            },
        }

    @staticmethod
    def _canonical_token(value: str) -> str:
        return "".join(ch for ch in clean_text(value).lower() if ch.isalnum())

    @classmethod
    def _resolve_sample_group_map(
        cls,
        sample_group_map: dict[str, str] | None,
        labels: list[str],
    ) -> dict[str, str]:
        if not isinstance(sample_group_map, dict):
            return {}
        normalized: dict[str, str] = {}
        for sample, group in sample_group_map.items():
            sample_token = cls._canonical_token(sample)
            group_value = clean_text(group)
            if sample_token and group_value:
                normalized[sample_token] = group_value
        matched = {cls._canonical_token(label): normalized[cls._canonical_token(label)] for label in labels if cls._canonical_token(label) in normalized}
        return matched if len(matched) >= 2 else {}

    @classmethod
    def _infer_sample_group_map(cls, labels: list[str]) -> dict[str, str]:
        """Infer only conservative prefix-number groups such as P_1 and C_11."""
        inferred: dict[str, str] = {}
        for label in labels:
            token = clean_text(label)
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)[_-]?[0-9]+", token)
            if not match:
                return {}
            group = clean_text(match.group(1))
            canonical = cls._canonical_token(token)
            if not canonical or not group:
                return {}
            inferred[canonical] = group
        groups = unique_preserve_order(inferred.values())
        return inferred if len(inferred) == len(labels) and len(groups) >= 2 else {}

    @classmethod
    def _resolve_group_value(cls, requested: str, available: list[str]) -> str:
        token = clean_text(requested)
        if not token:
            return ""
        if token in available:
            return token
        lowered = token.lower()
        for candidate in available:
            if candidate.lower() == lowered:
                return candidate
        canonical = cls._canonical_token(token)
        canonical_matches = [candidate for candidate in available if cls._canonical_token(candidate) == canonical]
        if len(canonical_matches) == 1:
            return canonical_matches[0]
        return ""

    @classmethod
    def _resolve_column(cls, frame: pd.DataFrame, requested: str, *, required: bool, label: str) -> str:
        token = clean_text(requested)
        if token and token in frame.columns:
            return token
        if token:
            lowered = token.lower()
            lowercase_matches = [str(column) for column in frame.columns if str(column).lower() == lowered]
            if len(lowercase_matches) == 1:
                return lowercase_matches[0]
            canonical = cls._canonical_token(token)
            canonical_matches = [
                str(column)
                for column in frame.columns
                if cls._canonical_token(str(column)) == canonical
            ]
            if len(canonical_matches) == 1:
                return canonical_matches[0]
        if required:
            available = cls._available_columns_preview(frame)
            raise ProviderError(
                f"Expression statistics requires a valid {label} column. Available columns include: {available}."
            )
        return ""

    @classmethod
    def _infer_overlap_column(
        cls,
        frame: pd.DataFrame,
        *,
        labels: list[str],
        preferred_tokens: tuple[str, ...] = (),
    ) -> str:
        label_tokens = {cls._canonical_token(label) for label in labels if cls._canonical_token(label)}
        if len(label_tokens) < 2:
            return ""

        ranked: list[tuple[int, int, int, str]] = []
        for index, column in enumerate(frame.columns):
            column_name = str(column)
            column_series = frame.iloc[:, index]
            values = {
                cls._canonical_token(value)
                for value in column_series.tolist()
                if cls._canonical_token(value)
            }
            overlap = len(values & label_tokens)
            if overlap < 2:
                continue
            column_token = cls._canonical_token(column_name)
            token_bonus = 2 if any(token in column_token for token in preferred_tokens) else 0
            identifier_bonus = 1 if cls._column_can_be_identifier(column_series) else 0
            ranked.append((overlap, token_bonus, identifier_bonus, f"{index}:{column_name}"))
        if not ranked:
            return ""
        ranked.sort(reverse=True)
        best_overlap, best_token_bonus, best_identifier_bonus, best_tag = ranked[0]
        if len(ranked) > 1 and ranked[1][:3] == (best_overlap, best_token_bonus, best_identifier_bonus):
            return ""
        return best_tag.split(":", 1)[1]

    @classmethod
    def _infer_group_column(
        cls,
        frame: pd.DataFrame,
        *,
        requested_values: list[str],
        exclude_columns: list[str],
    ) -> str:
        excluded = {clean_text(column) for column in exclude_columns if clean_text(column)}
        ranked: list[tuple[int, int, str]] = []
        for index, column in enumerate(frame.columns):
            column_name = str(column)
            if column_name in excluded:
                continue
            column_series = frame.iloc[:, index]
            values = [clean_text(value) for value in column_series.tolist() if clean_text(value)]
            if not values:
                continue
            unique_values = unique_preserve_order(values)
            if len(unique_values) < 2 or len(unique_values) > 20:
                continue
            if all(cls._is_numeric_like(value) for value in unique_values):
                continue
            match_score = cls._group_column_match_score_from_values(unique_values, requested_values)
            if match_score <= 0:
                continue
            ranked.append((match_score, -index, column_name))
        if not ranked:
            return ""
        ranked.sort(reverse=True)
        return ranked[0][2]

    @classmethod
    def _group_column_match_score(cls, series: pd.Series, requested_values: list[str]) -> int:
        values = unique_preserve_order([clean_text(value) for value in series.tolist() if clean_text(value)])
        return cls._group_column_match_score_from_values(values, requested_values)

    @classmethod
    def _group_column_match_score_from_values(cls, values: list[str], requested_values: list[str]) -> int:
        requested_tokens = {cls._canonical_token(value) for value in requested_values if cls._canonical_token(value)}
        if not requested_tokens:
            return 0
        value_tokens = {cls._canonical_token(value) for value in values if cls._canonical_token(value)}
        matched = len(requested_tokens & value_tokens)
        if matched == 0:
            return 0
        return matched * 10 + max(0, 6 - len(value_tokens))

    @classmethod
    def _column_overlap_count(cls, series: pd.Series, labels: list[str]) -> int:
        label_tokens = {cls._canonical_token(label) for label in labels if cls._canonical_token(label)}
        value_tokens = {
            cls._canonical_token(value)
            for value in series.tolist()
            if cls._canonical_token(value)
        }
        return len(label_tokens & value_tokens)

    @classmethod
    def _requested_identifier_like(cls, requested: str) -> bool:
        token = cls._canonical_token(requested)
        if not token:
            return False
        return any(marker in token for marker in ("gene", "feature", "symbol", "ensembl", "identifier", "id"))

    @classmethod
    def _requested_blank_header_alias(cls, requested: str) -> bool:
        token = cls._canonical_token(requested)
        if not token:
            return False
        return token.startswith("unnamed") or token in {"column1", "column01", "x", "index"}

    @staticmethod
    def _column_can_be_identifier(series: pd.Series) -> bool:
        values = [clean_text(value) for value in series.tolist()]
        nonempty = [value for value in values if value]
        if len(nonempty) < 3:
            return False
        unique_ratio = len(set(nonempty)) / float(len(nonempty))
        return unique_ratio >= 0.9

    @staticmethod
    def _is_numeric_like(value: object) -> bool:
        token = clean_text(value)
        if not token:
            return False
        try:
            float(token)
        except ValueError:
            return False
        return True

    @staticmethod
    def _available_columns_preview(frame: pd.DataFrame, *, limit: int = 8) -> str:
        columns = [repr(str(column)) for column in list(frame.columns)[:limit]]
        if len(frame.columns) > limit:
            columns.append(f"... (+{len(frame.columns) - limit} more)")
        return ", ".join(columns) or "none"

    @staticmethod
    def _correlate(left: np.ndarray, right: np.ndarray, *, method: Literal["pearson", "spearman"]) -> tuple[float, float]:
        if left.shape != right.shape:
            raise ProviderError("Correlation requires aligned vectors with matching length.")
        if left.size < 3:
            raise ProviderError("Correlation requires at least 3 aligned values.")
        if method == "spearman":
            correlation, p_value = stats.spearmanr(left, right)
        else:
            correlation, p_value = stats.pearsonr(left, right)
        if not math.isfinite(float(correlation)):
            raise ProviderError("Correlation could not be computed for the supplied values.")
        return float(correlation), float(p_value)

    @staticmethod
    def _skewness_label(skewness: float) -> str:
        if skewness >= 1.0:
            return "highly right skewed"
        if 0.3 <= skewness < 1.0:
            return "minimally right skewed"
        if -1.0 <= skewness <= -0.3:
            return "minimally left skewed"
        if skewness < -1.0:
            return "highly left skewed"
        return "approximately normal"

    @staticmethod
    def _normalize_methods(methods: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        mapping = {
            "bh": "bh",
            "fdr_bh": "bh",
            "benjamini-hochberg": "bh",
            "benjamini_hochberg": "bh",
            "by": "by",
            "benjamini-yekutieli": "by",
            "benjamini_yekutieli": "by",
            "bonferroni": "bonferroni",
        }
        for method in methods:
            token = mapping.get(clean_text(method).lower(), "")
            if token and token not in seen:
                seen.add(token)
                normalized.append(token)
        if not normalized:
            raise ProviderError("Multiple-testing summary requires at least one supported correction method.")
        return normalized

    @staticmethod
    def _adjust_p_values(values: np.ndarray, *, method: str) -> np.ndarray:
        p_values = np.asarray(values, dtype=float)
        n = p_values.size
        if method == "bonferroni":
            return np.clip(p_values * n, 0.0, 1.0)
        order = np.argsort(p_values)
        ranked = np.empty_like(p_values)
        harmonic = sum(1.0 / index for index in range(1, n + 1)) if method == "by" else 1.0
        running = 1.0
        for offset, index in enumerate(order[::-1], start=1):
            rank = n - offset + 1
            value = p_values[index] * n * harmonic / max(1, rank)
            running = min(running, value)
            ranked[index] = min(1.0, running)
        return ranked


def _eta_squared(group_values: dict[str, np.ndarray]) -> float:
    groups = [np.asarray(values, dtype=float) for values in group_values.values() if np.asarray(values, dtype=float).size > 0]
    if len(groups) < 2:
        return 0.0
    combined = np.concatenate(groups)
    grand_mean = float(np.mean(combined))
    ss_between = 0.0
    ss_total = float(np.sum((combined - grand_mean) ** 2))
    if ss_total <= 0:
        return 0.0
    for values in groups:
        ss_between += float(values.size) * float((np.mean(values) - grand_mean) ** 2)
    return float(max(0.0, min(1.0, ss_between / ss_total)))


def _cohen_d(left: np.ndarray, right: np.ndarray) -> float:
    first = np.asarray(left, dtype=float)
    second = np.asarray(right, dtype=float)
    first = first[np.isfinite(first)]
    second = second[np.isfinite(second)]
    if first.size == 0 or second.size == 0:
        return 0.0
    if first.size == 1 and second.size == 1:
        return float(first[0] - second[0])
    variance_first = float(np.var(first, ddof=1)) if first.size > 1 else 0.0
    variance_second = float(np.var(second, ddof=1)) if second.size > 1 else 0.0
    denominator = max(first.size + second.size - 2, 1)
    pooled_variance = ((first.size - 1) * variance_first + (second.size - 1) * variance_second) / denominator
    if pooled_variance <= 0 or not np.isfinite(pooled_variance):
        return float(np.mean(first) - np.mean(second))
    return float((np.mean(first) - np.mean(second)) / math.sqrt(pooled_variance))


def _epsilon_squared(statistic: float, n_obs: int, n_groups: int) -> float:
    if n_obs <= n_groups or n_groups < 2:
        return 0.0
    value = (float(statistic) - n_groups + 1.0) / (n_obs - n_groups)
    if not np.isfinite(value):
        return 0.0
    return float(max(0.0, value))


def _paired_effect_size(diff: np.ndarray) -> float:
    values = np.asarray(diff, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return 0.0
    if values.size == 1:
        return float(values[0])
    std = float(np.std(values, ddof=1))
    if std <= 0 or not np.isfinite(std):
        return float(np.mean(values))
    return float(np.mean(values) / std)
