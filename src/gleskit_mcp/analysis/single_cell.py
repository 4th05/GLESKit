from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
from scipy import sparse

from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.tabular import load_table
from gleskit_mcp.utils import clean_text, normalize_gene_list, unique_preserve_order

try:  # pragma: no cover - import guarded for environments without anndata
    import anndata as ad
except ImportError:  # pragma: no cover
    ad = None


@dataclass(slots=True)
class PseudobulkDataset:
    counts: pd.DataFrame
    metadata: pd.DataFrame
    summary: dict[str, Any]


@dataclass(slots=True)
class _ClusterMarkerObservations:
    values: np.ndarray
    labels: list[str]
    feature_labels: list[str]


class SingleCellAnalysisBackend:
    def summarize_h5ad(
        self,
        *,
        h5ad_path: str,
        obs_preview_limit: int = 10,
        var_preview_limit: int = 10,
    ) -> dict[str, Any]:
        adata = self._read_h5ad(h5ad_path)
        obs_preview = {
            column: self._top_counts(adata.obs[column], limit=obs_preview_limit)
            for column in list(adata.obs.columns[:obs_preview_limit])
        }
        return {
            "summary": (
                f"Loaded single-cell object with {adata.n_obs} cells, {adata.n_vars} genes, "
                f"{len(adata.obs.columns)} obs column(s), and {len(adata.var.columns)} var column(s)."
            ),
            "provider": "single_cell",
            "analysis": "summary",
            "input": {
                "h5ad_path": str(Path(h5ad_path).expanduser().resolve()),
            },
            "object": {
                "cell_count": int(adata.n_obs),
                "gene_count": int(adata.n_vars),
                "obs_columns": [str(column) for column in adata.obs.columns],
                "var_columns": [str(column) for column in adata.var.columns],
                "layers": sorted(str(key) for key in adata.layers.keys()),
                "obsm_keys": sorted(str(key) for key in adata.obsm.keys()),
                "uns_keys": sorted(str(key) for key in adata.uns.keys()),
                "obs_preview": obs_preview,
                "var_preview": [str(name) for name in list(adata.var_names[:var_preview_limit])],
            },
        }

    def cell_type_deg_counts(
        self,
        *,
        deg_table_path: str,
        padj_threshold: float = 0.05,
        gene_column: str = "",
        cell_type_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        log2_fold_change_column: str = "",
        sheet_names: list[str] | None = None,
    ) -> dict[str, Any]:
        path = Path(clean_text(deg_table_path)).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Single-cell DEG table not found: {path}")
        if path.suffix.lower() in {".xlsx", ".xls"} and not clean_text(cell_type_column):
            return self._sheetwise_deg_counts(
                deg_table_path=str(path),
                padj_threshold=padj_threshold,
                gene_column=gene_column,
                padj_column=padj_column,
                p_value_column=p_value_column,
                log2_fold_change_column=log2_fold_change_column,
                requested_sheet_names=sheet_names or [],
            )

        table = load_table(path=str(path), dtype=str, keep_default_na=False)
        resolved_cell_type = self._resolve_column(table.frame, cell_type_column, required=True, label="cell-type")
        resolved_gene = self._resolve_column(table.frame, gene_column or str(table.frame.columns[0]), required=True, label="gene")
        resolved_padj = self._resolve_optional_numeric_column(
            table.frame,
            requested=padj_column,
            fallback_aliases=["p_val_adj", "padj", "adjusted_p_value", "fdr", "q_value"],
            required=not clean_text(p_value_column),
        )
        resolved_p_value = self._resolve_optional_numeric_column(
            table.frame,
            requested=p_value_column,
            fallback_aliases=["p_val", "pvalue", "p_value"],
            required=not resolved_padj,
        )
        working = table.frame.copy()
        working[resolved_gene] = working[resolved_gene].map(clean_text)
        if resolved_padj:
            working["_sig"] = pd.to_numeric(working[resolved_padj], errors="coerce") <= padj_threshold
        else:
            working["_sig"] = pd.to_numeric(working[resolved_p_value], errors="coerce") <= padj_threshold
        rows = []
        for cell_type, subset in working.groupby(resolved_cell_type):
            significant = subset[subset["_sig"]]
            rows.append(
                {
                    "cell_type": clean_text(cell_type),
                    "significant_gene_count": int(significant[resolved_gene].map(clean_text).replace("", np.nan).dropna().nunique()),
                }
            )
        rows.sort(key=lambda item: (-item["significant_gene_count"], item["cell_type"]))
        question_ready = _build_cell_type_deg_count_question_ready(rows, padj_threshold=padj_threshold)
        summary = f"Computed significant DEG counts for {len(rows)} cell type(s)."
        if question_ready["top_cell_type"]:
            summary = (
                f"{summary} Top cell type: {question_ready['top_cell_type']} "
                f"({question_ready['top_significant_gene_count']} significant genes)."
            )
        return {
            "summary": summary,
            "provider": "single_cell",
            "analysis": "cell_type_deg_counts",
            "input": {
                "deg_table_path": str(path.resolve()),
                "cell_type_column": resolved_cell_type,
                "gene_column": resolved_gene,
                "padj_threshold": float(padj_threshold),
            },
            "cell_type_counts": rows,
            "question_ready": question_ready,
        }

    def gene_cell_type_lookup(
        self,
        *,
        deg_table_path: str,
        gene: str,
        gene_column: str = "",
        cell_type_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        log2_fold_change_column: str = "",
        padj_threshold: float = 0.05,
        sheet_names: list[str] | None = None,
    ) -> dict[str, Any]:
        query_gene = normalize_gene_list([gene])
        if not query_gene:
            raise ProviderError("Single-cell gene lookup requires a gene symbol.")
        target_gene = query_gene[0]
        path = Path(clean_text(deg_table_path)).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Single-cell DEG table not found: {path}")
        if path.suffix.lower() in {".xlsx", ".xls"} and not clean_text(cell_type_column):
            return self._sheetwise_gene_lookup(
                deg_table_path=str(path),
                gene=target_gene,
                gene_column=gene_column,
                padj_column=padj_column,
                p_value_column=p_value_column,
                log2_fold_change_column=log2_fold_change_column,
                padj_threshold=padj_threshold,
                requested_sheet_names=sheet_names or [],
            )

        table = load_table(path=str(path), dtype=str, keep_default_na=False)
        resolved_cell_type = self._resolve_column(table.frame, cell_type_column, required=True, label="cell-type")
        resolved_gene = self._resolve_column(table.frame, gene_column or str(table.frame.columns[0]), required=True, label="gene")
        resolved_padj = self._resolve_optional_numeric_column(
            table.frame,
            requested=padj_column,
            fallback_aliases=["p_val_adj", "padj", "adjusted_p_value", "fdr", "q_value"],
            required=not clean_text(p_value_column),
        )
        resolved_p_value = self._resolve_optional_numeric_column(
            table.frame,
            requested=p_value_column,
            fallback_aliases=["p_val", "pvalue", "p_value"],
            required=not resolved_padj,
        )
        resolved_log2fc = self._resolve_optional_numeric_column(
            table.frame,
            requested=log2_fold_change_column,
            fallback_aliases=["avg_log2FC", "log2FoldChange", "log2_fold_change", "avg_logfc"],
            required=False,
        )
        working = table.frame.copy()
        working[resolved_gene] = working[resolved_gene].map(lambda value: clean_text(value).upper())
        hits = working[working[resolved_gene] == target_gene].copy()
        rows = []
        for _, row in hits.iterrows():
            metric_value = None
            if resolved_padj:
                metric_value = pd.to_numeric(pd.Series([row[resolved_padj]]), errors="coerce").iloc[0]
            elif resolved_p_value:
                metric_value = pd.to_numeric(pd.Series([row[resolved_p_value]]), errors="coerce").iloc[0]
            rows.append(
                {
                    "cell_type": clean_text(row[resolved_cell_type]),
                    "gene": target_gene,
                    "padj": float(metric_value) if metric_value is not None and pd.notna(metric_value) else None,
                    "p_value": float(pd.to_numeric(pd.Series([row[resolved_p_value]]), errors="coerce").iloc[0]) if resolved_p_value else None,
                    "log2_fold_change": float(pd.to_numeric(pd.Series([row[resolved_log2fc]]), errors="coerce").iloc[0]) if resolved_log2fc else None,
                    "significant": bool(metric_value is not None and pd.notna(metric_value) and float(metric_value) <= padj_threshold),
                }
            )
        rows.sort(key=lambda item: (item["padj"] is None, item["padj"] if item["padj"] is not None else 1.0, item["cell_type"]))
        return {
            "summary": f"Looked up {target_gene} across {len(rows)} cell type row(s).",
            "provider": "single_cell",
            "analysis": "gene_cell_type_lookup",
            "input": {
                "deg_table_path": str(path.resolve()),
                "gene": target_gene,
                "padj_threshold": float(padj_threshold),
            },
            "matches": rows,
        }

    def cluster_marker_analysis(
        self,
        *,
        h5ad_path: str = "",
        matrix_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        cluster_labels: dict[str, Any] | list[dict[str, Any]] | list[Any] | None = None,
        top_n: int = 25,
        test: str = "welch",
        padj_threshold: float = 0.05,
        species: str = "human",
    ) -> dict[str, Any]:
        if clean_text(h5ad_path):
            adata = self._read_h5ad(h5ad_path)
            count_matrix = self._select_count_matrix(adata, layer="", use_raw=False)
            values = count_matrix.toarray() if sparse.issparse(count_matrix) else np.asarray(count_matrix)
            obs = _ClusterMarkerObservations(
                values=np.asarray(values, dtype=float),
                labels=[clean_text(value) or f"cell_{index + 1}" for index, value in enumerate(adata.obs_names)],
                feature_labels=[clean_text(value) or f"feature_{index + 1}" for index, value in enumerate(adata.var_names)],
            )
            source_path = str(Path(h5ad_path).expanduser().resolve())
        else:
            from gleskit_mcp.analysis.expression_matrix import ExpressionMatrixAnalysisBackend

            matrix_backend = ExpressionMatrixAnalysisBackend()
            matrix = matrix_backend.load_matrix(
                matrix_path=matrix_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=analysis_axis,
            )
            obs = matrix_backend._observations(matrix, analysis_axis=analysis_axis)
            source_path = str(Path(matrix_path).expanduser().resolve()) if matrix_path else ""
        cluster_map = _normalize_cluster_labels(cluster_labels, obs.labels)
        cluster_ids = _sorted_cluster_ids(cluster_map.values())
        if len(cluster_ids) < 2:
            raise ProviderError("Cluster marker analysis requires at least two clusters.")

        cluster_summaries: list[dict[str, Any]] = []
        marker_rows: list[dict[str, Any]] = []
        marker_gene_sets: dict[str, list[str]] = {}
        significant_marker_gene_sets: dict[str, list[str]] = {}
        for cluster_id in cluster_ids:
            member_indices = [index for index, label in enumerate(obs.labels) if cluster_map.get(label) == cluster_id]
            outside_indices = [index for index, label in enumerate(obs.labels) if cluster_map.get(label) != cluster_id]
            if len(member_indices) < 1 or len(outside_indices) < 1:
                continue

            per_feature_rows: list[dict[str, Any]] = []
            p_values: list[float] = []
            for feature_index, feature_label in enumerate(obs.feature_labels):
                in_values = np.asarray(obs.values[member_indices, feature_index], dtype=float)
                out_values = np.asarray(obs.values[outside_indices, feature_index], dtype=float)
                mean_in = float(np.mean(in_values)) if in_values.size else 0.0
                mean_out = float(np.mean(out_values)) if out_values.size else 0.0
                effect_size = _cohen_d(in_values, out_values)
                if mean_in > mean_out:
                    alternative = "greater"
                elif mean_in < mean_out:
                    alternative = "less"
                else:
                    alternative = "two-sided"
                p_value = _group_comparison_p_value(in_values, out_values, test=test, alternative=alternative)
                p_values.append(p_value)
                per_feature_rows.append(
                    {
                        "cluster": cluster_id,
                        "feature": feature_label,
                        "mean_in_cluster": mean_in,
                        "mean_outside": mean_out,
                        "difference": float(mean_in - mean_out),
                        "effect_size": effect_size,
                        "p_value": p_value,
                        "cluster_size": len(member_indices),
                        "outside_size": len(outside_indices),
                    }
                )

            positive_indices = [index for index, row in enumerate(per_feature_rows) if float(row["difference"]) > 0]
            positive_index_map = {index: rank for rank, index in enumerate(positive_indices)}
            positive_p_values = [p_values[index] for index in positive_indices]
            positive_padj_bh = _adjust_p_values(positive_p_values, method="bh") if positive_p_values else np.asarray([], dtype=float)
            positive_padj_by = _adjust_p_values(positive_p_values, method="by") if positive_p_values else np.asarray([], dtype=float)
            positive_padj_bonf = _adjust_p_values(positive_p_values, method="bonferroni") if positive_p_values else np.asarray([], dtype=float)
            for index, row in enumerate(per_feature_rows):
                if index in positive_index_map:
                    rank = positive_index_map[index]
                    bh = positive_padj_bh[rank]
                    by = positive_padj_by[rank]
                    bonf = positive_padj_bonf[rank]
                else:
                    bh = by = bonf = 1.0
                row["padj_bh"] = float(bh)
                row["padj_by"] = float(by)
                row["padj_bonferroni"] = float(bonf)
                row["significant_bh"] = bool(float(bh) <= padj_threshold and float(row["difference"]) > 0)
                row["significant_by"] = bool(float(by) <= padj_threshold and float(row["difference"]) > 0)
                row["significant_bonferroni"] = bool(float(bonf) <= padj_threshold and float(row["difference"]) > 0)

            per_feature_rows.sort(
                key=lambda row: (
                    float(row.get("padj_bh") or 1.0),
                    -float(row.get("effect_size") or 0.0),
                    row.get("feature") or "",
                )
            )
            significant_rows = [
                row
                for row in per_feature_rows
                if bool(row.get("significant_bh")) and float(row.get("difference") or 0.0) > 0
            ]
            top_rows = per_feature_rows[: max(1, min(int(top_n), len(per_feature_rows)))]
            top_marker_genes = unique_preserve_order(
                row["feature"] for row in top_rows if float(row.get("effect_size") or 0.0) > 0
            )
            significant_marker_genes = unique_preserve_order(row["feature"] for row in significant_rows)
            marker_gene_sets[str(cluster_id)] = top_marker_genes
            significant_marker_gene_sets[str(cluster_id)] = significant_marker_genes
            cluster_summaries.append(
                {
                    "cluster": cluster_id,
                    "cluster_size": len(member_indices),
                    "outside_size": len(outside_indices),
                    "feature_count": len(per_feature_rows),
                    "marker_count_bh": int(
                        sum(
                            1
                            for row in per_feature_rows
                            if float(row["difference"]) > 0 and float(row["padj_bh"]) <= padj_threshold
                        )
                    ),
                    "significant_marker_gene_count": len(significant_marker_genes),
                    "significant_marker_genes": significant_marker_genes,
                    "top_marker_gene_count": len(top_marker_genes),
                    "top_marker_genes": top_marker_genes,
                    "top_markers": top_rows,
                }
            )
            marker_rows.extend(top_rows)

        cluster_summaries.sort(key=lambda row: (-int(row["marker_count_bh"]), -int(row["cluster_size"]), str(row["cluster"])))
        return {
            "summary": (
                f"Computed cluster markers for {len(cluster_summaries)} cluster(s) across {len(obs.feature_labels)} feature(s)."
            ),
            "provider": "single_cell",
            "analysis": "cluster_marker_analysis",
            "input": {
                "h5ad_path": source_path if clean_text(h5ad_path) else "",
                "matrix_path": source_path if not clean_text(h5ad_path) else "",
                "sheet_name": clean_text(sheet_name) or None,
                "header_row": int(header_row),
                "skip_rows": int(skip_rows),
                "analysis_axis": clean_text(analysis_axis) or "columns",
                "top_n": int(top_n),
                "test": clean_text(test).lower() or "welch",
                "padj_threshold": float(padj_threshold),
                "species": clean_text(species) or "human",
            },
            "cluster_count": len(cluster_summaries),
            "feature_count": len(obs.feature_labels),
            "cluster_markers": cluster_summaries,
            "markers": marker_rows,
            "marker_gene_sets": marker_gene_sets,
            "significant_marker_gene_sets": significant_marker_gene_sets,
            "significant_marker_count_bh": int(sum(row["marker_count_bh"] for row in cluster_summaries)),
        }

    def build_pseudobulk_dataset(
        self,
        *,
        h5ad_path: str,
        sample_column: str,
        condition_column: str,
        cell_type_column: str = "",
        cell_type_values: list[str] | None = None,
        layer: str = "",
        use_raw: bool = False,
    ) -> PseudobulkDataset:
        adata = self._read_h5ad(h5ad_path)
        resolved_sample = self._resolve_obs_column(adata, sample_column, label="sample")
        resolved_condition = self._resolve_obs_column(adata, condition_column, label="condition")
        working = adata
        selected_types = [clean_text(value) for value in (cell_type_values or []) if clean_text(value)]
        resolved_cell_type = clean_text(cell_type_column)
        if resolved_cell_type:
            resolved_cell_type = self._resolve_obs_column(adata, resolved_cell_type, label="cell type")
            if selected_types:
                mask = adata.obs[resolved_cell_type].astype(str).isin(selected_types)
                working = adata[mask].copy()
        if working.n_obs == 0:
            raise ProviderError("Single-cell pseudobulk selection produced zero cells.")

        matrix = self._select_count_matrix(working, layer=layer, use_raw=use_raw)
        if not self._looks_like_raw_counts(matrix):
            raise ProviderError("Single-cell pseudobulk requires raw count-like values; log-transformed matrices are not supported.")

        obs = working.obs.copy()
        obs["_sample_id"] = obs[resolved_sample].map(clean_text)
        obs["_condition_id"] = obs[resolved_condition].map(clean_text)
        obs = obs[(obs["_sample_id"].str.len() > 0) & (obs["_condition_id"].str.len() > 0)]
        if obs.empty:
            raise ProviderError("Single-cell pseudobulk requires non-empty sample and condition annotations.")
        usable_index = obs.index
        working = working[usable_index].copy()
        matrix = self._select_count_matrix(working, layer=layer, use_raw=use_raw)
        obs = working.obs.copy()
        obs["_sample_id"] = obs[resolved_sample].map(clean_text)
        obs["_condition_id"] = obs[resolved_condition].map(clean_text)

        grouped = obs.groupby("_sample_id", sort=True, observed=False)
        count_columns: dict[str, np.ndarray] = {}
        metadata_rows: list[dict[str, Any]] = []
        for sample_id, subset in grouped:
            indices = np.asarray([working.obs_names.get_loc(index) for index in subset.index], dtype=int)
            column = self._sum_rows(matrix, indices)
            count_columns[sample_id] = column
            condition_values = sorted({clean_text(value) for value in subset["_condition_id"] if clean_text(value)})
            if len(condition_values) != 1:
                raise ProviderError(f"Pseudobulk sample '{sample_id}' maps to multiple condition labels.")
            metadata_rows.append(
                {
                    "sample": sample_id,
                    "condition": condition_values[0],
                    "cell_count": int(subset.shape[0]),
                }
            )

        if len(count_columns) < 2:
            raise ProviderError("Pseudobulk DESeq2 requires at least 2 aggregated samples.")
        counts = pd.DataFrame(count_columns, index=[str(gene) for gene in working.var_names])
        counts.insert(0, "gene", counts.index)
        counts = counts.reset_index(drop=True)
        metadata = pd.DataFrame(metadata_rows).sort_values("sample").reset_index(drop=True)
        summary = {
            "sample_count": int(metadata.shape[0]),
            "condition_count": int(metadata["condition"].nunique()),
            "cell_count": int(working.n_obs),
            "gene_count": int(working.n_vars),
            "sample_column": resolved_sample,
            "condition_column": resolved_condition,
            "cell_type_column": resolved_cell_type or None,
            "selected_cell_types": selected_types,
            "layer": clean_text(layer) or None,
            "used_raw": bool(use_raw),
        }
        return PseudobulkDataset(counts=counts, metadata=metadata, summary=summary)

    def _sheetwise_deg_counts(
        self,
        *,
        deg_table_path: str,
        padj_threshold: float,
        gene_column: str,
        padj_column: str,
        p_value_column: str,
        log2_fold_change_column: str,
        requested_sheet_names: list[str],
    ) -> dict[str, Any]:
        rows = []
        with pd.ExcelFile(deg_table_path, engine="openpyxl") as workbook:
            sheet_names = requested_sheet_names or list(workbook.sheet_names)
            for sheet_name in sheet_names:
                frame = pd.read_excel(workbook, sheet_name=sheet_name, dtype=str)
                if frame.empty:
                    continue
                resolved_gene = self._resolve_column(frame, gene_column or str(frame.columns[0]), required=True, label="gene")
                resolved_padj = self._resolve_optional_numeric_column(
                    frame,
                    requested=padj_column,
                    fallback_aliases=["p_val_adj", "padj", "adjusted_p_value", "fdr", "q_value"],
                    required=not clean_text(p_value_column),
                )
                resolved_p_value = self._resolve_optional_numeric_column(
                    frame,
                    requested=p_value_column,
                    fallback_aliases=["p_val", "pvalue", "p_value"],
                    required=not resolved_padj,
                )
                if resolved_padj:
                    significant = pd.to_numeric(frame[resolved_padj], errors="coerce") <= padj_threshold
                else:
                    significant = pd.to_numeric(frame[resolved_p_value], errors="coerce") <= padj_threshold
                genes = frame.loc[significant, resolved_gene].map(clean_text)
                rows.append(
                    {
                        "cell_type": sheet_name,
                        "significant_gene_count": int(genes.replace("", np.nan).dropna().nunique()),
                    }
                )
        rows.sort(key=lambda item: (-item["significant_gene_count"], item["cell_type"]))
        question_ready = _build_cell_type_deg_count_question_ready(rows, padj_threshold=padj_threshold)
        summary = f"Computed significant DEG counts for {len(rows)} cell type sheet(s)."
        if question_ready["top_cell_type"]:
            summary = (
                f"{summary} Top cell type: {question_ready['top_cell_type']} "
                f"({question_ready['top_significant_gene_count']} significant genes)."
            )
        return {
            "summary": summary,
            "provider": "single_cell",
            "analysis": "cell_type_deg_counts",
            "input": {
                "deg_table_path": str(Path(deg_table_path).expanduser().resolve()),
                "padj_threshold": float(padj_threshold),
                "sheet_count": len(sheet_names),
            },
            "cell_type_counts": rows,
            "question_ready": question_ready,
        }

    def _sheetwise_gene_lookup(
        self,
        *,
        deg_table_path: str,
        gene: str,
        gene_column: str,
        padj_column: str,
        p_value_column: str,
        log2_fold_change_column: str,
        padj_threshold: float,
        requested_sheet_names: list[str],
    ) -> dict[str, Any]:
        rows = []
        with pd.ExcelFile(deg_table_path, engine="openpyxl") as workbook:
            sheet_names = requested_sheet_names or list(workbook.sheet_names)
            for sheet_name in sheet_names:
                frame = pd.read_excel(workbook, sheet_name=sheet_name, dtype=str)
                if frame.empty:
                    continue
                resolved_gene = self._resolve_column(frame, gene_column or str(frame.columns[0]), required=True, label="gene")
                resolved_padj = self._resolve_optional_numeric_column(
                    frame,
                    requested=padj_column,
                    fallback_aliases=["p_val_adj", "padj", "adjusted_p_value", "fdr", "q_value"],
                    required=not clean_text(p_value_column),
                )
                resolved_p_value = self._resolve_optional_numeric_column(
                    frame,
                    requested=p_value_column,
                    fallback_aliases=["p_val", "pvalue", "p_value"],
                    required=not resolved_padj,
                )
                resolved_log2fc = self._resolve_optional_numeric_column(
                    frame,
                    requested=log2_fold_change_column,
                    fallback_aliases=["avg_log2FC", "log2FoldChange", "log2_fold_change", "avg_logfc"],
                    required=False,
                )
                working = frame.copy()
                working[resolved_gene] = working[resolved_gene].map(lambda value: clean_text(value).upper())
                hits = working[working[resolved_gene] == gene]
                if hits.empty:
                    rows.append(
                        {
                            "cell_type": sheet_name,
                            "gene": gene,
                            "padj": None,
                            "p_value": None,
                            "log2_fold_change": None,
                            "significant": False,
                        }
                    )
                    continue
                row = hits.iloc[0]
                metric = None
                if resolved_padj:
                    metric = pd.to_numeric(pd.Series([row[resolved_padj]]), errors="coerce").iloc[0]
                elif resolved_p_value:
                    metric = pd.to_numeric(pd.Series([row[resolved_p_value]]), errors="coerce").iloc[0]
                rows.append(
                    {
                        "cell_type": sheet_name,
                        "gene": gene,
                        "padj": float(metric) if metric is not None and pd.notna(metric) else None,
                        "p_value": float(pd.to_numeric(pd.Series([row[resolved_p_value]]), errors="coerce").iloc[0]) if resolved_p_value else None,
                        "log2_fold_change": float(pd.to_numeric(pd.Series([row[resolved_log2fc]]), errors="coerce").iloc[0]) if resolved_log2fc else None,
                        "significant": bool(metric is not None and pd.notna(metric) and float(metric) <= padj_threshold),
                    }
                )
        rows.sort(key=lambda item: (item["padj"] is None, item["padj"] if item["padj"] is not None else 1.0, item["cell_type"]))
        return {
            "summary": f"Looked up {gene} across {len(rows)} cell type sheet(s).",
            "provider": "single_cell",
            "analysis": "gene_cell_type_lookup",
            "input": {
                "deg_table_path": str(Path(deg_table_path).expanduser().resolve()),
                "gene": gene,
                "padj_threshold": float(padj_threshold),
                "sheet_count": len(sheet_names),
            },
            "matches": rows,
        }
    def _read_h5ad(self, h5ad_path: str):
        if ad is None:
            raise ProviderError("Single-cell analysis requires the Python package 'anndata' to be installed.")
        cleaned = clean_text(h5ad_path)
        if not cleaned:
            raise ProviderError("Single-cell analysis requires an h5ad_path.")
        path = Path(cleaned).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Single-cell h5ad file not found: {path}")
        return ad.read_h5ad(path)

    @staticmethod
    def _top_counts(series: pd.Series, *, limit: int) -> list[dict[str, Any]]:
        values = [clean_text(value) for value in series.astype(str)]
        counts = Counter(value for value in values if value)
        return [
            {"value": value, "count": int(count)}
            for value, count in counts.most_common(max(1, int(limit)))
        ]

    @staticmethod
    def _resolve_column(frame: pd.DataFrame, requested: str, *, required: bool, label: str) -> str:
        token = clean_text(requested)
        if token and token in frame.columns:
            return token
        if required:
            raise ProviderError(f"Single-cell analysis requires a valid {label} column.")
        return ""

    @staticmethod
    def _resolve_optional_numeric_column(
        frame: pd.DataFrame,
        *,
        requested: str,
        fallback_aliases: list[str],
        required: bool,
    ) -> str:
        token = clean_text(requested)
        if token and token in frame.columns:
            return token
        normalized = {SingleCellAnalysisBackend._normalize_header(column): str(column) for column in frame.columns}
        for alias in fallback_aliases:
            normalized_alias = SingleCellAnalysisBackend._normalize_header(alias)
            if normalized_alias in normalized:
                return normalized[normalized_alias]
        if required:
            raise ProviderError("Single-cell analysis could not identify the requested numeric column.")
        return ""

    @staticmethod
    def _normalize_header(value: str) -> str:
        return "".join(ch for ch in clean_text(value).lower() if ch.isalnum())

    @staticmethod
    def _resolve_obs_column(adata, requested: str, *, label: str) -> str:
        token = clean_text(requested)
        if token and token in adata.obs.columns:
            return token
        raise ProviderError(f"Single-cell analysis requires a valid obs {label} column.")

    @staticmethod
    def _select_count_matrix(adata, *, layer: str, use_raw: bool):
        if clean_text(layer):
            if clean_text(layer) not in adata.layers:
                raise ProviderError(f"Single-cell layer '{clean_text(layer)}' was not found.")
            return adata.layers[clean_text(layer)]
        if use_raw:
            if adata.raw is None:
                raise ProviderError("Single-cell analysis requested raw counts but adata.raw is unavailable.")
            return adata.raw.X
        return adata.X

    @staticmethod
    def _looks_like_raw_counts(matrix: Any) -> bool:
        if sparse.issparse(matrix):
            data = np.asarray(matrix.data, dtype=float)
        else:
            data = np.asarray(matrix, dtype=float).reshape(-1)
        if data.size == 0:
            return False
        finite = data[np.isfinite(data)]
        if finite.size == 0:
            return False
        if np.min(finite) < 0:
            return False
        sample = finite[: min(10000, finite.size)]
        return bool(np.all(np.abs(sample - np.round(sample)) < 1e-6))

    @staticmethod
    def _sum_rows(matrix: Any, indices: np.ndarray) -> np.ndarray:
        if sparse.issparse(matrix):
            column = np.asarray(matrix[indices].sum(axis=0)).reshape(-1)
        else:
            column = np.asarray(matrix[indices], dtype=float).sum(axis=0)
        return np.asarray(column, dtype=float)


