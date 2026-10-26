from __future__ import annotations

import csv
import math
from typing import Any

from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.tabular import infer_delimiter, load_table
from gleskit_mcp.utils import clean_text, unique_preserve_order


class RNASEQAnalysisBackend:
    def analyze_result(
        self,
        *,
        result_path: str = "",
        table_text: str = "",
        label: str = "",
        comparison: str = "",
        gene_column: str = "",
        log2_fold_change_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        base_mean_column: str = "",
        dispersion_column: str = "",
        stat_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 0.0,
        log2_fold_change_threshold: float = 0.0,
        top_n: int = 25,
        enrichment_gene_limit: int = 100,
        focus_genes: list[str] | None = None,
        focus_gene_aliases: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        source = load_table(
            path=result_path,
            text=table_text,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        inferred_delimiter = source.delimiter
        rows = list(csv.DictReader(source.text.splitlines(), delimiter=inferred_delimiter))
        if not rows:
            raise ProviderError("RNA-seq analysis requires a non-empty differential-expression table.")

        header = [str(key) for key in rows[0].keys()]
        column_map = {
            "gene": _resolve_column(header, gene_column, _COLUMN_ALIASES["gene"], required=True),
            "log2_fold_change": _resolve_column(
                header,
                log2_fold_change_column,
                _COLUMN_ALIASES["log2_fold_change"],
                required=True,
            ),
            "padj": _resolve_column(header, padj_column, _COLUMN_ALIASES["padj"], required=False),
            "p_value": _resolve_column(header, p_value_column, _COLUMN_ALIASES["p_value"], required=False),
            "base_mean": _resolve_column(header, base_mean_column, _COLUMN_ALIASES["base_mean"], required=False),
            "dispersion": _resolve_column(header, dispersion_column, _COLUMN_ALIASES["dispersion"], required=False),
            "stat": _resolve_column(header, stat_column, _COLUMN_ALIASES["stat"], required=False),
        }

        parsed_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            gene_token = clean_text(row.get(column_map["gene"])) if column_map["gene"] else ""
            if not gene_token:
                continue
            parsed_rows.append(
                {
                    "row_index": index,
                    "gene": gene_token,
                    "log2_fold_change": _float_or_none(row.get(column_map["log2_fold_change"])) if column_map["log2_fold_change"] else None,
                    "padj": _float_or_none(row.get(column_map["padj"])) if column_map["padj"] else None,
                    "p_value": _float_or_none(row.get(column_map["p_value"])) if column_map["p_value"] else None,
                    "base_mean": _float_or_none(row.get(column_map["base_mean"])) if column_map["base_mean"] else None,
                    "dispersion": _float_or_none(row.get(column_map["dispersion"])) if column_map["dispersion"] else None,
                    "stat": _float_or_none(row.get(column_map["stat"])) if column_map["stat"] else None,
                }
            )

        if not parsed_rows:
            raise ProviderError("RNA-seq analysis did not find any rows with a usable gene identifier.")

        significance_mode = _significance_mode(parsed_rows)
        ranked_rows = [row for row in parsed_rows if row.get("log2_fold_change") is not None]
        significant_rows = [
            row
            for row in ranked_rows
            if _is_significant(
                row,
                significance_mode=significance_mode,
                padj_threshold=padj_threshold,
                p_value_threshold=p_value_threshold,
                base_mean_threshold=base_mean_threshold,
                log2_fold_change_threshold=log2_fold_change_threshold,
            )
        ]
        up_rows = [row for row in significant_rows if float(row.get("log2_fold_change") or 0.0) >= log2_fold_change_threshold]
        down_rows = [row for row in significant_rows if float(row.get("log2_fold_change") or 0.0) <= -log2_fold_change_threshold]

        up_rows.sort(key=_row_rank_key, reverse=True)
        down_rows.sort(key=_row_rank_key_negative, reverse=True)

        top_n = max(1, min(int(top_n), 100))
        enrichment_gene_limit = max(3, min(int(enrichment_gene_limit), 300))
        up_genes = unique_preserve_order(row["gene"] for row in up_rows[:enrichment_gene_limit])
        down_genes = unique_preserve_order(row["gene"] for row in down_rows[:enrichment_gene_limit])
        ranked_signature = _build_ranked_signature(parsed_rows)
        focus_rows = _build_focus_rows(
            parsed_rows,
            focus_genes=focus_genes or [],
            focus_gene_aliases=focus_gene_aliases or {},
        )

        return {
            "input": {
                "result_path": source.path,
                "source_kind": source.source_kind,
                "label": clean_text(label) or None,
                "comparison": clean_text(comparison) or None,
                "sheet_name": source.sheet_name,
                "header_row": source.header_row,
                "skip_rows": source.skip_rows,
            },
            "table": {
                "delimiter": inferred_delimiter,
                "row_count": len(parsed_rows),
                "column_map": column_map,
                "header": header,
                "available_sheets": source.available_sheets or [],
            },
            "summary": {
                "row_count": len(parsed_rows),
                "ranked_gene_count": len(ranked_rows),
                "significant_gene_count": len(significant_rows),
                "upregulated_count": len(up_rows),
                "downregulated_count": len(down_rows),
                "significance_mode": significance_mode,
                "padj_threshold": padj_threshold,
                "p_value_threshold": p_value_threshold,
                "base_mean_threshold": base_mean_threshold,
                "log2_fold_change_threshold": log2_fold_change_threshold,
            },
            "top_genes": {
                "upregulated": up_rows[:top_n],
                "downregulated": down_rows[:top_n],
            },
            "significant_gene_sets": {
                "upregulated": up_genes,
                "downregulated": down_genes,
                # Directional lists stay compact for automatic follow-up
                # enrichment, but the all-significant handoff is an exact DE
                # set for downstream overlap and enrichment comparisons.
                "all_significant": unique_preserve_order(row["gene"] for row in significant_rows),
            },
            "ranked_signature": ranked_signature,
            "focus_rows": focus_rows,
            "results": parsed_rows,
        }

_COLUMN_ALIASES = {
    "gene": [
        "gene",
        "rowid",
        "row_id",
        "unnamed0",
        "column1",
        "column_1",
        "gene_symbol",
        "genesymbol",
        "symbol",
        "gene_name",
        "external_gene_name",
        "hgnc_symbol",
        "geneid",
        "gene_id",
        "ensembl_gene_id",
        "feature",
        "target",
        "id",
    ],
    "log2_fold_change": [
        "log2foldchange",
        "log2fc",
        "log_fc",
        "logfc",
        "avg_log2fc",
        "avg_logfc",
        "lfc",
        "foldchange_log2",
    ],
    "padj": [
        "padj",
        "p_val_adj",
        "adjusted_p_value",
        "adjpval",
        "adj_pval",
        "adj_p_value",
        "fdr",
        "qvalue",
        "q_value",
        "fdr_bh",
    ],
    "p_value": [
        "pvalue",
        "p_value",
        "pval",
        "p_val",
    ],
        "base_mean": [
        "basemean",
        "mean_expression",
        "avgexpr",
        "average_expression",
        "mean",
    ],
    "dispersion": [
        "dispersion",
        "disp",
        "dispersion_estimate",
    ],
    "stat": [
        "stat",
        "statistic",
        "score",
        "t",
        "waldstat",
    ],
}


def _infer_delimiter(text: str) -> str:
    return infer_delimiter(text)


def _resolve_column(header: list[str], explicit: str, aliases: list[str], *, required: bool) -> str | None:
    normalized = {_normalize_header(token): token for token in header}
    explicit_token = clean_text(explicit)
    if explicit_token:
        normalized_explicit = _normalize_header(explicit_token)
        if normalized_explicit in normalized:
            return normalized[normalized_explicit]
        raise ProviderError(f"RNA-seq column '{explicit_token}' was not found in the table header.")
    for alias in aliases:
        normalized_alias = _normalize_header(alias)
        if normalized_alias and normalized_alias in normalized:
            return normalized[normalized_alias]
    if required:
        raise ProviderError(
            "RNA-seq analysis could not infer a required column from the table header. "
            f"Expected one of: {', '.join(aliases)}."
        )
    return None


def _normalize_header(value: str) -> str:
    return "".join(ch for ch in clean_text(value).lower() if ch.isalnum())


def _normalize_focus_token(value: str) -> str:
    token = clean_text(value)
    if token.upper().startswith("ENS") and "." in token:
        base, suffix = token.rsplit(".", 1)
        if suffix.isdigit():
            token = base
    return "".join(ch for ch in token.lower() if ch.isalnum())


def _build_focus_rows(
    rows: list[dict[str, Any]],
    *,
    focus_genes: list[str],
    focus_gene_aliases: dict[str, list[str]],
) -> list[dict[str, Any]]:
    queries = [clean_text(gene) for gene in focus_genes if clean_text(gene)]
    if not queries:
        return []

    row_lookup: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        token = _normalize_focus_token(str(row.get("gene") or ""))
        if not token:
            continue
        row_lookup.setdefault(token, []).append(row)

    focus_rows: list[dict[str, Any]] = []
    for query in queries:
        alias_candidates = [query, *(focus_gene_aliases.get(query) or [])]
        normalized_candidates: list[str] = []
        seen: set[str] = set()
        for candidate in alias_candidates:
            token = _normalize_focus_token(candidate)
            if token and token not in seen:
                seen.add(token)
                normalized_candidates.append(token)

        matched_row: dict[str, Any] | None = None
        matched_identifier = ""
        match_source = ""
        for candidate in normalized_candidates:
            matches = row_lookup.get(candidate) or []
            if not matches:
                continue
            matched_row = matches[0]
            matched_identifier = str(matched_row.get("gene") or "")
            if candidate == _normalize_focus_token(query):
                match_source = "direct"
            else:
                match_source = "alias"
            break

        focus_rows.append(
            {
                "query_gene": query,
                "matched": matched_row is not None,
                "matched_identifier": matched_identifier or None,
                "match_source": match_source or None,
                "key_metrics": {
                    "log2_fold_change": matched_row.get("log2_fold_change") if matched_row else None,
                    "padj": matched_row.get("padj") if matched_row else None,
                    "p_value": matched_row.get("p_value") if matched_row else None,
                    "base_mean": matched_row.get("base_mean") if matched_row else None,
                    "dispersion": matched_row.get("dispersion") if matched_row else None,
                    "stat": matched_row.get("stat") if matched_row else None,
                },
                "matched_row": matched_row,
            }
        )

    return focus_rows


def _significance_mode(rows: list[dict[str, Any]]) -> str:
    if any(row.get("padj") is not None for row in rows):
        return "padj"
    if any(row.get("p_value") is not None for row in rows):
        return "p_value"
    return "log2_fold_change_only"


def _is_significant(
    row: dict[str, Any],
    *,
    significance_mode: str,
    padj_threshold: float,
    p_value_threshold: float,
    base_mean_threshold: float,
    log2_fold_change_threshold: float,
) -> bool:
    log2fc = row.get("log2_fold_change")
    if log2fc is None or abs(float(log2fc)) < log2_fold_change_threshold:
        return False
    base_mean = row.get("base_mean")
    if base_mean_threshold > 0 and (base_mean is None or float(base_mean) < base_mean_threshold):
        return False
    if significance_mode == "padj":
        padj = row.get("padj")
        return padj is not None and float(padj) <= padj_threshold
    if significance_mode == "p_value":
        p_value = row.get("p_value")
        return p_value is not None and float(p_value) <= p_value_threshold
    return True


def _row_rank_key(row: dict[str, Any]) -> tuple[float, float]:
    return (
        abs(float(row.get("log2_fold_change") or 0.0)),
        -(float(row.get("padj") or row.get("p_value") or 1.0)),
    )


def _row_rank_key_negative(row: dict[str, Any]) -> tuple[float, float]:
    return (
        abs(float(row.get("log2_fold_change") or 0.0)),
        -(float(row.get("padj") or row.get("p_value") or 1.0)),
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _build_ranked_signature(rows: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    ranked: dict[str, float] = {}
    for row in rows:
        gene = clean_text(row.get("gene")).upper()
        if not gene:
            continue
        score = _gene_rank_score(row)
        if score is None:
            continue
        current = ranked.get(gene)
        if current is None or abs(score) > abs(current):
            ranked[gene] = score
    return [
        {"gene": gene, "score": score}
        for gene, score in sorted(ranked.items(), key=lambda item: item[1], reverse=True)
    ]


def _gene_rank_score(row: dict[str, Any]) -> float | None:
    stat = row.get("stat")
    if stat is not None:
        return float(stat)

    log2fc = row.get("log2_fold_change")
    if log2fc is None:
        return None

    log2fc_value = float(log2fc)
    if log2fc_value == 0:
        return 0.0

    significance = row.get("padj")
    if significance is None:
        significance = row.get("p_value")
    if significance is None:
        return log2fc_value

    significance_value = max(float(significance), 1e-300)
    score = -math.log10(significance_value)
    return score if log2fc_value > 0 else -score
