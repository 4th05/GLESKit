from __future__ import annotations

import csv
import math
from typing import Any

from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.tabular import load_table
from gleskit_mcp.utils import clean_text


class DifferentialResultBackend:
    """Parse precomputed, modality-neutral differential-result tables.

    Biological identifier resolution and pathway interpretation deliberately live
    in the service layer.  This backend only establishes an auditable tabular
    contract shared by transcriptomics and proteomics results.
    """

    _ALIASES = {
        "identifier": ("gene", "gene_symbol", "symbol", "gene_id", "ensembl_gene_id", "entrez", "uniprot", "accession", "protein", "protein_id", "protein_accession", "id"),
        "effect": ("log2foldchange", "log2fc", "logfc", "log_fc", "lfc", "foldchange_log2"),
        "ratio": ("abundanceratio", "ratio", "foldchange", "fc", "abundancefoldchange"),
        "padj": ("padj", "adjusted_p_value", "adj_p_value", "fdr", "qvalue", "q_value", "p_val_adj"),
        "p_value": ("pvalue", "p_value", "pval", "p_val"),
        "stat": ("stat", "statistic", "score"),
    }

    @staticmethod
    def _token(value: Any) -> str:
        return "".join(character for character in clean_text(value).lower() if character.isalnum())

    def _column(self, header: list[str], explicit: str, role: str, *, required: bool) -> str:
        if clean_text(explicit):
            if explicit not in header:
                raise ProviderError(f"Requested {role} column '{explicit}' was not found.")
            return explicit
        matches = [column for column in header if self._token(column) in self._ALIASES[role]]
        if len(matches) == 1:
            return matches[0]
        if required:
            detail = "none" if not matches else ", ".join(matches)
            raise ProviderError(f"Provide an explicit {role} column; automatic detection found {detail}.")
        return ""

    def _effect_column(self, header: list[str], explicit: str, *, scale: str) -> str:
        if clean_text(explicit):
            if explicit not in header:
                raise ProviderError(f"Requested effect column '{explicit}' was not found.")
            return explicit
        roles = ("effect",) if scale == "log2_fold_change" else ("ratio", "effect")
        matches = [column for column in header if any(self._matches_role(column, role) for role in roles)]
        if len(matches) == 1:
            return matches[0]
        detail = "none" if not matches else ", ".join(matches)
        raise ProviderError(f"Provide an explicit effect column; automatic detection found {detail}.")

    def _matches_role(self, column: str, role: str) -> bool:
        token = self._token(column)
        if token in self._ALIASES[role]:
            return True
        if role == "ratio":
            return (
                ("ratio" in token or token.endswith("foldchange") or token.startswith("foldchange"))
                and not any(marker in token for marker in ("pvalue", "qvalue", "variability", "cv"))
            )
        if role == "padj":
            return any(marker in token for marker in ("adjustedpvalue", "adjpvalue", "pvalueadj", "qvalue", "fdr"))
        if role == "p_value":
            return "pvalue" in token and not any(marker in token for marker in ("adjusted", "adj", "qvalue", "fdr"))
        return False

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _condition_token(value: Any) -> str:
        return "".join(character for character in clean_text(value).casefold() if character.isalnum())

    def _normalize_effect_definition(
        self,
        effect_definition: dict[str, Any] | None,
        *,
        positive_condition: str,
        reference_condition: str,
    ) -> dict[str, Any]:
        """Validate an explicit, reproducible mapping from a table effect to a contrast.

        The function intentionally does not extract group meaning from a column
        name.  A header such as ``HCA/HCC`` can be recorded in source_formula,
        but only declared semantic numerator/denominator conditions determine
        whether the analysis must retain or invert its sign.
        """
        raw = dict(effect_definition or {})
        scale = clean_text(raw.get("scale")).lower() or "log2_fold_change"
        aliases = {
            "log2fc": "log2_fold_change",
            "logfc": "log2_fold_change",
            "log2_foldchange": "log2_fold_change",
            "ratio": "linear_ratio",
            "fold_change_ratio": "linear_ratio",
        }
        scale = aliases.get(scale, scale)
        if scale not in {"log2_fold_change", "linear_ratio"}:
            raise ProviderError("effect_definition.scale must be 'log2_fold_change' or 'linear_ratio'.")

        numerator = clean_text(raw.get("numerator_condition"))
        denominator = clean_text(raw.get("denominator_condition"))
        direction = clean_text(raw.get("direction")).lower()
        positive_token = self._condition_token(positive_condition)
        reference_token = self._condition_token(reference_condition)
        transform = "identity"

        if numerator or denominator:
            if not numerator or not denominator:
                raise ProviderError(
                    "effect_definition requires both numerator_condition and denominator_condition when either is supplied."
                )
            numerator_token = self._condition_token(numerator)
            denominator_token = self._condition_token(denominator)
            if numerator_token == positive_token and denominator_token == reference_token:
                transform = "identity"
            elif numerator_token == reference_token and denominator_token == positive_token:
                transform = "invert"
            else:
                raise ProviderError(
                    "The declared effect numerator/denominator do not match the requested positive/reference conditions. "
                    "Use the biological condition labels represented by the column, not only its shorthand header."
                )
        elif direction:
            if direction in {"positive_over_reference", "positive_vs_reference", "as_declared"}:
                transform = "identity"
            elif direction in {"reference_over_positive", "reference_vs_positive", "invert"}:
                transform = "invert"
            else:
                raise ProviderError(
                    "effect_definition.direction must state positive_over_reference or reference_over_positive."
                )

        if scale == "linear_ratio" and not (numerator and denominator):
            raise ProviderError(
                "A linear abundance ratio requires effect_definition.numerator_condition and denominator_condition."
            )
        return {
            "scale": scale,
            "source_formula": clean_text(raw.get("source_formula")) or None,
            "numerator_condition": numerator or None,
            "denominator_condition": denominator or None,
            "transform_to_declared_contrast": transform,
            "semantic_verification": "explicit" if (numerator and denominator) or direction else "not_supplied",
        }

    @staticmethod
    def _apply_effect_definition(value: float, definition: dict[str, Any]) -> float | None:
        scale = str(definition.get("scale") or "")
        if scale == "linear_ratio":
            if value <= 0:
                return None
            value = math.log2(value)
        if definition.get("transform_to_declared_contrast") == "invert":
            value = -value
        return value

    def inspect(
        self,
        *,
        result_path: str,
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
    ) -> dict[str, Any]:
        """Return compact structural facts for one possible differential table."""
        source = load_table(
            path=result_path,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        frame = source.frame
        header = [str(item) for item in frame.columns]
        token_to_columns: dict[str, list[str]] = {}
        for column in header:
            token_to_columns.setdefault(self._token(column), []).append(column)

        def candidates(role: str) -> list[str]:
            return [column for column in header if self._matches_role(column, role)]

        identifier_candidates = candidates("identifier")
        effect_candidates = candidates("effect")
        ratio_candidates = candidates("ratio")
        padj_candidates = candidates("padj")
        p_value_candidates = candidates("p_value")
        stat_candidates = candidates("stat")
        abundance_columns = [
            column for column in header
            if any(token in self._token(column) for token in ("abundance", "intensity", "lfq", "reporter", "area"))
        ]
        nonempty_rows = int(
            frame.apply(lambda row: any(clean_text(value) for value in row.tolist()), axis=1).sum()
        )
        differential_candidate = bool(
            identifier_candidates
            and (effect_candidates or ratio_candidates)
            and (padj_candidates or p_value_candidates)
        )
        role = "differential_result" if differential_candidate else "feature_matrix" if abundance_columns else "table"
        scope_evidence: list[str] = []
        if abundance_columns:
            scope_evidence.append("contains quantitative abundance/intensity-like columns")
        if effect_candidates or ratio_candidates:
            scope_evidence.append("contains an effect or ratio-like column")
        if padj_candidates or p_value_candidates:
            scope_evidence.append("contains a significance-statistic column")
        return {
            "path": source.path,
            "sheet_name": source.sheet_name,
            "header_row": source.header_row,
            "row_count": nonempty_rows,
            "column_count": len(header),
            "columns": header,
            "role": role,
            "differential_result_candidate": differential_candidate,
            "identifier_candidates": identifier_candidates,
            "effect_candidates": effect_candidates,
            "ratio_candidates": ratio_candidates,
            "padj_candidates": padj_candidates,
            "p_value_candidates": p_value_candidates,
            "stat_candidates": stat_candidates,
            "abundance_columns": abundance_columns,
            "result_scope": "undetermined",
            "scope_evidence": scope_evidence,
            "requires_scope_declaration": differential_candidate,
        }

    def analyze(
        self,
        *,
        result_path: str = "",
        table_text: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        identifier_column: str = "",
        effect_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        stat_column: str = "",
        effect_definition: dict[str, Any] | None = None,
        positive_condition: str = "",
        reference_condition: str = "",
        significance_mode: str = "padj",
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        log2_fold_change_threshold: float = 0.0,
    ) -> dict[str, Any]:
        source = load_table(path=result_path, text=table_text, sheet_name=sheet_name, header_row=header_row, skip_rows=skip_rows, dtype=str, keep_default_na=False)
        rows = list(csv.DictReader(source.text.splitlines(), delimiter=source.delimiter))
        if not rows:
            raise ProviderError("Differential-result analysis requires a non-empty table.")
        header = [str(item) for item in rows[0].keys()]
        if significance_mode not in {"padj", "p_value"}:
            raise ProviderError("significance_mode must be 'padj' or 'p_value'.")
        definition = self._normalize_effect_definition(
            effect_definition,
            positive_condition=positive_condition,
            reference_condition=reference_condition,
        )
        column_map = {
            "identifier": self._column(header, identifier_column, "identifier", required=True),
            "effect": self._effect_column(header, effect_column, scale=str(definition["scale"])),
            "padj": self._column(header, padj_column, "padj", required=significance_mode == "padj"),
            "p_value": self._column(header, p_value_column, "p_value", required=significance_mode == "p_value"),
            "stat": self._column(header, stat_column, "stat", required=False),
        }
        parsed: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            identifier = clean_text(row.get(column_map["identifier"]))
            raw_effect = self._number(row.get(column_map["effect"]))
            effect = self._apply_effect_definition(raw_effect, definition) if raw_effect is not None else None
            if not identifier or effect is None:
                continue
            padj = self._number(row.get(column_map["padj"])) if column_map["padj"] else None
            p_value = self._number(row.get(column_map["p_value"])) if column_map["p_value"] else None
            metric = padj if significance_mode == "padj" else p_value
            threshold = padj_threshold if significance_mode == "padj" else p_value_threshold
            significant = bool(metric is not None and metric <= threshold and abs(effect) >= log2_fold_change_threshold)
            parsed.append({
                "row_index": index, "identifier": identifier, "log2_fold_change": effect,
                "padj": padj, "p_value": p_value,
                "stat": self._number(row.get(column_map["stat"])) if column_map["stat"] else None,
                "significant": significant,
            })
        if not parsed:
            raise ProviderError("Differential-result analysis found no usable identifier/effect rows.")
        return {
            "input": {"result_path": source.path, "source_kind": source.source_kind, "sheet_name": source.sheet_name, "header_row": source.header_row, "skip_rows": source.skip_rows},
            "table": {"header": header, "column_map": column_map, "delimiter": source.delimiter, "row_count": len(parsed)},
            "effect_definition": definition,
            "significance": {"mode": significance_mode, "padj_threshold": padj_threshold, "p_value_threshold": p_value_threshold, "log2_fold_change_threshold": log2_fold_change_threshold},
            "rows": parsed,
        }
