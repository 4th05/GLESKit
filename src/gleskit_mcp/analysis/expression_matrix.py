from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import NormalDist
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, leaves_list, linkage as scipy_linkage
from scipy.spatial.distance import squareform
from sklearn.decomposition import PCA

from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.tabular import (
    LoadedTable,
    _deduplicate_columns,
    excel_workbook_engine,
    is_excel_workbook,
    load_table,
)
from gleskit_mcp.utils import clean_text


AnalysisAxis = Literal["rows", "columns"]


@dataclass(slots=True)
class LoadedMatrix:
    source_kind: str
    path: str
    source_text: str
    delimiter: str
    row_labels: list[str]
    column_labels: list[str]
    values: np.ndarray
    row_id_column: str
    analysis_axis_default: AnalysisAxis
    sheet_name: str | None = None
    available_sheets: list[str] | None = None
    header_row: int | None = None
    skip_rows: int | None = None


class ExpressionMatrixAnalysisBackend:
    @staticmethod
    def normalize_analysis_axis(axis: str, *, default: AnalysisAxis = "columns") -> AnalysisAxis:
        token = clean_text(axis).lower().replace("-", "_").replace(" ", "_")
        if not token:
            return default
        alias_map = {
            "rows": "rows",
            "row": "rows",
            "samples_in_rows": "rows",
            "sample_rows": "rows",
            "sample_axis_rows": "rows",
            "observations_in_rows": "rows",
            "genes_in_columns": "rows",
            "gene_columns": "rows",
            "columns": "columns",
            "column": "columns",
            "cols": "columns",
            "col": "columns",
            "samples_in_columns": "columns",
            "sample_columns": "columns",
            "sample_axis_columns": "columns",
            "observations_in_columns": "columns",
            "genes_in_rows": "columns",
            "gene_rows": "columns",
        }
        normalized = alias_map.get(token)
        if normalized:
            return normalized  # type: ignore[return-value]
        if "sample" in token and "column" in token:
            return "columns"
        if "sample" in token and "row" in token:
            return "rows"
        if "gene" in token and "column" in token:
            return "rows"
        if "gene" in token and "row" in token:
            return "columns"
        raise ProviderError(
            "analysis_axis must be 'rows' or 'columns'. "
            "Use 'columns' when samples are in the columns of a gene x sample matrix, "
            "and use 'rows' when samples are in the rows."
        )

    def load_matrix(
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
        analysis_axis: AnalysisAxis = "columns",
        require_unique_row_ids: bool = True,
    ) -> LoadedMatrix:
        source = self._load_matrix_source(
            matrix_path=matrix_path,
            table_text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
        )
        normalized_analysis_axis = self.normalize_analysis_axis(analysis_axis)
        frame = source.frame.copy()
        if frame.empty:
            raise ProviderError("Expression matrix analysis requires a non-empty table.")

        cleaned_row_id_column = clean_text(row_id_column)
        if cleaned_row_id_column and cleaned_row_id_column not in frame.columns:
            raise ProviderError(f"Matrix row id column '{cleaned_row_id_column}' was not found.")
        if not cleaned_row_id_column:
            cleaned_row_id_column = self._infer_row_id_column(frame)

        row_labels = [clean_text(value) or f"row_{index + 1}" for index, value in enumerate(frame[cleaned_row_id_column].tolist())]
        if require_unique_row_ids and len(set(row_labels)) != len(row_labels):
            raise ProviderError("Matrix row identifiers must be unique.")

        value_frame = frame.drop(columns=[cleaned_row_id_column], errors="ignore")
        if value_frame.empty:
            raise ProviderError("Expression matrix analysis needs at least one numeric feature column.")

        numeric_frame = value_frame.apply(pd.to_numeric, errors="coerce")
        if numeric_frame.isna().any().any():
            bad_cells = int(numeric_frame.isna().sum().sum())
            raise ProviderError(f"Expression matrix contains {bad_cells} non-numeric or missing value(s).")

        column_labels = [clean_text(token) or f"feature_{index + 1}" for index, token in enumerate(numeric_frame.columns.tolist())]
        numeric_frame.columns = column_labels

        if transpose:
            numeric_frame = numeric_frame.T
            row_labels, column_labels = column_labels, row_labels

        values = numeric_frame.to_numpy(dtype=float, copy=True)
        if values.size == 0:
            raise ProviderError("Expression matrix analysis produced an empty numeric matrix.")

        return LoadedMatrix(
            source_kind=source.source_kind,
            path=source.path,
            source_text=source.text,
            delimiter=source.delimiter,
            row_labels=list(row_labels),
            column_labels=list(column_labels),
            values=values,
            row_id_column=cleaned_row_id_column,
            analysis_axis_default=normalized_analysis_axis,
            sheet_name=source.sheet_name,
            available_sheets=source.available_sheets,
            header_row=source.header_row,
            skip_rows=source.skip_rows,
        )

    def _load_matrix_source(
        self,
        *,
        matrix_path: str = "",
        table_text: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
    ) -> LoadedTable:
        cleaned_path = clean_text(matrix_path)
        if cleaned_path and not clean_text(table_text) and is_excel_workbook(cleaned_path):
            return self._load_excel_matrix_source(
                matrix_path=cleaned_path,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                delimiter=delimiter,
            )

        source = load_table(
            path=matrix_path,
            text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        repaired = self._repair_excel_matrix_source(
            current=source,
            matrix_path=matrix_path,
            sheet_name=sheet_name,
            requested_header_row=header_row,
            requested_skip_rows=skip_rows,
        )
        return repaired or source

    def _load_excel_matrix_source(
        self,
        *,
        matrix_path: str,
        sheet_name: str,
        header_row: int,
        skip_rows: int,
        delimiter: str,
    ) -> LoadedTable:
        workbook = pd.ExcelFile(matrix_path, engine=excel_workbook_engine(matrix_path))
        available_sheets = list(workbook.sheet_names)
        selected_sheet = clean_text(sheet_name) or (available_sheets[0] if available_sheets else "")
        if not selected_sheet:
            raise ProviderError("Excel workbook does not contain any readable sheet.")
        if selected_sheet not in available_sheets:
            raise ProviderError(
                f"Unable to load Excel sheet '{selected_sheet}'. Available sheets: {', '.join(available_sheets) or 'none'}."
            )

        normalized_header_row = max(1, int(header_row or 1))
        preview_frame = pd.read_excel(
            workbook,
            sheet_name=selected_sheet,
            header=None,
            dtype=str,
            keep_default_na=False,
            nrows=max(25, normalized_header_row),
        )
        detected_index = normalized_header_row - 1
        if normalized_header_row == 1:
            detected_index = self._detect_excel_matrix_header_index(preview_frame)

        raw_frame = pd.read_excel(
            workbook,
            sheet_name=selected_sheet,
            header=None,
            dtype=str,
            keep_default_na=False,
            skiprows=detected_index,
        )
        if raw_frame.empty:
            raise ProviderError("Expression matrix analysis requires a non-empty table.")
        header_values = raw_frame.iloc[0].tolist()
        columns = _deduplicate_columns(
            [clean_text(value) or f"column_{index + 1}" for index, value in enumerate(header_values)]
        )
        data_frame = raw_frame.iloc[1:].copy()
        data_frame.columns = columns
        data_frame = data_frame.dropna(how="all")
        return LoadedTable(
            source_kind="file",
            path=str(matrix_path),
            delimiter=delimiter or "\t",
            text=data_frame.to_csv(sep="\t", index=False),
            frame=data_frame,
            file_format="excel",
            sheet_name=selected_sheet or None,
            available_sheets=available_sheets,
            header_row=detected_index + 1,
            skip_rows=max(0, int(skip_rows or 0)),
        )

    def _repair_excel_matrix_source(
        self,
        *,
        current: LoadedTable,
        matrix_path: str,
        sheet_name: str,
        requested_header_row: int,
        requested_skip_rows: int,
    ) -> LoadedTable | None:
        if current.file_format != "excel" or clean_text(current.path or matrix_path) == "":
            return None
        if int(requested_header_row or 1) != 1:
            return None
        if not self._sheet_looks_like_shifted_matrix(current.frame):
            return None

        selected_sheet = clean_text(sheet_name) or clean_text(current.sheet_name)
        workbook = pd.ExcelFile(current.path, engine=excel_workbook_engine(current.path))
        try:
            raw_frame = pd.read_excel(
                workbook,
                sheet_name=selected_sheet,
                header=None,
                dtype=str,
                keep_default_na=False,
            )
        except ValueError as exc:
            raise ProviderError(f"Unable to load Excel sheet '{selected_sheet}': {exc}") from exc

        detected_index = self._detect_excel_matrix_header_index(raw_frame)
        if detected_index <= 0:
            return None

        header_values = raw_frame.iloc[detected_index].tolist()
        columns = _deduplicate_columns(
            [clean_text(value) or f"column_{index + 1}" for index, value in enumerate(header_values)]
        )
        data_frame = raw_frame.iloc[detected_index + 1 :].copy()
        data_frame.columns = columns
        data_frame = data_frame.dropna(how="all")
        return LoadedTable(
            source_kind=current.source_kind,
            path=current.path,
            delimiter=current.delimiter,
            text=data_frame.to_csv(sep="\t", index=False),
            frame=data_frame,
            file_format=current.file_format,
            sheet_name=selected_sheet or None,
            available_sheets=current.available_sheets or [],
            header_row=detected_index + 1,
            skip_rows=max(0, int(requested_skip_rows or 0)),
        )

    def _detect_excel_matrix_header_index(self, raw_frame: pd.DataFrame) -> int:
        if raw_frame.empty:
            return 0

        best_index = 0
        best_score = float("-inf")
        row_limit = min(len(raw_frame.index), 25)
        for index in range(row_limit):
            score = self._score_excel_matrix_header_row(raw_frame, index=index)
            if score > best_score:
                best_score = score
                best_index = index
        return best_index

    def _score_excel_matrix_header_row(self, raw_frame: pd.DataFrame, *, index: int) -> float:
        row_values = [clean_text(value) for value in raw_frame.iloc[index].tolist()]
        nonempty = [value for value in row_values if value]
        nonempty_count = len(nonempty)
        if nonempty_count < 4:
            return float("-inf")

        leading = row_values[: min(4, len(row_values))]
        id_token_bonus = 0
        for value in leading:
            token = self._canonical_token(value)
            if token in {"gene", "geneid", "genes", "symbol", "ensembl", "feature", "featureid", "index", "id"}:
                id_token_bonus += 1

        numeric_start = self._matrix_numeric_column_start(row_values)
        next_block = raw_frame.iloc[index + 1 : min(len(raw_frame.index), index + 6), numeric_start:]
        numeric_like = 0
        numeric_total = 0
        for value in next_block.to_numpy().flatten().tolist():
            token = clean_text(value)
            if not token:
                continue
            numeric_total += 1
            if self._is_numeric_like(token):
                numeric_like += 1
        numeric_ratio = (numeric_like / numeric_total) if numeric_total else 0.0

        return float(nonempty_count) + float(id_token_bonus * 6) + float(numeric_ratio * 20.0)

    def _sheet_looks_like_shifted_matrix(self, frame: pd.DataFrame) -> bool:
        if frame.empty or len(frame.columns) < 5:
            return False
        generic_prefix = 0
        for column in list(frame.columns)[:4]:
            token = self._canonical_token(column)
            if token.startswith("column"):
                generic_prefix += 1
        return generic_prefix >= 2

    def _infer_row_id_column(self, frame: pd.DataFrame) -> str:
        preferred_tokens = (
            "geneid",
            "gene",
            "genes",
            "symbol",
            "ensembl",
            "featureid",
            "feature",
        )
        for preferred in preferred_tokens:
            matches = [str(column) for column in frame.columns if self._canonical_token(str(column)) == preferred]
            if len(matches) == 1:
                return matches[0]
        for column in frame.columns:
            if self._column_can_be_identifier(frame[str(column)]):
                return str(column)
        return str(frame.columns[0])

    @staticmethod
    def _matrix_numeric_column_start(row_values: list[str]) -> int:
        leading_tokens = [ExpressionMatrixAnalysisBackend._canonical_token(value) for value in row_values[:3]]
        if len(leading_tokens) >= 2 and leading_tokens[0] in {"index", "id"} and leading_tokens[1] in {
            "gene",
            "geneid",
            "symbol",
            "ensembl",
            "feature",
            "featureid",
        }:
            return 2
        if leading_tokens and leading_tokens[0] in {"gene", "geneid", "symbol", "ensembl", "feature", "featureid"}:
            return 1
        return 1

    @staticmethod
    def _canonical_token(value: object) -> str:
        return "".join(ch for ch in clean_text(value).lower() if ch.isalnum())

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
    def _column_can_be_identifier(series: pd.Series) -> bool:
        values = [clean_text(value) for value in series.tolist()]
        nonempty = [value for value in values if value]
        if len(nonempty) < 3:
            return False
        if all(ExpressionMatrixAnalysisBackend._is_numeric_like(value) for value in nonempty):
            return False
        unique_ratio = len(set(nonempty)) / float(len(nonempty))
        return unique_ratio >= 0.9

    def run_pca(
        self,
        matrix: LoadedMatrix,
        *,
        analysis_axis: AnalysisAxis = "columns",
        n_components: int = 2,
        scale: bool = True,
        top_n_loadings: int = 15,
        force: bool = False,
    ) -> dict[str, Any]:
        obs = self._observations(matrix, analysis_axis=analysis_axis)
        if not force and obs.values.shape[0] > obs.values.shape[1]:
            raise ProviderError(
                "PCA orientation looks suspicious for a gene-level matrix: the selected observation axis has "
                f"{obs.values.shape[0]} item(s) while the feature axis has only {obs.values.shape[1]} item(s). "
                "PCA usually expects samples/observations in rows and genes/features in columns. "
                "If the matrix is intentionally transposed, set force=True or switch analysis_axis to the other axis."
            )
        x = self._center_and_scale(obs.values, scale=scale)
        if x.shape[0] < 2 or x.shape[1] < 1:
            raise ProviderError("PCA requires at least two observations and one numeric feature.")

        max_components = min(int(n_components), x.shape[0], x.shape[1])
        if max_components < 1:
            raise ProviderError("PCA requires at least one component.")

        model = PCA(n_components=max_components)
        scores = model.fit_transform(x)
        explained_variance = model.explained_variance_
        variance_ratio = model.explained_variance_ratio_
        component_names = [f"PC{i + 1}" for i in range(max_components)]

        score_rows = [
            {
                "label": obs.labels[index],
                **{component_names[component_index]: float(scores[index, component_index]) for component_index in range(max_components)},
            }
            for index in range(scores.shape[0])
        ]
        loading_rows = self._top_loadings(
            feature_labels=obs.feature_labels,
            loadings=model.components_.T,
            component_names=component_names,
            top_n=top_n_loadings,
        )

        return {
            "summary": (
                f"Computed PCA on {obs.values.shape[0]} observation(s) and {obs.values.shape[1]} feature(s). "
                f"First component explains {variance_ratio[0] * 100:.2f}% of the variance."
            ),
            "provider": "expression_matrix",
            "analysis": "pca",
            "input": self._matrix_input_payload(matrix, analysis_axis=analysis_axis),
            "observations": {
                "axis": analysis_axis,
                "count": obs.values.shape[0],
                "labels": obs.labels,
            },
            "features": {
                "count": obs.values.shape[1],
                "labels": obs.feature_labels,
            },
            "components": {
                "names": component_names,
                "explained_variance": [float(value) for value in explained_variance[:max_components]],
                "explained_variance_ratio": [float(value) for value in variance_ratio],
            },
            "scores": score_rows,
            "loadings": loading_rows,
        }

    def run_log_transform(
        self,
        matrix: LoadedMatrix,
        *,
        base: float = 2.0,
        offset: float = 1.0,
        preset: str = "",
    ) -> tuple[LoadedMatrix, dict[str, Any]]:
        effective_base, effective_offset, preset_name = self._normalize_log_transform_parameters(
            base=base,
            offset=offset,
            preset=preset,
        )
        values = np.asarray(matrix.values, dtype=float)
        shifted = values + effective_offset
        if np.any(~np.isfinite(shifted)):
            raise ProviderError("Log transform produced non-finite values.")
        if np.any(shifted <= 0):
            raise ProviderError(
                "Log transform requires strictly positive values after adding the offset/pseudocount."
            )
        if math.isclose(effective_base, math.e):
            transformed_values = np.log(shifted)
            base_label = "e"
        else:
            transformed_values = np.log(shifted) / math.log(effective_base)
            base_label = f"{effective_base:g}"

        transformed_matrix = LoadedMatrix(
            source_kind=matrix.source_kind,
            path=matrix.path,
            source_text=self._matrix_to_text(
                row_labels=matrix.row_labels,
                column_labels=matrix.column_labels,
                values=transformed_values,
                delimiter=matrix.delimiter,
                row_id_column=matrix.row_id_column,
            ),
            delimiter=matrix.delimiter,
            row_labels=list(matrix.row_labels),
            column_labels=list(matrix.column_labels),
            values=np.asarray(transformed_values, dtype=float),
            row_id_column=matrix.row_id_column,
            analysis_axis_default=matrix.analysis_axis_default,
            sheet_name=matrix.sheet_name,
            available_sheets=matrix.available_sheets,
            header_row=matrix.header_row,
            skip_rows=matrix.skip_rows,
        )
        output_preview = self._matrix_preview(transformed_matrix, row_limit=5, column_limit=5)
        summary = (
            f"Applied log{base_label}(x + {effective_offset:g}) to "
            f"{values.shape[0]} observation(s) across {values.shape[1]} feature(s)."
        )
        payload = {
            "summary": summary,
            "provider": "expression_matrix",
            "analysis": "log_transform",
            "input": self._matrix_input_payload(matrix, analysis_axis=matrix.analysis_axis_default),
            "parameters": {
                "base": float(effective_base),
                "offset": float(effective_offset),
                "preset": preset_name,
            },
            "output": {
                "row_count": int(transformed_matrix.values.shape[0]),
                "column_count": int(transformed_matrix.values.shape[1]),
                "minimum": float(np.min(transformed_matrix.values)),
                "maximum": float(np.max(transformed_matrix.values)),
                "mean": float(np.mean(transformed_matrix.values)),
                "preview": output_preview,
            },
        }
        return transformed_matrix, payload

    def run_hierarchical_clustering(
        self,
        matrix: LoadedMatrix,
        *,
        analysis_axis: AnalysisAxis = "columns",
        cluster_count: int = 3,
        linkage: str = "average",
        metric: str = "correlation",
    ) -> dict[str, Any]:
        obs = self._observations(matrix, analysis_axis=analysis_axis)
        normalized_metric = self.normalize_clustering_metric(metric)
        clustering = self._agglomerative_cluster(
            obs.values,
            labels=obs.labels,
            cluster_count=max(1, int(cluster_count)),
            linkage=linkage,
            metric=normalized_metric,
        )
        return {
            "summary": (
                f"Computed hierarchical clustering for {obs.values.shape[0]} observation(s) "
                f"into {len(set(clustering['labels'].values()))} cluster(s)."
            ),
            "provider": "expression_matrix",
            "analysis": "hierarchical_clustering",
            "input": self._matrix_input_payload(matrix, analysis_axis=analysis_axis),
            "parameters": {
                "cluster_count": max(1, int(cluster_count)),
                "linkage": linkage,
                "metric": normalized_metric,
                "requested_metric": clean_text(metric) or normalized_metric,
            },
            "observations": {
                "axis": analysis_axis,
                "count": obs.values.shape[0],
                "labels": obs.labels,
            },
            "features": {
                "count": obs.values.shape[1],
                "labels": obs.feature_labels,
            },
            "cluster_count": len(set(clustering["labels"].values())),
            "cluster_labels": clustering["labels"],
            "clusters": clustering["clusters"],
            "merges": clustering["merges"],
            "order": clustering["order"],
        }

    def run_correlation(
        self,
        matrix: LoadedMatrix,
        *,
        analysis_axis: AnalysisAxis = "columns",
        method: Literal["pearson", "spearman"] = "pearson",
        secondary_matrix: LoadedMatrix | None = None,
        secondary_value_column: str = "",
        secondary_alignment_axis: Literal["features", "observations"] = "features",
        group_labels: list[str] | None = None,
        group_summary_stat: Literal["mean", "median"] = "mean",
        top_n: int = 25,
    ) -> dict[str, Any]:
        obs = self._observations(matrix, analysis_axis=analysis_axis)
        if secondary_matrix is None:
            correlations = self._pairwise_correlations(obs.values, obs.labels, method=method)
            return {
                "summary": (
                    f"Computed {method} pairwise correlations across {obs.values.shape[0]} observation(s)."
                ),
                "provider": "expression_matrix",
                "analysis": "correlation",
                "mode": "pairwise",
                "input": self._matrix_input_payload(matrix, analysis_axis=analysis_axis),
                "observations": {
                    "axis": analysis_axis,
                    "count": obs.values.shape[0],
                    "labels": obs.labels,
                },
                "features": {
                    "count": obs.values.shape[1],
                    "labels": obs.feature_labels,
                },
                "correlations": correlations[: max(1, int(top_n))],
            }

        vector_payload = self._extract_vector(
            secondary_matrix,
            desired_labels=obs.feature_labels if secondary_alignment_axis == "features" else obs.labels,
            alignment_axis=secondary_alignment_axis,
            value_column=secondary_value_column,
        )
        comparison = {
            "secondary_source_kind": secondary_matrix.source_kind,
            "secondary_path": secondary_matrix.path,
            "secondary_value_column": vector_payload["value_column"],
            "secondary_alignment_axis": vector_payload["alignment_axis"],
            "matched_label_count": len(vector_payload["labels"]),
            "missing_label_count": vector_payload["missing_count"],
        }
        if group_labels is not None:
            grouped = self._grouped_vector_correlations(
                obs,
                vector_payload,
                group_labels=group_labels,
                method=method,
                group_summary_stat=group_summary_stat,
            )
            grouped["input"] = self._matrix_input_payload(matrix, analysis_axis=analysis_axis)
            grouped["comparison"] = comparison
            return grouped

        if vector_payload["alignment_axis"] == "features":
            feature_indices = vector_payload["indices"]
            feature_labels = vector_payload["labels"]
            sub_values = obs.values[:, feature_indices]
            rows = self._vector_correlations(sub_values, obs.labels, vector_payload["vector"], method=method)
            rows = self._bh_adjust(rows)
            return {
                "summary": (
                    f"Computed {method} correlations between {obs.values.shape[0]} observation(s) and a feature-aligned vector."
                ),
                "provider": "expression_matrix",
                "analysis": "correlation",
                "mode": "vector",
                "input": self._matrix_input_payload(matrix, analysis_axis=analysis_axis),
                "comparison": comparison,
                "vector": {
                    "alignment_axis": vector_payload["alignment_axis"],
                    "labels": feature_labels,
                    "missing_count": vector_payload["missing_count"],
                },
                "correlations": rows[: max(1, int(top_n))],
            }

        observation_indices = vector_payload["indices"]
        aligned_values = obs.values[observation_indices, :]
        rows = self._feature_correlations(
            aligned_values,
            obs.feature_labels,
            vector_payload["vector"],
            method=method,
        )
        rows = self._bh_adjust(rows)
        return {
            "summary": (
                f"Computed {method} correlations between feature profiles and an observation-aligned vector."
            ),
            "provider": "expression_matrix",
            "analysis": "correlation",
            "mode": "vector",
            "input": self._matrix_input_payload(matrix, analysis_axis=analysis_axis),
            "comparison": comparison,
            "vector": {
                "alignment_axis": vector_payload["alignment_axis"],
                "labels": vector_payload["labels"],
                "missing_count": vector_payload["missing_count"],
            },
            "correlations": rows[: max(1, int(top_n))],
        }

    def run_consensus_clustering(
        self,
        matrix: LoadedMatrix,
        *,
        analysis_axis: AnalysisAxis = "columns",
        cluster_count: int = 3,
        iterations: int = 50,
        sample_fraction: float = 0.7,
        feature_fraction: float = 1.0,
        random_seed: int = 0,
        metric: str = "correlation",
        linkage: str = "average",
    ) -> dict[str, Any]:
        obs = self._observations(matrix, analysis_axis=analysis_axis)
        if obs.values.shape[0] < 2:
            raise ProviderError("Consensus clustering requires at least two observations.")

        rng = np.random.default_rng(int(random_seed))
        n_obs, n_features = obs.values.shape
        normalized_metric = self.normalize_clustering_metric(metric)
        n_sample = max(2, min(n_obs, int(round(n_obs * float(sample_fraction)))))
        n_features_sub = max(1, min(n_features, int(round(n_features * float(feature_fraction)))))
        coassoc = np.zeros((n_obs, n_obs), dtype=float)
        assignment_counts = np.zeros((n_obs, n_obs), dtype=float)

        for _ in range(max(1, int(iterations))):
            obs_idx = np.sort(rng.choice(n_obs, size=n_sample, replace=False))
            feat_idx = np.sort(rng.choice(n_features, size=n_features_sub, replace=False))
            sub_values = obs.values[np.ix_(obs_idx, feat_idx)]
            sub_labels = [obs.labels[index] for index in obs_idx]
            hierarchical = self._agglomerative_cluster(
                sub_values,
                labels=sub_labels,
                cluster_count=max(1, int(cluster_count)),
                linkage=linkage,
                metric=normalized_metric,
            )
            assignments = hierarchical["labels"]
            centroids = self._cluster_centroids(sub_values, sub_labels, assignments)
            full_projection = obs.values[:, feat_idx]
            full_labels = self._assign_to_centroids(full_projection, centroids)
            for cluster_label in set(full_labels.values()):
                members = [index for index, label in full_labels.items() if label == cluster_label]
                for i in members:
                    assignment_counts[i, i] += 1
                    for j in members:
                        coassoc[i, j] += 1

        consensus = coassoc / max(1.0, float(iterations))
        final = self._agglomerative_cluster(
            1.0 - consensus,
            labels=obs.labels,
            cluster_count=max(1, int(cluster_count)),
            linkage=linkage,
            metric="precomputed",
        )
        stability = []
        for index, label in enumerate(obs.labels):
            members = [other for other, assigned in final["labels"].items() if assigned == final["labels"][label]]
            if len(members) <= 1:
                score = 1.0
            else:
                idx = obs.labels.index(label)
                score = float(np.mean([consensus[idx, obs.labels.index(other)] for other in members if other != label]))
            stability.append({"label": label, "cluster": final["labels"][label], "stability": score})
        stability_scores = np.asarray([float(row["stability"]) for row in stability], dtype=float)
        stability_thresholds = [0.8, 0.85, 0.9, 0.93, 0.95, 0.99, 1.0]
        cluster_stability = []
        for cluster_label in sorted(set(int(row["cluster"]) for row in stability)):
            cluster_scores = np.asarray(
                [float(row["stability"]) for row in stability if int(row["cluster"]) == cluster_label],
                dtype=float,
            )
            cluster_stability.append(
                {
                    "cluster": int(cluster_label),
                    "sample_count": int(cluster_scores.size),
                    "mean_stability": float(np.mean(cluster_scores)),
                    "median_stability": float(np.median(cluster_scores)),
                    "minimum_stability": float(np.min(cluster_scores)),
                    "maximum_stability": float(np.max(cluster_scores)),
                }
            )
        consistency_summary = {
            "scope": "all_observations",
            "observation_count": int(stability_scores.size),
            "mean_stability": float(np.mean(stability_scores)),
            "median_stability": float(np.median(stability_scores)),
            "minimum_stability": float(np.min(stability_scores)),
            "maximum_stability": float(np.max(stability_scores)),
            "counts_at_thresholds": [
                {
                    "threshold": float(threshold),
                    "count": int(np.sum(stability_scores >= float(threshold))),
                }
                for threshold in stability_thresholds
            ],
            "per_cluster": cluster_stability,
        }

        return {
            "summary": (
                f"Computed consensus clustering for {n_obs} observation(s) into {len(set(final['labels'].values()))} cluster(s) "
                f"across {max(1, int(iterations))} iteration(s)."
            ),
            "provider": "expression_matrix",
            "analysis": "consensus_clustering",
            "input": self._matrix_input_payload(matrix, analysis_axis=analysis_axis),
            "parameters": {
                "cluster_count": max(1, int(cluster_count)),
                "iterations": max(1, int(iterations)),
                "sample_fraction": float(sample_fraction),
                "feature_fraction": float(feature_fraction),
                "random_seed": int(random_seed),
                "metric": normalized_metric,
                "requested_metric": clean_text(metric) or normalized_metric,
                "linkage": linkage,
            },
            "observations": {
                "axis": analysis_axis,
                "count": n_obs,
                "labels": obs.labels,
            },
            "features": {
                "count": n_features,
                "labels": obs.feature_labels,
            },
            "cluster_count": len(set(final["labels"].values())),
            "cluster_labels": final["labels"],
            "clusters": final["clusters"],
            "merges": final["merges"],
            "order": final["order"],
            "consensus_matrix": consensus.tolist(),
            "stability": stability,
            "consistency_summary": consistency_summary,
            "iterations": int(iterations),
            "sample_fraction": float(sample_fraction),
            "feature_fraction": float(feature_fraction),
        }

    def _observations(self, matrix: LoadedMatrix, *, analysis_axis: AnalysisAxis) -> "_PreparedObservations":
        normalized_axis = self.normalize_analysis_axis(analysis_axis)
        if normalized_axis == "rows":
            return _PreparedObservations(
                values=np.asarray(matrix.values, dtype=float),
                labels=list(matrix.row_labels),
                feature_labels=list(matrix.column_labels),
            )
        return _PreparedObservations(
            values=np.asarray(matrix.values, dtype=float).T,
            labels=list(matrix.column_labels),
            feature_labels=list(matrix.row_labels),
        )

    @staticmethod
    def _center_and_scale(values: np.ndarray, *, scale: bool) -> np.ndarray:
        centered = values - np.mean(values, axis=0, keepdims=True)
        if not scale:
            return centered
        std = np.std(centered, axis=0, ddof=1, keepdims=True)
        std[~np.isfinite(std)] = 1.0
        std[std == 0] = 1.0
        return centered / std

    def _top_loadings(
        self,
        *,
        feature_labels: list[str],
        loadings: np.ndarray,
        component_names: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        abs_loadings = np.abs(loadings)
        top_n = max(1, min(int(top_n), len(feature_labels)))
        for component_index, component_name in enumerate(component_names):
            ranking = np.argsort(abs_loadings[:, component_index])[::-1][:top_n]
            for rank, feature_index in enumerate(ranking, start=1):
                rows.append(
                    {
                        "component": component_name,
                        "feature": feature_labels[feature_index],
                        "loading": float(loadings[feature_index, component_index]),
                        "absolute_loading": float(abs_loadings[feature_index, component_index]),
                        "rank": rank,
                    }
                )
        return rows

    def _pairwise_correlations(
        self,
        values: np.ndarray,
        labels: list[str],
        *,
        method: Literal["pearson", "spearman"],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for i in range(values.shape[0]):
            for j in range(i + 1, values.shape[0]):
                correlation = self._correlate(values[i], values[j], method=method)
                rows.append(
                {
                    "label_a": labels[i],
                    "label_b": labels[j],
                    "correlation": correlation["correlation"],
                    "r": correlation["correlation"],
                    "p_value": correlation["p_value"],
                }
            )
        rows.sort(key=lambda item: (abs(float(item["correlation"] or 0.0)), -float(item["p_value"] or 1.0)), reverse=True)
        return rows

    def _vector_correlations(
        self,
        values: np.ndarray,
        labels: list[str],
        vector: np.ndarray,
        *,
        method: Literal["pearson", "spearman"],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, label in enumerate(labels):
            correlation = self._correlate(values[index], vector, method=method)
            rows.append(
                {
                    "label": label,
                    "correlation": correlation["correlation"],
                    "r": correlation["correlation"],
                    "p_value": correlation["p_value"],
                }
            )
        rows.sort(key=lambda item: abs(float(item["correlation"] or 0.0)), reverse=True)
        return rows

    def _feature_correlations(
        self,
        values: np.ndarray,
        feature_labels: list[str],
        vector: np.ndarray,
        *,
        method: Literal["pearson", "spearman"],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if values.shape[0] != vector.size:
            raise ProviderError("Feature correlations require a vector aligned to the observation axis.")
        for feature_index, feature_label in enumerate(feature_labels):
            correlation = self._correlate(values[:, feature_index], vector, method=method)
            rows.append(
                {
                    "label": feature_label,
                    "correlation": correlation["correlation"],
                    "r": correlation["correlation"],
                    "p_value": correlation["p_value"],
                }
            )
        rows.sort(key=lambda item: abs(float(item["correlation"] or 0.0)), reverse=True)
        return rows

    def _grouped_vector_correlations(
        self,
        obs: "_PreparedObservations",
        vector_payload: dict[str, Any],
        *,
        group_labels: list[str],
        method: Literal["pearson", "spearman"],
        group_summary_stat: Literal["mean", "median"],
    ) -> dict[str, Any]:
        if len(group_labels) != obs.values.shape[0]:
            raise ProviderError("Group labels must match the number of observations.")
        if vector_payload["alignment_axis"] != "features":
            raise ProviderError("Grouped correlations currently require a feature-aligned secondary vector.")

        group_map: dict[str, list[int]] = {}
        for index, label in enumerate(group_labels):
            token = clean_text(label)
            if not token:
                continue
            group_map.setdefault(token, []).append(index)
        if len(group_map) < 2:
            raise ProviderError("Grouped correlations require at least two non-empty groups.")

        matched_indices = vector_payload["indices"]
        aligned_vector = np.asarray(vector_payload["vector"], dtype=float)
        sub_values = obs.values[:, matched_indices]
        overall_row = np.mean(sub_values, axis=0) if group_summary_stat == "mean" else np.median(sub_values, axis=0)
        overall_corr = self._correlate(np.asarray(overall_row, dtype=float), aligned_vector, method=method)

        rows: list[dict[str, Any]] = []
        for group_label, indices in sorted(group_map.items(), key=lambda item: (-len(item[1]), item[0])):
            group_values = sub_values[indices, :]
            if group_summary_stat == "median":
                summary_values = np.median(group_values, axis=0)
            else:
                summary_values = np.mean(group_values, axis=0)
            missing_count = int(np.isnan(summary_values).sum())
            valid_mask = np.isfinite(summary_values) & np.isfinite(aligned_vector)
            n = int(np.sum(valid_mask))
            if n < 3:
                correlation = 0.0
                p_value = 1.0
            else:
                result = self._correlate(summary_values[valid_mask], aligned_vector[valid_mask], method=method)
                correlation = result["correlation"]
                p_value = result["p_value"]
            rows.append(
                {
                    "group": group_label,
                    "n": n,
                    "missing_count": missing_count,
                    "correlation": float(correlation),
                    "r": float(correlation),
                    "p_value": float(p_value),
                    "group_size": len(indices),
                }
            )

        rows = self._bh_adjust(rows)
        return {
            "summary": (
                f"Computed {method} grouped correlations across {len(rows)} group(s) with a feature-aligned vector."
            ),
            "provider": "expression_matrix",
            "analysis": "correlation",
            "mode": "grouped_vector",
            "group_summary_stat": group_summary_stat,
            "vector": {
                "alignment_axis": vector_payload["alignment_axis"],
                "labels": vector_payload["labels"],
                "missing_count": vector_payload["missing_count"],
            },
            "overall": {
                "n": int(np.sum(np.isfinite(overall_row) & np.isfinite(aligned_vector))),
                "missing_count": int(np.isnan(overall_row).sum()),
                "correlation": float(overall_corr["correlation"]),
                "r": float(overall_corr["correlation"]),
                "p_value": float(overall_corr["p_value"]),
            },
            "correlations": rows[: max(1, len(rows))],
        }

    def _paired_matrix_correlations(
        self,
        primary_values: np.ndarray,
        primary_labels: list[str],
        secondary_values: np.ndarray,
        secondary_labels: list[str],
        *,
        method: Literal["pearson", "spearman"],
    ) -> list[dict[str, Any]]:
        shared = [label for label in primary_labels if label in secondary_labels]
        if not shared:
            raise ProviderError("No shared labels were found between the paired matrices.")
        secondary_index = {label: index for index, label in enumerate(secondary_labels)}
        rows: list[dict[str, Any]] = []
        for label in shared:
            i = primary_labels.index(label)
            j = secondary_index[label]
            correlation = self._correlate(primary_values[i], secondary_values[j], method=method)
            rows.append(
                {
                    "label": label,
                    "correlation": correlation["correlation"],
                    "p_value": correlation["p_value"],
                }
            )
        rows.sort(key=lambda item: abs(float(item["correlation"] or 0.0)), reverse=True)
        return rows

    def _prepare_correlation_comparison(
        self,
        *,
        matrix: LoadedMatrix,
        analysis_axis: AnalysisAxis,
        secondary_matrix: LoadedMatrix,
        secondary_value_column: str,
        secondary_alignment_axis: Literal["features", "observations"] = "features",
    ) -> dict[str, Any]:
        primary = self._observations(matrix, analysis_axis=analysis_axis)
        secondary = self._observations(secondary_matrix, analysis_axis=analysis_axis)

        if secondary_value_column:
            desired_labels = primary.feature_labels if secondary_alignment_axis == "features" else primary.labels
            vector_payload = self._extract_vector(
                secondary_matrix,
                desired_labels=desired_labels,
                alignment_axis=secondary_alignment_axis,
                value_column=secondary_value_column,
            )
            return {
                "mode": "vector",
                "comparison": {
                    "secondary_source_kind": secondary_matrix.source_kind,
                    "secondary_path": secondary_matrix.path,
                    "secondary_value_column": secondary_value_column,
                    "secondary_alignment_axis": secondary_alignment_axis,
                },
                "vector": vector_payload["vector"],
            }

        if primary.values.shape != secondary.values.shape:
            raise ProviderError(
                "Paired matrix correlation requires matrices with the same shape when no vector column is specified."
            )
        if primary.labels != secondary.labels or primary.feature_labels != secondary.feature_labels:
            raise ProviderError("Paired matrix correlation requires matching labels on both matrices.")
        return {
            "mode": "paired_matrix",
            "comparison": {
                "secondary_source_kind": secondary_matrix.source_kind,
                "secondary_path": secondary_matrix.path,
                "analysis_axis": analysis_axis,
            },
            "secondary_obs": secondary,
        }

    def _extract_vector(
        self,
        matrix: LoadedMatrix,
        *,
        desired_labels: list[str],
        alignment_axis: Literal["features", "observations"],
        value_column: str,
    ) -> dict[str, Any]:
        delimiter = matrix.delimiter
        source_text = matrix.source_text or ""
        if not clean_text(source_text) and matrix.path:
            source_text = load_table(path=matrix.path, delimiter=delimiter, dtype=str, keep_default_na=False).text
        frame = load_table(text=source_text, delimiter=delimiter, dtype=str, keep_default_na=False).frame
        if frame.empty:
            raise ProviderError("Secondary vector table is empty.")

        row_id_column = matrix.row_id_column if matrix.row_id_column in frame.columns else str(frame.columns[0])
        if row_id_column not in frame.columns:
            raise ProviderError(f"Secondary vector row id column '{row_id_column}' was not found.")
        if value_column and value_column not in frame.columns:
            raise ProviderError(f"Secondary vector value column '{value_column}' was not found.")
        if not value_column:
            candidate_columns = [column for column in frame.columns if column != row_id_column]
            if len(candidate_columns) != 1:
                raise ProviderError("Secondary vector tables require exactly one numeric value column or an explicit value column.")
            value_column = candidate_columns[0]

        series = frame.set_index(row_id_column)[value_column]
        values = pd.to_numeric(series, errors="coerce")
        lookup = {clean_text(label): float(value) for label, value in values.items() if clean_text(label) and pd.notna(value)}
        aligned_labels: list[str] = []
        aligned_values: list[float] = []
        indices: list[int] = []
        missing_count = 0
        seen: set[str] = set()
        for index, label in enumerate(desired_labels):
            token = clean_text(label)
            if not token or token in seen:
                continue
            seen.add(token)
            value = lookup.get(token)
            if value is None:
                missing_count += 1
                continue
            aligned_labels.append(token)
            aligned_values.append(float(value))
            indices.append(index)

        if len(aligned_values) < 3:
            raise ProviderError("Secondary vector could not be aligned to at least 3 labels shared with the matrix.")

        return {
            "alignment_axis": alignment_axis,
            "labels": aligned_labels,
            "indices": indices,
            "vector": np.asarray(aligned_values, dtype=float),
            "missing_count": missing_count,
            "value_column": value_column,
        }

    def _matrix_input_payload(self, matrix: LoadedMatrix, *, analysis_axis: AnalysisAxis) -> dict[str, Any]:
        return {
            "source_kind": matrix.source_kind,
            "path": matrix.path,
            "delimiter": matrix.delimiter,
            "row_id_column": matrix.row_id_column,
            "analysis_axis": analysis_axis,
            "sheet_name": matrix.sheet_name,
            "available_sheets": list(matrix.available_sheets or []),
            "header_row": matrix.header_row,
            "skip_rows": matrix.skip_rows,
            "row_count": int(matrix.values.shape[0]),
            "column_count": int(matrix.values.shape[1]),
            "row_labels": list(matrix.row_labels),
            "column_labels": list(matrix.column_labels),
        }

    @staticmethod
    def _matrix_preview(
        matrix: LoadedMatrix,
        *,
        row_limit: int = 5,
        column_limit: int = 5,
    ) -> dict[str, Any]:
        row_limit = max(1, int(row_limit))
        column_limit = max(1, int(column_limit))
        row_count = int(matrix.values.shape[0])
        column_count = int(matrix.values.shape[1])
        visible_columns = list(matrix.column_labels[:column_limit])
        rows: list[dict[str, Any]] = []
        for row_index in range(min(row_count, row_limit)):
            values = {
                visible_columns[column_index]: round(float(matrix.values[row_index, column_index]), 6)
                for column_index in range(min(column_count, column_limit))
            }
            rows.append({"label": matrix.row_labels[row_index], "values": values})
        return {
            "row_count": row_count,
            "column_count": column_count,
            "visible_row_count": len(rows),
            "visible_column_count": len(visible_columns),
            "row_labels": list(matrix.row_labels[:row_limit]),
            "column_labels": visible_columns,
            "rows": rows,
        }

    @staticmethod
    def _matrix_to_text(
        *,
        row_labels: list[str],
        column_labels: list[str],
        values: np.ndarray,
        delimiter: str,
        row_id_column: str,
    ) -> str:
        frame = pd.DataFrame(np.asarray(values, dtype=float), columns=column_labels)
        frame.insert(0, row_id_column or "row_id", row_labels)
        return frame.to_csv(sep=delimiter or "\t", index=False)

    @staticmethod
    def _normalize_log_transform_parameters(
        *,
        base: float,
        offset: float,
        preset: str,
    ) -> tuple[float, float, str]:
        preset_token = clean_text(preset).lower().replace(" ", "").replace("_", "")
        if preset_token in {"custom", "default"}:
            preset_token = ""
        presets = {
            "log2": (2.0, 0.0),
            "log2p1": (2.0, 1.0),
            "log10": (10.0, 0.0),
            "log10p1": (10.0, 1.0),
            "ln": (math.e, 0.0),
            "lnp1": (math.e, 1.0),
        }
        if preset_token:
            if preset_token not in presets:
                raise ProviderError(
                    "Unsupported log transform preset. Use one of: custom, log2, log2p1, log10, log10p1, ln, lnp1."
                )
            effective_base, effective_offset = presets[preset_token]
            return float(effective_base), float(effective_offset), preset_token

        effective_base = float(base)
        effective_offset = float(offset)
        if not math.isfinite(effective_base) or effective_base <= 1.0:
            raise ProviderError("Log transform base must be a finite number greater than 1.")
        if not math.isfinite(effective_offset) or effective_offset < 0.0:
            raise ProviderError("Log transform offset must be a finite number greater than or equal to 0.")
        return effective_base, effective_offset, "custom"

    def _agglomerative_cluster(
        self,
        values: np.ndarray,
        *,
        labels: list[str],
        cluster_count: int,
        linkage: str,
        metric: str,
    ) -> dict[str, Any]:
        if values.shape[0] != len(labels):
            raise ProviderError("Cluster labels do not match the number of observations.")
        if values.shape[0] == 0:
            raise ProviderError("Clustering requires at least one observation.")
        if cluster_count > values.shape[0]:
            cluster_count = values.shape[0]
        if cluster_count < 1:
            cluster_count = 1

        if values.shape[0] == 1:
            return {
                "labels": {labels[0]: 1},
                "clusters": [{"cluster": 1, "members": [labels[0]], "size": 1}],
                "merges": [],
                "order": [labels[0]],
            }

        condensed = self._condensed_distance_vector(values, metric=metric)
        linkage_matrix = scipy_linkage(condensed, method=self._scipy_linkage_method(linkage))
        raw_labels = fcluster(linkage_matrix, t=cluster_count, criterion="maxclust")
        cluster_members: dict[int, list[int]] = {}
        for index, raw_label in enumerate(raw_labels.tolist()):
            cluster_members.setdefault(int(raw_label), []).append(index)

        ordered_clusters = sorted(cluster_members.values(), key=lambda member_idx: min(member_idx))
        label_map: dict[str, int] = {}
        clusters_payload: list[dict[str, Any]] = []
        for cluster_label, member_indices in enumerate(ordered_clusters, start=1):
            members = [labels[index] for index in member_indices]
            clusters_payload.append({"cluster": cluster_label, "members": members, "size": len(members)})
            for member in members:
                label_map[member] = cluster_label

        merges = [
            {
                "left": int(row[0]),
                "right": int(row[1]),
                "distance": float(row[2]),
                "size": int(row[3]),
            }
            for row in linkage_matrix.tolist()
        ]
        order = [labels[index] for index in leaves_list(linkage_matrix).tolist()]
        return {
            "labels": label_map,
            "clusters": clusters_payload,
            "merges": merges,
            "order": order,
        }

    def _condensed_distance_vector(self, values: np.ndarray, *, metric: str) -> np.ndarray:
        if metric == "precomputed":
            matrix = np.asarray(values, dtype=float)
            if matrix.shape[0] != matrix.shape[1]:
                raise ProviderError("Precomputed clustering distance matrices must be square.")
            matrix = (matrix + matrix.T) / 2.0
            np.fill_diagonal(matrix, 0.0)
            return squareform(matrix, checks=False)
        pairwise = self._pairwise_distance_matrix(values, metric=metric)
        return squareform(pairwise, checks=False)

    def _pairwise_distance_matrix(self, values: np.ndarray, *, metric: str) -> np.ndarray:
        n_obs = values.shape[0]
        distances = np.zeros((n_obs, n_obs), dtype=float)
        for i in range(n_obs):
            for j in range(i + 1, n_obs):
                distances[i, j] = distances[j, i] = self._distance(values[i], values[j], metric=metric)
        return distances

    def _distance(self, left: np.ndarray, right: np.ndarray, *, metric: str) -> float:
        if metric == "euclidean":
            return float(np.sqrt(np.sum((left - right) ** 2)))
        if metric == "manhattan":
            return float(np.sum(np.abs(left - right)))
        if metric == "correlation":
            result = self._correlate(left, right, method="pearson")
            correlation = result["correlation"]
            if correlation is None or math.isnan(correlation):
                return 1.0
            return float(1.0 - correlation)
        if metric == "spearman":
            result = self._correlate(left, right, method="spearman")
            correlation = result["correlation"]
            if correlation is None or math.isnan(correlation):
                return 1.0
            return float(1.0 - correlation)
        raise ProviderError(f"Unsupported clustering metric: {metric}")

    @staticmethod
    def normalize_clustering_metric(metric: str) -> str:
        token = clean_text(metric).lower().replace("-", "_").replace(" ", "_")
        if not token:
            return "correlation"
        alias_map = {
            "correlation": "correlation",
            "corr": "correlation",
            "pearson": "correlation",
            "pearsonr": "correlation",
            "pearson_correlation": "correlation",
            "pearson_distance": "correlation",
            "correlation_distance": "correlation",
            "spearman": "spearman",
            "spearmanr": "spearman",
            "spearman_correlation": "spearman",
            "rank_correlation": "spearman",
            "euclidean": "euclidean",
            "l2": "euclidean",
            "manhattan": "manhattan",
            "cityblock": "manhattan",
            "l1": "manhattan",
            "taxicab": "manhattan",
            "precomputed": "precomputed",
            # Small models sometimes place the downstream agreement score into the clustering metric slot.
            "accuracy": "correlation",
            "agreement": "correlation",
            "label_accuracy": "correlation",
            "classification_accuracy": "correlation",
        }
        normalized = alias_map.get(token)
        if normalized:
            return normalized
        raise ProviderError(
            "Unsupported clustering metric: "
            f"{metric}. Use correlation/pearson, spearman, euclidean, or manhattan. "
            "Accuracy is an agreement output, not a clustering distance."
        )

    def _cluster_distance(
        self,
        left_members: list[int],
        right_members: list[int],
        distance_matrix: np.ndarray,
        *,
        linkage: str,
    ) -> float:
        sub = distance_matrix[np.ix_(left_members, right_members)]
        if linkage == "single":
            return float(np.min(sub))
        if linkage == "complete":
            return float(np.max(sub))
        if linkage == "average":
            return float(np.mean(sub))
        raise ProviderError(f"Unsupported linkage method: {linkage}")

    @staticmethod
    def _scipy_linkage_method(linkage: str) -> str:
        token = clean_text(linkage).lower()
        if token in {"single", "complete", "average"}:
            return token
        raise ProviderError(f"Unsupported linkage method: {linkage}")

    @staticmethod
    def _cluster_centroids(values: np.ndarray, labels: list[str], assignments: dict[str, int]) -> dict[int, np.ndarray]:
        centroids: dict[int, list[np.ndarray]] = {}
        for index, label in enumerate(labels):
            cluster_id = assignments[label]
            centroids.setdefault(cluster_id, []).append(values[index])
        return {cluster_id: np.mean(np.vstack(members), axis=0) for cluster_id, members in centroids.items() if members}

    @staticmethod
    def _assign_to_centroids(values: np.ndarray, centroids: dict[int, np.ndarray]) -> dict[int, int]:
        assignments: dict[int, int] = {}
        centroid_ids = sorted(centroids)
        centroid_values = [centroids[cluster_id] for cluster_id in centroid_ids]
        for index, row in enumerate(values):
            best_cluster = centroid_ids[0]
            best_distance = math.inf
            for cluster_id, centroid in zip(centroid_ids, centroid_values, strict=False):
                distance = float(np.sqrt(np.sum((row - centroid) ** 2)))
                if distance < best_distance:
                    best_distance = distance
                    best_cluster = cluster_id
            assignments[index] = best_cluster
        return assignments

    def _correlate(self, left: np.ndarray, right: np.ndarray, *, method: Literal["pearson", "spearman"]) -> dict[str, float]:
        x = np.asarray(left, dtype=float)
        y = np.asarray(right, dtype=float)
        if x.shape != y.shape:
            raise ProviderError("Correlation requires vectors with matching length.")
        if method == "spearman":
            x = pd.Series(x).rank(method="average").to_numpy(dtype=float)
            y = pd.Series(y).rank(method="average").to_numpy(dtype=float)
        if x.size < 2:
            return {"correlation": 0.0, "p_value": 1.0}
        x = x - np.mean(x)
        y = y - np.mean(y)
        denom = float(np.sqrt(np.sum(x**2) * np.sum(y**2)))
        if denom == 0:
            return {"correlation": 0.0, "p_value": 1.0}
        correlation = float(np.clip(np.sum(x * y) / denom, -1.0, 1.0))
        p_value = self._correlation_p_value(correlation, n=x.size)
        return {"correlation": correlation, "p_value": p_value}

    @staticmethod
    def _correlation_p_value(correlation: float, *, n: int) -> float:
        if n <= 3 or not math.isfinite(correlation):
            return 1.0
        clipped = min(0.999999999, max(-0.999999999, correlation))
        z = 0.5 * math.log((1 + clipped) / (1 - clipped)) * math.sqrt(max(1, n - 3))
        tail = 1.0 - NormalDist().cdf(abs(z))
        return float(max(0.0, min(1.0, 2.0 * tail)))

    def _bh_adjust(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return rows
        p_values = np.asarray([float(row.get("p_value") or 1.0) for row in rows], dtype=float)
        order = np.argsort(p_values)
        adjusted = np.empty_like(p_values)
        n = len(p_values)
        running = 1.0
        for offset, index in enumerate(order[::-1], start=1):
            rank = n - offset + 1
            value = p_values[index] * n / max(1, rank)
            running = min(running, value)
            adjusted[index] = min(1.0, running)
        for row, adj in zip(rows, adjusted, strict=False):
            row["padj"] = float(adj)
        rows.sort(key=lambda item: (float(item["padj"]), float(item["p_value"])))
        return rows


@dataclass(slots=True)
class _PreparedObservations:
    values: np.ndarray
    labels: list[str]
    feature_labels: list[str]