def _build_cell_type_deg_count_question_ready(
    rows: list[dict[str, Any]],
    *,
    padj_threshold: float,
) -> dict[str, Any]:
    top_row = rows[0] if rows else {}
    return {
        "top_cell_type": clean_text(top_row.get("cell_type")) or None,
        "top_significant_gene_count": int(top_row.get("significant_gene_count") or 0) if top_row else 0,
        "ranked_cell_type_count": len(rows),
        "padj_threshold": float(padj_threshold),
    }


def _normalize_cluster_labels(
    cluster_labels: dict[str, Any] | list[dict[str, Any]] | list[Any] | None,
    labels: list[str],
) -> dict[str, Any]:
    if isinstance(cluster_labels, dict):
        return {clean_text(key): _coerce_cluster_id(value) for key, value in cluster_labels.items() if clean_text(key)}
    if isinstance(cluster_labels, list):
        if cluster_labels and all(isinstance(item, dict) for item in cluster_labels):
            mapping: dict[str, Any] = {}
            for row in cluster_labels:
                if not isinstance(row, dict):
                    continue
                label = clean_text(row.get("label") or row.get("sample") or row.get("cell"))
                cluster = row.get("cluster") or row.get("cluster_id") or row.get("assignment")
                if label:
                    mapping[label] = _coerce_cluster_id(cluster)
            return mapping
        if len(cluster_labels) != len(labels):
            raise ProviderError("Cluster label list must match the number of matrix observations.")
        return {clean_text(label): _coerce_cluster_id(cluster) for label, cluster in zip(labels, cluster_labels, strict=False)}
    raise ProviderError("Cluster marker analysis requires cluster labels.")


def _normalize_cluster_id(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    token = clean_text(value)
    if not token:
        return ""
    try:
        return int(token)
    except ValueError:
        return token


def _coerce_cluster_id(value: Any) -> Any:
    return _normalize_cluster_id(value)


def _group_comparison_p_value(
    in_values: np.ndarray,
    out_values: np.ndarray,
    *,
    test: str,
    alternative: str = "two-sided",
) -> float:
    cleaned_test = clean_text(test).lower() or "welch"
    cleaned_alternative = clean_text(alternative).lower() or "two-sided"
    if in_values.size == 0 or out_values.size == 0:
        return 1.0
    if cleaned_test in {"t", "student"}:
        statistic, p_value = stats.ttest_ind(
            in_values,
            out_values,
            equal_var=True,
            nan_policy="omit",
            alternative=cleaned_alternative,
        )
    elif cleaned_test in {"mannwhitney", "mann-whitney", "wilcoxon", "mw", "u"}:
        statistic, p_value = stats.mannwhitneyu(in_values, out_values, alternative=cleaned_alternative)
    else:
        statistic, p_value = stats.ttest_ind(
            in_values,
            out_values,
            equal_var=False,
            nan_policy="omit",
            alternative=cleaned_alternative,
        )
    if not np.isfinite(float(p_value)):
        return 1.0
    return float(p_value)


def _cohen_d(left: np.ndarray, right: np.ndarray) -> float:
    if left.size == 0 or right.size == 0:
        return 0.0
    mean_diff = float(np.mean(left) - np.mean(right))
    left_var = float(np.var(left, ddof=1)) if left.size > 1 else 0.0
    right_var = float(np.var(right, ddof=1)) if right.size > 1 else 0.0
    denom_numerator = ((left.size - 1) * left_var) + ((right.size - 1) * right_var)
    denom_denominator = max(1, left.size + right.size - 2)
    pooled = denom_numerator / denom_denominator if denom_denominator else 0.0
    if pooled <= 0 or not np.isfinite(pooled):
        return mean_diff
    return float(mean_diff / np.sqrt(pooled))


def _adjust_p_values(values: list[float], *, method: str) -> np.ndarray:
    p_values = np.asarray(values, dtype=float)
    n = p_values.size
    if n == 0:
        return np.asarray([], dtype=float)
    method = clean_text(method).lower()
    if method == "bonferroni":
        return np.clip(p_values * n, 0.0, 1.0)
    order = np.argsort(p_values)
    adjusted = np.empty_like(p_values)
    running = 1.0
    harmonic = sum(1.0 / index for index in range(1, n + 1)) if method == "by" else 1.0
    for offset, index in enumerate(order[::-1], start=1):
        rank = n - offset + 1
        raw = p_values[index] * n * harmonic / max(1, rank)
        running = min(running, raw)
        adjusted[index] = min(1.0, running)
    return adjusted


def _sorted_cluster_ids(values: Any) -> list[Any]:
    def _sort_key(token: Any) -> tuple[int, str]:
        if isinstance(token, int):
            return (0, f"{token:020d}")
        try:
            return (0, f"{int(token):020d}")
        except Exception:
            return (1, clean_text(token))

    return sorted({value for value in values if clean_text(value) != ""}, key=_sort_key)
