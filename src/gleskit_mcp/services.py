from __future__ import annotations

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
from threading import Lock
import tempfile
import time
from typing import Any

import pandas as pd
from scipy import stats

from gleskit_mcp.analysis import (
    DESeq2AnalysisBackend,
    DifferentialResultBackend,
    ExpressionMatrixAnalysisBackend,
    ExpressionStatisticsBackend,
    RNASEQAnalysisBackend,
    SingleCellAnalysisBackend,
    SupervisedExpressionBackend,
    YuLabAnalysisBackend,
)
from gleskit_mcp.analysis.r_runner import RJsonScriptRunner
from gleskit_mcp.cohort_library import inspect_tabular_file, inspect_tabular_frame
from gleskit_mcp.config import Settings, load_settings
from gleskit_mcp.evidence import build_evidence_bundle_payload
from gleskit_mcp.models import Artifact, dump_json, new_id, utc_now_iso
from gleskit_mcp.reference_data import ReferenceCatalog
from gleskit_mcp.providers import (
    ChEMBLProvider,
    EnsemblProvider,
    EnrichrProvider,
    EuropePMCProvider,
    ExpressionAtlasProvider,
    GProfilerProvider,
    GTExProvider,
    GWASCatalogProvider,
    MyGeneProvider,
    OpenTargetsProvider,
    ProviderError,
    QuickGOProvider,
    ReactomeProvider,
    StringDBProvider,
    UniProtProvider,
)
from gleskit_mcp.providers.enrichr import normalize_enrichr_library
from gleskit_mcp.report_templates import get_report_template_payload, list_report_templates_payload
from gleskit_mcp.preview import preview_model_response
from gleskit_mcp.tabular import (
    TemporaryDelimitedTable,
    excel_workbook_engine,
    is_excel_workbook,
    load_table,
    table_rows_as_dicts,
)
from gleskit_mcp.utils import build_literature_query, clean_text, normalize_gene_list, normalize_gene_token, unique_preserve_order
from gleskit_mcp.workspace import WorkspaceGateway, WorkspaceUnavailableError


@dataclass
class _FigureSourceEntry:
    artifact: Artifact
    source_tool: str
    created_at: float
    expires_at: float


class _FigureSourceRegistry:
    def __init__(self, *, ttl_seconds: int = 1800, max_entries: int = 512) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(32, int(max_entries))
        self._entries: dict[str, _FigureSourceEntry] = {}
        self._order: list[str] = []
        self._lock = Lock()

    def _purge_locked(self, *, now: float | None = None) -> None:
        current = now if now is not None else time.time()
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= current]
        for token in expired:
            self._entries.pop(token, None)
            try:
                self._order.remove(token)
            except ValueError:
                continue
        while len(self._order) > self.max_entries:
            oldest = self._order.pop(0)
            self._entries.pop(oldest, None)

    def register(self, *, artifact: Artifact, source_tool: str) -> str:
        now = time.time()
        token = f"figure_source_{secrets.token_urlsafe(12)}"
        with self._lock:
            self._purge_locked(now=now)
            self._entries[token] = _FigureSourceEntry(
                artifact=artifact,
                source_tool=source_tool,
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._order.append(token)
            self._purge_locked(now=now)
        return token

    def get(self, figure_source_id: str) -> _FigureSourceEntry | None:
        cleaned = str(figure_source_id or "").strip()
        if not cleaned:
            return None
        with self._lock:
            self._purge_locked()
            return self._entries.get(cleaned)


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _eval_path_policy_enabled() -> bool:
    return _env_flag("GLESKIT_EVAL_PATH_POLICY")


def _eval_path_entries(name: str) -> tuple[Path, ...]:
    raw = str(os.environ.get(name, "") or "")
    entries: list[Path] = []
    for token in raw.split(os.pathsep):
        cleaned = clean_text(token)
        if not cleaned:
            continue
        entries.append(Path(cleaned).expanduser().resolve())
    return tuple(dict.fromkeys(entries))


class GLESKitService:
    WORKBOOK_INSPECTION_PREVIEW_ROWS = 64

    def __init__(
        self,
        *,
        settings: Settings,
        mygene: MyGeneProvider | None = None,
        ensembl: EnsemblProvider | None = None,
        uniprot: UniProtProvider | None = None,
        expression_atlas: ExpressionAtlasProvider | None = None,
        gtex: GTExProvider | None = None,
        quickgo: QuickGOProvider | None = None,
        reactome: ReactomeProvider | None = None,
        stringdb: StringDBProvider | None = None,
        gwas_catalog: GWASCatalogProvider | None = None,
        chembl: ChEMBLProvider | None = None,
        enrichr: EnrichrProvider | None = None,
        gprofiler: GProfilerProvider | None = None,
        europepmc: EuropePMCProvider | None = None,
        opentargets: OpenTargetsProvider | None = None,
        deseq2_backend: DESeq2AnalysisBackend | None = None,
        differential_result_backend: DifferentialResultBackend | None = None,
        expression_matrix_backend: ExpressionMatrixAnalysisBackend | None = None,
        expression_statistics_backend: ExpressionStatisticsBackend | None = None,
        rnaseq_backend: RNASEQAnalysisBackend | None = None,
        single_cell_backend: SingleCellAnalysisBackend | None = None,
        supervised_expression_backend: SupervisedExpressionBackend | None = None,
        yulab_backend: YuLabAnalysisBackend | None = None,
        reference_catalog: ReferenceCatalog | None = None,
        workspace_gateway: WorkspaceGateway | None = None,
    ) -> None:
        self.settings = settings
        self.mygene = mygene or MyGeneProvider()
        self.ensembl = ensembl or EnsemblProvider()
        self.uniprot = uniprot or UniProtProvider()
        self.expression_atlas = expression_atlas or ExpressionAtlasProvider()
        self.gtex = gtex or GTExProvider()
        self.quickgo = quickgo or QuickGOProvider()
        self.reactome = reactome or ReactomeProvider()
        self.stringdb = stringdb or StringDBProvider()
        self.gwas_catalog = gwas_catalog or GWASCatalogProvider()
        self.chembl = chembl or ChEMBLProvider()
        self.enrichr = enrichr or EnrichrProvider()
        self.gprofiler = gprofiler or GProfilerProvider()
        self.europepmc = europepmc or EuropePMCProvider()
        self.opentargets = opentargets or OpenTargetsProvider()
        self.deseq2_backend = deseq2_backend or DESeq2AnalysisBackend(settings)
        self.differential_result_backend = differential_result_backend or DifferentialResultBackend()
        self.expression_matrix_backend = expression_matrix_backend or ExpressionMatrixAnalysisBackend()
        self.expression_statistics_backend = expression_statistics_backend or ExpressionStatisticsBackend(self.expression_matrix_backend)
        self.rnaseq_backend = rnaseq_backend or RNASEQAnalysisBackend()
        self.single_cell_backend = single_cell_backend or SingleCellAnalysisBackend()
        self.supervised_expression_backend = supervised_expression_backend or SupervisedExpressionBackend(self.expression_matrix_backend)
        self.yulab_backend = yulab_backend or YuLabAnalysisBackend(settings)
        self.reference_catalog = reference_catalog or ReferenceCatalog(settings)
        self.workspace_gateway = workspace_gateway or WorkspaceGateway(settings)
        self.figure_sources = _FigureSourceRegistry()
        self.rjson_runner = RJsonScriptRunner(settings)

    @cached_property
    def figure_catalog(self):
        # MCP metadata/chat must not initialize Matplotlib's platform font
        # discovery. Keep the same catalog and rendering behavior on first use.
        from gleskit_mcp.figures import FigureCatalog

        return FigureCatalog(self.settings)

    @staticmethod
    def _slugify_path_segment(value: str) -> str:
        token = "".join(ch if ch.isalnum() else "-" for ch in clean_text(value).lower()).strip("-")
        while "--" in token:
            token = token.replace("--", "-")
        return token or "deseq2"

    def _deseq2_handoff_root(self, *, label: str, comparison: str, counts_path: str) -> Path:
        base = self.settings.runtime_root / "analysis_handoffs" / "deseq2"
        token = self._slugify_path_segment(label or comparison or Path(counts_path).stem or "deseq2")
        handoff_root = base / f"{token}-{secrets.token_hex(4)}"
        handoff_root.mkdir(parents=True, exist_ok=True)
        return handoff_root

    @staticmethod
    def _write_tsv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
        lines = ["\t".join(header)]
        for row in rows:
            values = [clean_text(value) for value in row]
            lines.append("\t".join(values))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _canonical_sample_token(value: Any) -> str:
        token = clean_text(value).lower()
        return "".join(ch for ch in token if ch.isalnum())

    def _metadata_from_sample_group_map(
        self,
        *,
        sample_columns: list[str],
        sample_group_map: dict[str, str] | None,
        sample_column: str,
        condition_column: str,
    ) -> pd.DataFrame:
        """Materialize exact sample metadata from a caller-provided group map.

        This is intentionally strict: a map must cover every count column and
        cannot silently discard samples. It provides a safe alternative when a
        raw-count matrix is the only supplied input, without granting the
        agent filesystem access or asking it to synthesize delimited text.
        """
        raw_map: Any = sample_group_map or {}
        if isinstance(raw_map, str):
            try:
                raw_map = json.loads(raw_map)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    "sample_group_map must be a JSON object mapping sample names to group labels.",
                    error_code="invalid_input",
                    retryable=False,
                    suggested_next_step="Pass sample_group_map as an object with exact matrix_sample_columns keys.",
                ) from exc
        if not isinstance(raw_map, dict):
            raise ProviderError(
                "sample_group_map must be an object mapping sample names to group labels.",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Pass sample_group_map as an object with exact matrix_sample_columns keys.",
            )

        normalized_map: dict[str, tuple[str, str]] = {}
        for raw_sample, raw_group in raw_map.items():
            sample = clean_text(raw_sample)
            group = clean_text(raw_group)
            token = self._canonical_sample_token(sample)
            if not token or not group:
                raise ProviderError(
                    "sample_group_map requires non-empty sample identifiers and group labels.",
                    error_code="invalid_input",
                    retryable=False,
                    suggested_next_step="Provide one non-empty group label for every inspected count-column name.",
                )
            prior = normalized_map.get(token)
            if prior is not None and prior != (sample, group):
                raise ProviderError(
                    "sample_group_map contains ambiguous sample identifiers after canonical normalization.",
                    error_code="invalid_input",
                    retryable=False,
                    suggested_next_step="Use each inspected sample-column name only once in sample_group_map.",
                )
            normalized_map[token] = (sample, group)

        if not normalized_map:
            raise ProviderError(
                "sample_group_map must contain at least one sample-to-group assignment.",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Inspect the raw-count matrix and provide every sample column in sample_group_map.",
            )

        missing = [column for column in sample_columns if self._canonical_sample_token(column) not in normalized_map]
        if missing:
            raise ProviderError(
                "sample_group_map does not cover every count sample column: " + ", ".join(missing[:12]) + ".",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Copy all matrix_sample_columns returned by inspection into sample_group_map.",
                diagnostic=f"missing_sample_columns={','.join(missing[:24])}",
            )

        count_tokens = {self._canonical_sample_token(column) for column in sample_columns}
        extras = [sample for token, (sample, _group) in normalized_map.items() if token not in count_tokens]
        if extras:
            raise ProviderError(
                "sample_group_map includes names that are not count sample columns: " + ", ".join(extras[:12]) + ".",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Use exact matrix_sample_columns returned by inspection as sample_group_map keys.",
                diagnostic=f"unknown_sample_columns={','.join(extras[:24])}",
            )

        return pd.DataFrame(
            {
                sample_column: sample_columns,
                condition_column: [normalized_map[self._canonical_sample_token(column)][1] for column in sample_columns],
            }
        )

    @staticmethod
    def _eval_execution_timeout_seconds() -> int | None:
        """Return the runner-provided MCP-call budget when evaluation mode sets one."""
        try:
            timeout = int(os.environ.get("GLESKIT_EVAL_TOOL_TIMEOUT_SECONDS", "0") or 0)
        except ValueError:
            timeout = 0
        return timeout if timeout > 0 else None

    def _prepared_rnaseq_root(self, *, label: str, source_path: str) -> Path:
        base = self.settings.runtime_root / "analysis_handoffs" / "prepared_rnaseq"
        token = self._slugify_path_segment(label or Path(source_path).stem or "prepared-rnaseq")
        root = base / f"{token}-{secrets.token_hex(4)}"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _inspection_result_score(payload: dict[str, Any]) -> int:
        data_type = clean_text(payload.get("detected_data_type"))
        sample_column = clean_text(payload.get("candidate_sample_column"))
        group_columns = list(payload.get("candidate_group_columns") or [])
        matrix_preview = [clean_text(item) for item in (payload.get("matrix_sample_preview") or []) if clean_text(item)]
        role_bonus = {
            "raw_counts": 50,
            "continuous_matrix": 60,
            "metadata": 45,
            "deseq2_results": 40,
            "ranked_signature": 35,
            "gene_list": 30,
            "enrichment_result": 25,
            "generic_table": 10,
        }.get(data_type, 0)
        if data_type == "raw_counts" and matrix_preview:
            preview_window = matrix_preview[:5]
            numeric_like = 0
            for token in preview_window:
                try:
                    float(token)
                except ValueError:
                    continue
                numeric_like += 1
            if numeric_like == len(preview_window):
                role_bonus -= 20
        identifier = clean_text(payload.get("row_identifier_column")).lower().replace(" ", "").replace("_", "")
        if data_type in {"raw_counts", "continuous_matrix"}:
            if identifier in {"column1", "unnamed0", "index"}:
                role_bonus -= 15
            elif identifier in {"gene", "geneid", "feature", "featureid", "transcriptid"}:
                role_bonus += 10
            if matrix_preview and all(token.lower().startswith("column_") for token in matrix_preview[:3]):
                role_bonus -= 8
            if data_type == "continuous_matrix" and payload.get("candidate_sample_group_map"):
                role_bonus += 15
        return (
            role_bonus
            + (5 if sample_column else 0)
            + min(5, len(group_columns))
            + GLESKitService._inspection_header_semantic_score(payload)
        )

    @staticmethod
    def _inspection_header_semantic_score(payload: dict[str, Any]) -> int:
        """Prefer schema-like labels over rows that merely resemble metadata.

        Workbook inspection evaluates several possible header rows. A data row
        can look like metadata when it contains categorical values, so semantic
        labels provide a general tie-breaker without assuming a question's
        column names or embedding a dataset-specific header row.
        """
        semantic_labels = {
            "sample",
            "sample_id",
            "sampleid",
            "condition",
            "group",
            "treatment",
            "control",
            "case",
            "sex",
            "gender",
            "age",
            "batch",
            "celltype",
            "cell_type",
            "gene",
            "geneid",
            "gene_id",
            "feature",
            "featureid",
            "feature_id",
            "count",
            "counts",
            "symbol",
            "gene_symbol",
            "biotype",
            "species",
            "strain",
            "sire",
            "hispanic / non hispanic",
        }
        semantic_tokens = {
            "".join(character for character in label if character.isalnum())
            for label in semantic_labels
        }
        score = 0
        sample_column = clean_text(payload.get("candidate_sample_column")).lower()
        row_identifier = clean_text(payload.get("row_identifier_column")).lower()
        labels = [sample_column, row_identifier]
        labels.extend(clean_text(item).lower() for item in payload.get("candidate_group_columns") or [])
        labels.extend(clean_text(item).lower() for item in payload.get("candidate_covariates") or [])
        for label in labels:
            normalized_label = "".join(character for character in label if character.isalnum())
            if normalized_label in semantic_tokens:
                score += 6
        normalized_sample_column = "".join(character for character in sample_column if character.isalnum())
        normalized_row_identifier = "".join(character for character in row_identifier if character.isalnum())
        if normalized_sample_column in {"sample", "sampleid", "barcode", "id"}:
            score += 8
        if normalized_row_identifier in {"gene", "geneid", "feature", "featureid"}:
            score += 8
        if normalized_row_identifier.startswith(("mgd", "gsm", "sample")):
            score -= 8
        return score

    @staticmethod
    def _candidate_analysis_families_from_type(data_type: str, role: str) -> list[str]:
        token = clean_text(data_type or role).lower()
        mapping = {
            "raw_counts": ["prepare_rnaseq_inputs", "analyze_deseq2_counts"],
            "continuous_matrix": ["run_expression_statistics"],
            "metadata": ["prepare_rnaseq_inputs"],
            "deseq2_results": [
                "analyze_rnaseq_result",
                "compare_differential_expression_results",
                "enrich_gene_set",
                "ranked_gene_set_gsea",
            ],
            "ranked_signature": ["ranked_gene_set_gsea"],
            "gene_list": ["enrich_gene_set"],
            "enrichment_result": ["pathway_context"],
            "generic_table": ["run_expression_statistics"],
        }
        return list(mapping.get(token, ["run_expression_statistics"]))

    @staticmethod
    def _looks_like_sheet_role(payload: dict[str, Any], *targets: str) -> bool:
        data_type = clean_text(payload.get("detected_data_type")).lower()
        role = clean_text(payload.get("role")).lower()
        return any(data_type == target or role == target for target in targets)

    def _summarize_inspection_result(
        self,
        inspection: Any,
        *,
        source_path: str,
        header_row: int | None = None,
        sheet_name: str = "",
    ) -> dict[str, Any]:
        structured = dict(getattr(inspection, "inspection", {}) or {})
        data_type = clean_text(structured.get("data_type") or getattr(inspection, "data_type", ""))
        role = clean_text(structured.get("role") or getattr(inspection, "role", ""))
        detected_columns = dict(structured.get("detected_columns") or {})
        candidate_gene_id_columns: list[str] = []
        for key in ("gene_column", "identifier_column"):
            for value in detected_columns.get(key, []) if isinstance(detected_columns.get(key), list) else []:
                cleaned = clean_text(value)
                if cleaned and cleaned not in candidate_gene_id_columns:
                    candidate_gene_id_columns.append(cleaned)
        row_identifier_column = clean_text(structured.get("row_identifier_column"))
        if row_identifier_column and row_identifier_column not in candidate_gene_id_columns:
            candidate_gene_id_columns.append(row_identifier_column)
        next_step_hints = [clean_text(item) for item in (structured.get("next_step_hints") or []) if clean_text(item)]
        warnings = [clean_text(item) for item in getattr(inspection, "warnings", []) if clean_text(item)]
        candidate_group_columns = [clean_text(item) for item in (structured.get("candidate_group_columns") or []) if clean_text(item)]
        candidate_covariates = [clean_text(item) for item in (structured.get("candidate_covariates") or []) if clean_text(item)]
        candidate_group_levels = {
            clean_text(column): [clean_text(value) for value in values if clean_text(value)]
            for column, values in dict(structured.get("candidate_group_levels") or {}).items()
            if clean_text(column)
        }
        candidate_sample_preview = [clean_text(item) for item in (structured.get("candidate_sample_preview") or []) if clean_text(item)]
        matrix_sample_preview = [clean_text(item) for item in (structured.get("matrix_sample_preview") or []) if clean_text(item)]
        matrix_sample_columns = [clean_text(item) for item in (structured.get("matrix_sample_columns") or []) if clean_text(item)]
        candidate_sample_group_map = {
            clean_text(key): clean_text(value)
            for key, value in dict(structured.get("candidate_sample_group_map") or {}).items()
            if clean_text(key) and clean_text(value)
        }
        summary = {
            "detected_data_type": data_type or "generic_table",
            "role": role or "table",
            "row_identifier_column": row_identifier_column,
            "candidate_sample_column": clean_text(structured.get("candidate_sample_column")),
            "candidate_group_columns": candidate_group_columns,
            "candidate_covariates": candidate_covariates,
            "candidate_group_levels": candidate_group_levels,
            "candidate_sample_preview": candidate_sample_preview,
            "matrix_sample_preview": matrix_sample_preview,
            "matrix_sample_columns": matrix_sample_columns,
            "candidate_sample_group_map": candidate_sample_group_map,
            "candidate_gene_id_columns": candidate_gene_id_columns,
            "matrix_orientation": clean_text(structured.get("orientation")),
            "candidate_analysis_families": self._candidate_analysis_families_from_type(data_type, role),
            "recommended_arguments": dict(structured.get("recommended_arguments") or {}),
            "ambiguities": [],
            "next_step_hints": next_step_hints,
            "provenance_notes": [
                note
                for note in (
                    f"source_path={source_path}" if source_path else "",
                    f"sheet_name={sheet_name}" if sheet_name else "",
                    f"header_row={header_row}" if header_row is not None else "",
                )
                if note
            ],
            "warnings": warnings,
            "one_line_summary": clean_text((getattr(inspection, "summary", {}) or {}).get("one_line_summary")),
            "shape": dict(structured.get("shape") or {}),
            "validation": dict(structured.get("validation") or {}),
            "header_row": int(header_row) if header_row is not None else None,
        }
        if len(candidate_group_columns) > 1:
            summary["ambiguities"].append(
                "Multiple plausible group columns were detected: " + ", ".join(candidate_group_columns[:5])
            )
        if not summary["candidate_sample_column"] and self._looks_like_sheet_role(summary, "metadata"):
            summary["ambiguities"].append("No clear sample column was identified in the metadata-like table.")
        return summary

    def _inspect_workbook_sheet(
        self,
        *,
        path: str,
        sheet_name: str,
        role_hint: str,
    ) -> dict[str, Any]:
        best_payload: dict[str, Any] | None = None
        best_score = -1
        max_header_row = 25
        for header_row in range(1, max_header_row + 1):
            try:
                loaded = load_table(
                    path=path,
                    sheet_name=sheet_name,
                    header_row=header_row,
                    max_rows=max(self.WORKBOOK_INSPECTION_PREVIEW_ROWS, header_row + 24),
                    dtype=str,
                    keep_default_na=False,
                )
                inspection = inspect_tabular_frame(
                    loaded.frame,
                    source_path=path,
                    role_hint=role_hint,
                    provenance={"sheet_name": sheet_name, "header_row": header_row},
                )
            except Exception:
                continue
            payload = self._summarize_inspection_result(
                inspection,
                source_path=path,
                header_row=header_row,
                sheet_name=sheet_name,
            )
            score = self._inspection_result_score(payload)
            if score > best_score:
                best_payload = payload
                best_score = score
        if best_payload is None:
            raise ProviderError(
                f"GLESKit could not inspect workbook sheet '{sheet_name}'. Try specifying a different sheet name."
            )
        # Continuous/qPCR workbooks are typically compact enough to inspect in
        # full, and their feature counts and missing-value counts are needed to
        # choose a valid featurewise test. Header selection still uses a small
        # preview so large, unrelated workbook sheets keep their existing cost.
        if clean_text(best_payload.get("detected_data_type")).lower() == "continuous_matrix":
            try:
                full_loaded = load_table(
                    path=path,
                    sheet_name=sheet_name,
                    header_row=int(best_payload.get("header_row") or 1),
                    dtype=str,
                    keep_default_na=False,
                )
                full_inspection = inspect_tabular_frame(
                    full_loaded.frame,
                    source_path=path,
                    role_hint=role_hint,
                    provenance={
                        "sheet_name": sheet_name,
                        "header_row": int(best_payload.get("header_row") or 1),
                    },
                )
                full_payload = self._summarize_inspection_result(
                    full_inspection,
                    source_path=path,
                    header_row=int(best_payload.get("header_row") or 1),
                    sheet_name=sheet_name,
                )
                full_payload["header_selection_score"] = int(best_score)
                full_payload["header_confidence"] = "high" if best_score >= 60 else "medium" if best_score >= 35 else "low"
                full_payload.setdefault("provenance_notes", []).append(
                    f"inspection_full_rows={len(full_loaded.frame.index)}"
                )
                best_payload = full_payload
            except Exception as exc:
                best_payload.setdefault("warnings", []).append(
                    f"Full continuous-matrix inspection was unavailable; preview-derived routing remains in use: {type(exc).__name__}."
                )
        best_payload.setdefault("provenance_notes", []).append(
            f"inspection_preview_rows={self.WORKBOOK_INSPECTION_PREVIEW_ROWS}"
        )
        best_payload["header_row"] = int(best_payload.get("header_row") or 1)
        best_payload["header_selection_score"] = int(best_score)
        best_payload["header_confidence"] = "high" if best_score >= 60 else "medium" if best_score >= 35 else "low"
        if best_payload["header_confidence"] == "low":
            best_payload.setdefault("ambiguities", []).append(
                "The selected header row has low structural confidence; provide header_row and identifier columns explicitly before analysis."
            )
        return best_payload

    @staticmethod
    def _column_token(column: Any) -> str:
        return "".join(character for character in clean_text(column).lower() if character.isalnum())

    def _best_metadata_sample_key_column(
        self,
        *,
        matrix_columns: list[str],
        metadata_frame: pd.DataFrame,
    ) -> tuple[str, int]:
        """Select a metadata key by actual overlap with matrix sample labels."""
        matrix_tokens = {
            self._canonical_sample_token(column)
            for column in matrix_columns
            if self._canonical_sample_token(column)
        }
        if not matrix_tokens:
            return "", 0

        best_column = ""
        best_match_count = 0
        best_semantic_score = -1
        for column in metadata_frame.columns:
            values = {
                self._canonical_sample_token(value)
                for value in metadata_frame[column].tolist()
                if self._canonical_sample_token(value)
            }
            match_count = len(matrix_tokens.intersection(values))
            token = self._column_token(column)
            semantic_score = 0
            if token in {"sample", "sampleid", "barcode", "samplebarcode", "submitterid"}:
                semantic_score = 2
            elif token in {"code", "samplecode", "libraryid"}:
                semantic_score = 1
            if (match_count, semantic_score) > (best_match_count, best_semantic_score):
                best_column = str(column)
                best_match_count = match_count
                best_semantic_score = semantic_score
        return (best_column, best_match_count) if best_match_count >= 2 else ("", 0)

    def _infer_rnaseq_workbook_layout(self, *, path: str) -> dict[str, Any]:
        """Infer a safe counts/metadata handoff for a conventional multi-sheet workbook.

        The inference remains intentionally conservative: it reports candidate
        condition/filter columns rather than choosing a biological contrast.
        """
        if not is_excel_workbook(path):
            return {}
        workbook = pd.ExcelFile(path, engine=excel_workbook_engine(path))
        try:
            sheet_roles = []
            for sheet_name in workbook.sheet_names:
                role = self._inspect_workbook_sheet(path=path, sheet_name=sheet_name, role_hint="")
                role["sheet_name"] = sheet_name
                sheet_roles.append(role)
        finally:
            workbook.close()

        count_candidates = [item for item in sheet_roles if self._looks_like_sheet_role(item, "raw_counts")]
        metadata_candidates = [item for item in sheet_roles if self._looks_like_sheet_role(item, "metadata")]
        if not count_candidates or not metadata_candidates:
            return {}
        counts_role = max(count_candidates, key=self._inspection_result_score)
        metadata_role = max(metadata_candidates, key=self._inspection_result_score)
        try:
            counts_loaded = load_table(
                path=path,
                sheet_name=clean_text(counts_role.get("sheet_name")),
                header_row=int(counts_role.get("header_row") or 1),
                max_rows=max(8, int(counts_role.get("header_row") or 1) + 1),
                dtype=str,
                keep_default_na=False,
            )
            metadata_loaded = load_table(
                path=path,
                sheet_name=clean_text(metadata_role.get("sheet_name")),
                header_row=int(metadata_role.get("header_row") or 1),
                dtype=str,
                keep_default_na=False,
            )
        except Exception:
            return {}

        counts_frame = counts_loaded.frame
        metadata_frame = metadata_loaded.frame
        if counts_frame.empty or metadata_frame.empty:
            return {}
        gene_column = clean_text(counts_role.get("row_identifier_column"))
        if gene_column not in counts_frame.columns:
            gene_column = str(counts_frame.columns[0])
        matrix_columns = [str(column) for column in counts_frame.columns if str(column) != gene_column]
        sample_key_column, sample_key_match_count = self._best_metadata_sample_key_column(
            matrix_columns=matrix_columns,
            metadata_frame=metadata_frame,
        )

        group_columns = [
            column
            for column in list(metadata_role.get("candidate_group_columns") or [])
            if clean_text(column) in metadata_frame.columns
        ]
        group_priority = {
            "condition": 100,
            "tissue": 95,
            "treatment": 90,
            "group": 85,
            "response": 80,
            "status": 75,
            "timepoint": 70,
            "time": 65,
            "strain": 60,
            "sex": 55,
        }
        group_columns = sorted(
            dict.fromkeys(group_columns),
            key=lambda column: (-group_priority.get(self._column_token(column), 0), str(column)),
        )
        semantic_group_columns = [
            column for column in group_columns if group_priority.get(self._column_token(column), 0) > 0
        ]
        if semantic_group_columns:
            group_columns = semantic_group_columns
        group_levels = {
            str(column): list(
                dict.fromkeys(
                    clean_text(value)
                    for value in metadata_frame[column].tolist()
                    if clean_text(value)
                )
            )[:20]
            for column in group_columns
        }

        # Decimal values in a count-like sheet indicate normalized abundance.
        # Rounding is explicit provenance, not an implicit DESeq2 coercion.
        count_transform = "none"
        for column in matrix_columns:
            for value in counts_frame[column].tolist():
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    continue
                if not numeric_value.is_integer():
                    count_transform = "round_int"
                    break
            if count_transform != "none":
                break

        return {
            "counts_sheet_name": clean_text(counts_role.get("sheet_name")),
            "counts_header_row": int(counts_role.get("header_row") or 1),
            "metadata_sheet_name": clean_text(metadata_role.get("sheet_name")),
            "metadata_header_row": int(metadata_role.get("header_row") or 1),
            "gene_column": gene_column,
            "sample_key_column": sample_key_column,
            "sample_key_match_count": sample_key_match_count,
            "count_transform": count_transform,
            "candidate_condition_columns": group_columns,
            "candidate_filter_columns": group_columns,
            "candidate_group_levels": group_levels,
        }

    def _load_prepared_rnaseq_manifest(self, manifest_path: str) -> dict[str, Any]:
        cleaned = clean_text(manifest_path)
        if not cleaned:
            return {}
        path = Path(cleaned).expanduser()
        if not path.exists():
            raise ProviderError(f"Prepared RNA-seq manifest not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Prepared RNA-seq manifest is not valid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"Prepared RNA-seq manifest must be a JSON object: {path}")
        return payload

    @staticmethod
    def _apply_count_transform(frame: pd.DataFrame, *, count_transform: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        numeric = frame.apply(pd.to_numeric, errors="raise")
        normalized = clean_text(count_transform).lower() or "none"
        notes: dict[str, Any] = {"count_transform": normalized}
        if normalized == "none":
            return numeric, notes
        if normalized == "round_int":
            return numeric.round().astype("int64"), notes
        if normalized == "library_scale_round":
            library_sizes = numeric.sum(axis=0)
            positive_sizes = library_sizes[library_sizes > 0]
            if positive_sizes.empty:
                raise ProviderError("library_scale_round requires at least one sample with positive total abundance.")
            median_total = float(positive_sizes.median())
            scaled = numeric.copy()
            scale_factors: dict[str, float] = {}
            for column in scaled.columns:
                total = float(library_sizes[column])
                factor = median_total / total if total > 0 else 1.0
                scale_factors[str(column)] = factor
                scaled[column] = scaled[column].astype(float) * factor
            notes["scale_factors"] = scale_factors
            notes["target_library_size"] = median_total
            return scaled.round().astype("int64"), notes
        raise ProviderError(
            "count_transform must be one of: none, round_int, library_scale_round."
        )

    @staticmethod
    def _top_enrichment_terms(payload: dict[str, Any], *, limit: int = 3) -> list[dict[str, Any]]:
        rows = [dict(row) for row in (payload.get("terms") or []) if isinstance(row, dict)]
        top_rows: list[dict[str, Any]] = []
        for row in rows[: max(1, int(limit))]:
            top_rows.append(
                {
                    "term_name": clean_text(row.get("term_name")) or clean_text(row.get("description")) or None,
                    "source": clean_text(row.get("source")) or None,
                    "adjusted_p_value": row.get("adjusted_p_value"),
                    "intersection_size": row.get("intersection_size"),
                    "matched_genes": list(row.get("matched_genes") or [])[:10],
                }
            )
        return top_rows

    def _attach_enrichment_highlights(self, payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        top_terms = self._top_enrichment_terms(enriched)
        enriched["top_terms"] = top_terms
        enriched["top_term"] = top_terms[0] if top_terms else None
        return enriched

    def _compact_enrichment_payload_for_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Expose only answer-bearing enrichment evidence to constrained agents."""
        requested_focus_terms = [
            clean_text(value)
            for value in (payload.get("focus_terms_requested") or [])
            if clean_text(value)
        ]
        focused_terms = [
            self._standardize_enrichment_term(
                row,
                species=clean_text(payload.get("species")) or "human",
                query_size=self._enrichment_int(payload.get("query_size")),
                background_size=self._enrichment_int(payload.get("background_size")),
                backend=clean_text(payload.get("backend")) or "unknown",
            )
            for row in (payload.get("focused_terms") or [])
            if isinstance(row, dict)
        ][:20]
        focus_terms_found = [
            clean_text(value)
            for value in (payload.get("focus_terms_found") or [])
            if clean_text(value)
        ][:10]
        if not requested_focus_terms:
            focus_status = "not_requested"
            focus_message = "No enrichment focus term was requested."
        elif focused_terms or focus_terms_found:
            focus_status = "matched"
            focus_message = (
                "The requested enrichment focus term was found. Do not make another tool call; "
                "return the final JSON answer from focused_terms now."
            )
        else:
            focus_status = "not_found"
            focus_message = (
                "The requested enrichment focus term was not found in this configured result. "
                "Do not make another tool call; return the final JSON answer reporting that result now."
            )

        normalized = dict(payload.get("normalized_gene_set") or {})
        background_resolution = dict(payload.get("background_resolution") or {})
        return {
            "label": payload.get("label"),
            "species": payload.get("species"),
            "requested_species": payload.get("requested_species"),
            "database": payload.get("database"),
            "library": payload.get("library"),
            "backend": payload.get("backend"),
            "requested_backend": payload.get("requested_backend"),
            "enrichment_backend": payload.get("enrichment_backend"),
            "fallback_used": bool(payload.get("fallback_used", False)),
            "local_backend_warning": payload.get("local_backend_warning"),
            "query_size": payload.get("query_size"),
            "background_size": payload.get("background_size"),
            "term_count": payload.get("term_count"),
            "top_term": dict(payload.get("top_term") or {}),
            "top_terms": self._top_enrichment_terms(payload, limit=3),
            "focus_terms_requested": requested_focus_terms,
            "focus_terms_found": focus_terms_found,
            "focused_terms": focused_terms,
            "focus_status": focus_status,
            "focus_finalization": {
                "recommended_action": "finalize" if requested_focus_terms else "continue_only_if_the_question_needs_another_analysis",
                "message": focus_message,
            },
            "simplify_go_similarity": payload.get("simplify_go_similarity"),
            "identifier_mapping": {
                "mapping_status": normalized.get("mapping_status"),
                "resolved_gene_count": normalized.get("resolved_gene_count"),
                "unresolved_gene_count": len(normalized.get("unresolved_genes") or []),
                "provider_usage": dict(normalized.get("provider_usage") or {}),
                "background_mapping_status": background_resolution.get("mapping_status"),
                "background_resolved_gene_count": background_resolution.get("resolved_gene_count"),
                "background_unresolved_gene_count": len(background_resolution.get("unresolved_genes") or []),
            },
        }

    @staticmethod
    def _enrichment_summary_suffix(payload: dict[str, Any], *, limit: int = 3) -> str:
        top_terms = [item for item in (payload.get("top_terms") or []) if isinstance(item, dict)]
        bits: list[str] = []
        for row in top_terms[: max(1, int(limit))]:
            name = clean_text(row.get("term_name"))
            if not name:
                continue
            adjusted = row.get("adjusted_p_value")
            if adjusted not in (None, ""):
                bits.append(f"{name} (adjusted p={adjusted})")
            else:
                bits.append(name)
        suffix = ""
        if bits:
            label = "Top enriched term" if len(bits) == 1 else "Top enriched terms"
            suffix = f" {label}: " + "; ".join(bits) + "."

        requested_focus_terms = [
            clean_text(value)
            for value in (payload.get("focus_terms_requested") or [])
            if clean_text(value)
        ]
        if not requested_focus_terms:
            return suffix
        focused_terms = [row for row in (payload.get("focused_terms") or []) if isinstance(row, dict)]
        if not focused_terms and not (payload.get("focus_terms_found") or []):
            return (
                suffix
                + " Requested focus term(s) were not found in this configured enrichment result; "
                "treat that as the answer and finalize."
            )
        first_focus = focused_terms[0] if focused_terms else {}
        focus_name = clean_text(first_focus.get("term_name")) or requested_focus_terms[0]
        adjusted_p_value = first_focus.get("adjusted_p_value")
        if adjusted_p_value not in (None, ""):
            return suffix + f" Requested focus term {focus_name} was found (adjusted p={adjusted_p_value}); finalize from this row."
        return suffix + f" Requested focus term {focus_name} was found; finalize from this row."

    @staticmethod
    def _compact_expression_statistics_result(result: dict[str, Any]) -> dict[str, Any]:
        compact = dict(result)
        aligned_rows = compact.pop("aligned_rows", None)
        if isinstance(aligned_rows, list):
            compact["aligned_rows_count"] = len(aligned_rows)
        features = compact.pop("features", None)
        if isinstance(features, list):
            compact["feature_rows_count"] = len(features)
        result_block = dict(compact.get("result") or {})
        input_block = dict(compact.get("input") or {})
        if compact.get("analysis") == "correlation":
            compact["correlation_summary"] = {
                "correlation": result_block.get("correlation"),
                "p_value": result_block.get("p_value"),
                "label_count": result_block.get("label_count"),
                "method": input_block.get("method"),
                "summary_stat": input_block.get("summary_stat"),
                "target_axis": input_block.get("target_axis"),
                "annotation_group_column": input_block.get("annotation_group_column"),
                "annotation_group_value": input_block.get("annotation_group_value"),
                "matched_annotation_rows": input_block.get("matched_annotation_rows"),
            }
        elif compact.get("analysis") == "distribution":
            compact["distribution_summary"] = {
                "value_count": result_block.get("value_count"),
                "mean": result_block.get("mean"),
                "median": result_block.get("median"),
                "standard_deviation": result_block.get("standard_deviation"),
                "skewness": result_block.get("skewness"),
                "skewness_label": result_block.get("skewness_label"),
            }
        elif compact.get("analysis") in {"featurewise_group_test", "matrix_featurewise_group_test"}:
            significant_features = dict(compact.pop("significant_features", {}) or {})
            compact["question_ready_counts"] = {
                "feature_count": result_block.get("feature_count"),
                "tested_feature_count": result_block.get("tested_feature_count"),
                "significant_feature_count_raw": result_block.get("significant_feature_count_raw"),
                "significant_feature_percentage_raw": result_block.get("significant_feature_percentage_raw"),
                "significant_feature_count_bh": result_block.get("significant_feature_count_bh"),
                "significant_feature_count_by": result_block.get("significant_feature_count_by"),
                "significant_feature_count_bonferroni": result_block.get("significant_feature_count_bonferroni"),
                "significant_feature_count_all_corrections": result_block.get("significant_feature_count_all_corrections"),
            }
            compact["significant_features_preview"] = {
                method: list(significant_features.get(method) or [])[:25]
                for method in ("raw", "bh", "by", "bonferroni", "all_corrections")
            }
            compact["significant_features_counts"] = {
                method: len(list(significant_features.get(method) or []))
                for method in ("raw", "bh", "by", "bonferroni", "all_corrections")
            }
        elif compact.get("analysis") == "matrix_group_fold_change_summary":
            compact["fold_change_summary"] = {
                "feature_count": result_block.get("feature_count"),
                "tested_feature_count": result_block.get("tested_feature_count"),
                "summary_stat": result_block.get("summary_stat"),
                "summary_value": result_block.get("summary_value"),
                "mean": result_block.get("mean"),
                "median": result_block.get("median"),
                "q25": result_block.get("q25"),
                "q75": result_block.get("q75"),
                "q10": result_block.get("q10"),
                "q90": result_block.get("q90"),
            }
            if isinstance(features, list):
                compact["feature_preview"] = features[:20]
        elif compact.get("analysis") == "matrix_group_test":
            compact["matrix_group_test_summary"] = {
                "group_count": result_block.get("group_count"),
                "observation_count": result_block.get("observation_count"),
                "feature_count": result_block.get("feature_count"),
                "tested_feature_count": result_block.get("tested_feature_count"),
                "significant_feature_count_bh": result_block.get("significant_feature_count_bh"),
                "statistic_name": result_block.get("statistic_name"),
                "statistic": result_block.get("statistic"),
                "p_value": result_block.get("p_value"),
                "observation_unit": input_block.get("observation_unit"),
                "value_transform": input_block.get("value_transform"),
            }
            if isinstance(features, list):
                compact["feature_preview"] = features[:20]
        elif compact.get("analysis") == "matrix_log2_fold_change_distribution":
            compact["fold_change_distribution_summary"] = {
                "feature_count": result_block.get("feature_count"),
                "pair_count": result_block.get("pair_count"),
                "value_count": result_block.get("value_count"),
                "mean": result_block.get("mean"),
                "median": result_block.get("median"),
                "standard_deviation": result_block.get("standard_deviation"),
                "skewness": result_block.get("skewness"),
                "distribution_label": result_block.get("distribution_label"),
            }
        return compact

    @staticmethod
    def _rds_result_cache_root(settings: Settings) -> Path:
        root = settings.runtime_root / "analysis_handoffs" / "rds_tables"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _materialize_rds_result_table(self, path: str) -> tuple[str, dict[str, Any]]:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"RDS file not found: {resolved}")
        stat = resolved.stat()
        token = hashlib.sha256(
            f"{resolved}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:16]
        cache_root = self._rds_result_cache_root(self.settings) / token
        cache_root.mkdir(parents=True, exist_ok=True)
        output_path = cache_root / f"{resolved.stem}.tsv"
        metadata_path = cache_root / "conversion.json"
        if output_path.exists() and metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(metadata, dict):
                return str(output_path), metadata

        payload = self.rjson_runner.run(
            "gleskit_rds_table.R",
            {
                "rds_path": str(resolved),
                "output_path": str(output_path),
            },
            capability="RDS result-table conversion",
            timeout_seconds=max(120, int(getattr(self.settings, "local_r_timeout_seconds", 45) or 45)),
        )
        materialized_path = output_path
        returned_output_path = clean_text(payload.get("output_path"))
        if returned_output_path:
            candidate = Path(returned_output_path).expanduser()
            if candidate.exists():
                materialized_path = candidate
        if not materialized_path.exists():
            raise ProviderError(
                "GLESKit RDS conversion did not produce a tabular output file. "
                f"Expected {output_path}, runner returned {returned_output_path or 'no output_path'}."
            )
        metadata = dict(payload)
        metadata["requested_output_path"] = str(output_path)
        metadata["output_path"] = str(materialized_path)
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        return str(materialized_path), metadata

    @staticmethod
    def _default_deseq2_handoff_column_map() -> dict[str, str]:
        return {
            "gene": "gene",
            "log2FoldChange": "log2_fold_change",
            "padj": "padj",
            "pvalue": "p_value",
            "stat": "stat",
            "baseMean": "base_mean",
            "dispersion": "dispersion",
        }

    @classmethod
    def _normalize_deseq2_handoff_manifest(
        cls,
        manifest: dict[str, Any],
        *,
        manifest_path: str = "",
    ) -> dict[str, Any]:
        if not isinstance(manifest, dict):
            return {}
        normalized = dict(manifest)
        files = dict(normalized.get("files") or {})
        follow_up_tools = dict(normalized.get("follow_up_tools") or {})
        analyze_follow_up = dict(follow_up_tools.get("analyze_rnaseq_result") or {})
        handoff_root = clean_text(normalized.get("handoff_root"))
        manifest_parent = str(Path(manifest_path).expanduser().resolve().parent) if clean_text(manifest_path) else ""
        inferred_handoff_root = handoff_root or manifest_parent

        candidate_result_paths: list[str] = []
        for candidate in (
            files.get("result_table_path"),
            analyze_follow_up.get("result_path"),
            str(Path(inferred_handoff_root) / "deseq2_results.tsv") if inferred_handoff_root else "",
        ):
            cleaned = clean_text(candidate)
            if cleaned:
                candidate_result_paths.append(cleaned)
        resolved_result_table_path = ""
        for candidate in candidate_result_paths:
            candidate_path = Path(candidate).expanduser()
            if candidate_path.exists():
                resolved_result_table_path = str(candidate_path.resolve())
                break
        if not resolved_result_table_path and candidate_result_paths:
            resolved_result_table_path = candidate_result_paths[0]
        if resolved_result_table_path:
            files["result_table_path"] = resolved_result_table_path

        merged_column_map = {
            str(key): str(value)
            for key, value in dict(normalized.get("column_map") or {}).items()
            if clean_text(key) and clean_text(value)
        }
        normalized["files"] = files
        normalized["column_map"] = merged_column_map
        return normalized

    def _normalize_rnaseq_result_path(self, path: str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        cleaned = clean_text(path)
        if not cleaned:
            return "", None, {}
        resolved = self._resolve_input_file_selector(cleaned, role="result_path")
        handoff_manifest: dict[str, Any] = {}
        if Path(resolved).suffix.lower() == ".json":
            try:
                manifest = self._normalize_deseq2_handoff_manifest(
                    self._read_deseq2_handoff_manifest(resolved),
                    manifest_path=resolved,
                )
            except ProviderError:
                manifest = {}
            if manifest:
                handoff_manifest = dict(manifest)
            manifest_result_path = clean_text(((manifest.get("files") or {}) if isinstance(manifest, dict) else {}).get("result_table_path"))
            if manifest_result_path:
                resolved = self._resolve_input_file_selector(manifest_result_path, role="result_path")
            elif handoff_manifest:
                raise ProviderError(
                    "DESeq2 handoff manifest did not expose a reusable result table. "
                    "Expected files.result_table_path or a sibling deseq2_results.tsv."
                )
        if Path(resolved).suffix.lower() != ".rds":
            return resolved, None, handoff_manifest
        materialized_path, metadata = self._materialize_rds_result_table(resolved)
        return materialized_path, metadata, handoff_manifest

    def _build_focus_gene_aliases(
        self,
        *,
        focus_genes: list[str],
        species: str,
        gene_alias_table_path: str = "",
        gene_alias_table_text: str = "",
        gene_alias_query_column: str = "",
        gene_alias_target_column: str = "",
    ) -> dict[str, list[str]]:
        alias_map: dict[str, list[str]] = {}
        queries = [clean_text(gene) for gene in (focus_genes or []) if clean_text(gene)]
        if not queries:
            return alias_map

        def _append(query: str, value: Any) -> None:
            token = clean_text(value)
            if not token:
                return
            alias_map.setdefault(query, [])
            if token not in alias_map[query]:
                alias_map[query].append(token)

        if clean_text(gene_alias_table_path) or clean_text(gene_alias_table_text):
            alias_table = load_table(
                path=clean_text(gene_alias_table_path),
                text=clean_text(gene_alias_table_text),
                dtype=str,
                keep_default_na=False,
            )
            frame = alias_table.frame
            if not frame.empty:
                columns = [str(column) for column in frame.columns]
                normalized = {"".join(ch for ch in column.lower() if ch.isalnum()): column for column in columns}
                query_column = clean_text(gene_alias_query_column)
                target_column = clean_text(gene_alias_target_column)
                resolved_query_column = normalized.get("".join(ch for ch in query_column.lower() if ch.isalnum()), query_column) if query_column else next(
                    (normalized[key] for key in ("gene", "symbol", "query", "genesymbol", "externalgenename") if key in normalized),
                    columns[0],
                )
                resolved_target_column = normalized.get("".join(ch for ch in target_column.lower() if ch.isalnum()), target_column) if target_column else next(
                    (
                        normalized[key]
                        for key in ("geneid", "gene_id", "ensemblgeneid", "target", "alias", "mappedgene")
                        if key in normalized and normalized[key] != resolved_query_column
                    ),
                    columns[1] if len(columns) > 1 else columns[0],
                )
                for row in table_rows_as_dicts(frame):
                    left = clean_text(row.get(resolved_query_column))
                    right = clean_text(row.get(resolved_target_column))
                    if not left or not right:
                        continue
                    for query in queries:
                        if normalize_gene_token(left, species=species) == normalize_gene_token(query, species=species):
                            _append(query, right)

        for query in queries:
            try:
                resolution = self._resolve_gene_provider_result(query, species=species)
            except ProviderError:
                resolution = None
            matches = (resolution.payload.get("matches") or []) if resolution is not None else []
            best = dict(matches[0]) if matches else {}
            for candidate in (
                query,
                best.get("symbol"),
                best.get("ensembl_gene_id"),
                best.get("matched_value"),
                *(best.get("aliases") or []),
            ):
                _append(query, candidate)
        return alias_map

    @staticmethod
    def _read_deseq2_handoff_manifest(manifest_path: str) -> dict[str, Any]:
        cleaned = clean_text(manifest_path)
        if not cleaned:
            return {}
        path = Path(cleaned).expanduser()
        if not path.exists():
            raise ProviderError(f"DESeq2 handoff manifest was not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"DESeq2 handoff manifest is not valid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"DESeq2 handoff manifest must be a JSON object: {path}")
        return payload

    @staticmethod
    def _load_gene_list_from_table_path(path: str, *, species: str | None = None) -> list[str]:
        loaded = load_table(path=path, dtype=str, keep_default_na=False)
        frame = loaded.frame
        if frame.empty:
            return []
        columns = [str(column) for column in frame.columns]
        preferred = {"gene", "genes", "symbol", "geneid", "genesymbol", "id", "genename", "hgncsymbol"}
        column = next((col for col in columns if "".join(ch for ch in col.lower() if ch.isalnum()) in preferred), columns[0])
        return normalize_gene_list(frame[column].tolist(), species=species)

    @staticmethod
    def _load_gene_list_from_text(text: str, *, species: str | None = None) -> list[str]:
        lines = [clean_text(line) for line in str(text or "").replace("\r", "\n").splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            return []
        header_token = "".join(ch for ch in lines[0].lower() if ch.isalnum())
        if header_token in {"gene", "genes", "symbol", "geneid", "genesymbol", "id", "genename", "hgncsymbol"} and len(lines) > 1:
            lines = lines[1:]
        return normalize_gene_list(lines, species=species)

    @staticmethod
    def _load_gene_score_rows_from_table_path(path: str, *, species: str | None = None) -> list[dict[str, Any]]:
        loaded = load_table(path=path, dtype=str, keep_default_na=False)
        frame = loaded.frame
        if frame.empty:
            return []
        columns = [str(column) for column in frame.columns]
        normalized = {"".join(ch for ch in column.lower() if ch.isalnum()): column for column in columns}
        gene_column = next((normalized[key] for key in ("gene", "genesymbol", "geneid", "symbol", "id", "genename", "hgncsymbol") if key in normalized), columns[0])
        score_column = next((normalized[key] for key in ("score", "stat", "statistic", "log2foldchange", "log2fc", "logfc") if key in normalized), None)
        rows: list[dict[str, Any]] = []
        for item in table_rows_as_dicts(frame):
            gene = normalize_gene_token(item.get(gene_column), species=species)
            if not gene:
                continue
            score_value: Any = item.get(score_column) if score_column is not None else item.get(columns[1] if len(columns) > 1 else columns[0])
            try:
                score = float(score_value)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "gene": gene,
                    "score": score,
                    "score_source": "file",
                }
            )
        rows.sort(key=lambda row: abs(float(row.get("score") or 0.0)), reverse=True)
        return rows

    def _write_deseq2_handoff_files(
        self,
        *,
        label: str,
        comparison: str,
        counts_path: str,
        species: str,
        structured_data: dict[str, Any],
    ) -> dict[str, Any]:
        handoff_root = self._deseq2_handoff_root(label=label, comparison=comparison, counts_path=counts_path)
        significant_gene_sets = dict(structured_data.get("significant_gene_sets") or {})
        all_significant = normalize_gene_list(significant_gene_sets.get("all_significant") or [], species=species)
        upregulated = normalize_gene_list(significant_gene_sets.get("upregulated") or [], species=species)
        downregulated = normalize_gene_list(significant_gene_sets.get("downregulated") or [], species=species)
        ranked_gene_scores = list(structured_data.get("ranked_gene_scores") or [])

        all_significant_path = handoff_root / "all_significant_genes.tsv"
        upregulated_path = handoff_root / "upregulated_genes.tsv"
        downregulated_path = handoff_root / "downregulated_genes.tsv"
        ranked_scores_path = handoff_root / "ranked_gene_scores.tsv"
        manifest_path = handoff_root / "handoff_manifest.json"

        self._write_tsv(all_significant_path, ["gene"], [[gene] for gene in all_significant])
        self._write_tsv(upregulated_path, ["gene"], [[gene] for gene in upregulated])
        self._write_tsv(downregulated_path, ["gene"], [[gene] for gene in downregulated])
        ranked_rows = [
            [
                row.get("gene"),
                row.get("score"),
                row.get("score_source"),
                row.get("log2_fold_change"),
                row.get("padj"),
                row.get("p_value"),
            ]
            for row in ranked_gene_scores
            if isinstance(row, dict)
        ]
        self._write_tsv(
            ranked_scores_path,
            ["gene", "score", "score_source", "log2_fold_change", "padj", "p_value"],
            ranked_rows,
        )
        result_table_path = handoff_root / "deseq2_results.tsv"
        tested_genes_path = handoff_root / "tested_genes.tsv"
        result_rows = [
            [
                row.get("gene"),
                row.get("base_mean"),
                row.get("log2_fold_change"),
                row.get("lfc_se"),
                row.get("stat"),
                row.get("p_value"),
                row.get("padj"),
                row.get("dispersion"),
            ]
            for row in (structured_data.get("results") or [])
            if isinstance(row, dict)
        ]
        self._write_tsv(
            result_table_path,
            ["gene", "base_mean", "log2_fold_change", "lfc_se", "stat", "p_value", "padj", "dispersion"],
            result_rows,
        )
        tested_genes = unique_preserve_order(
            normalize_gene_token(row.get("gene"), species=species)
            for row in (structured_data.get("results") or [])
            if isinstance(row, dict) and clean_text(row.get("gene"))
        )
        self._write_tsv(tested_genes_path, ["gene"], [[gene] for gene in tested_genes])

        manifest = {
            "manifest_version": 2,
            "label": clean_text(label) or None,
            "comparison": clean_text(comparison) or None,
            "species": clean_text(species) or None,
            "generated_at": utc_now_iso(),
            "handoff_root": str(handoff_root),
            "files": {
                "result_table_path": str(result_table_path),
                "tested_genes_path": str(tested_genes_path),
                "all_significant_genes_path": str(all_significant_path),
                "upregulated_genes_path": str(upregulated_path),
                "downregulated_genes_path": str(downregulated_path),
                "ranked_gene_scores_path": str(ranked_scores_path),
            },
            "column_map": self._default_deseq2_handoff_column_map(),
            "counts": {
                "result_row_count": len(result_rows),
                "all_significant_gene_count": len(all_significant),
                "upregulated_gene_count": len(upregulated),
                "downregulated_gene_count": len(downregulated),
                "ranked_gene_score_count": len(ranked_gene_scores),
            },
            "follow_up_tools": {
                "analyze_rnaseq_result": {
                    "result_path": str(result_table_path),
                    "handoff_manifest_path": str(manifest_path),
                },
                "compare_differential_expression_results": {
                    "all_significant_genes_path": str(all_significant_path),
                    "upregulated_genes_path": str(upregulated_path),
                    "downregulated_genes_path": str(downregulated_path),
                    "handoff_manifest_path": str(manifest_path),
                },
                "ranked_gene_set_gsea": {
                    "ranked_gene_scores_path": str(ranked_scores_path),
                },
                "enrich_gene_set": {
                    "all_significant_genes_path": str(all_significant_path),
                    "upregulated_genes_path": str(upregulated_path),
                    "downregulated_genes_path": str(downregulated_path),
                    "background_genes_path": str(tested_genes_path),
                },
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        return {
            "handoff_root": str(handoff_root),
            "manifest_path": str(manifest_path),
            "result_table_path": str(result_table_path),
            "tested_genes_path": str(tested_genes_path),
            "column_map": dict(manifest.get("column_map") or {}),
            "all_significant_genes_path": str(all_significant_path),
            "upregulated_genes_path": str(upregulated_path),
            "downregulated_genes_path": str(downregulated_path),
            "ranked_gene_scores_path": str(ranked_scores_path),
            "result_row_count": len(result_rows),
            "tested_gene_count": len(tested_genes),
            "all_significant_gene_count": len(all_significant),
            "upregulated_gene_count": len(upregulated),
            "downregulated_gene_count": len(downregulated),
            "ranked_gene_score_count": len(ranked_gene_scores),
        }

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "GLESKitService":
        effective_settings = settings or load_settings()
        return cls(settings=effective_settings)

    def workspace_status(self) -> dict[str, Any]:
        return self.workspace_gateway.status()

    def reference_status(self) -> dict[str, Any]:
        return self.reference_catalog.status()

    def update_references(self) -> dict[str, Any]:
        payload = self.reference_catalog.refresh_all()
        dataset = next(iter(payload.get("datasets") or []), {})
        record_count = int(dataset.get("record_count") or 0)
        alias_count = int(dataset.get("alias_count") or 0)
        return {
            **payload,
            "summary": (
                f"Updated {len(payload.get('datasets') or [])} local reference dataset(s). "
                f"HGNC snapshot now includes {record_count} gene record(s) and {alias_count} alias/xref token(s)."
            ),
        }

    def create_session(
        self,
        *,
        title: str,
        description: str,
        species: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.create_session(
            title=title,
            description=description,
            species=clean_text(species or self.settings.default_species) or self.settings.default_species,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        payload["is_active"] = runtime.active_session_id == payload["session_id"]
        return payload

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        cleaned_title = clean_text(title) if title is not None else None
        cleaned_description = clean_text(description) if description is not None else None
        if cleaned_title == "":
            raise ValueError("title must not be empty.")
        if cleaned_description == "":
            raise ValueError("description must not be empty.")
        if cleaned_title is None and cleaned_description is None:
            raise ValueError("Provide a title, a description, or both.")
        payload = workspace.update_session(
            session_id,
            title=cleaned_title,
            description=cleaned_description,
        )
        payload["workspace"] = self.workspace_gateway.status()
        payload["is_active"] = runtime.active_session_id == payload["session_id"]
        return payload

    def add_session_note(
        self,
        session_id: str,
        *,
        title: str,
        body_markdown: str,
        author: str = "assistant",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.add_note(
            session_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_session_notes(self, session_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_notes(session_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def get_session_note(self, session_id: str, note_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.get_note(session_id, note_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def update_session_note(
        self,
        session_id: str,
        note_id: str,
        *,
        title: str,
        body_markdown: str,
        author: str = "assistant",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.update_note(
            session_id,
            note_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def delete_session_note(self, session_id: str, note_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_note(session_id, note_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def delete_session_notes(self, session_id: str, note_ids: list[str]) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_notes(session_id, note_ids)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_session_reports(self, session_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_reports(session_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_cohorts(self) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_cohorts()
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_cohort_providers(self) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_cohort_providers()
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_cohort_catalog(self, provider_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_cohort_catalog(provider_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def get_cohort(self, cohort_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.get_cohort(cohort_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def create_cohort(
        self,
        *,
        title: str,
        counts_path: str,
        metadata_path: str,
        provider_id: str = "local_import",
        source_kind: str = "imported",
        species: str = "human",
        transpose: bool = False,
        sample_column: str = "",
        group_column: str = "",
        min_count: int = 10,
        sample_limit: int = 0,
        source_counts_path: str = "",
        source_metadata_path: str = "",
        source_counts_url: str = "",
        source_metadata_url: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.create_cohort(
            title=title,
            counts_path=counts_path,
            metadata_path=metadata_path,
            provider_id=provider_id,
            source_kind=source_kind,
            species=species,
            transpose=transpose,
            sample_column=sample_column,
            group_column=group_column,
            min_count=min_count,
            sample_limit=sample_limit,
            source_counts_path=source_counts_path,
            source_metadata_path=source_metadata_path,
            source_counts_url=source_counts_url,
            source_metadata_url=source_metadata_url,
            download_provider_id=provider_id if source_kind == "downloaded" else "",
            download_dataset_id="",
            download_tissue="",
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def download_cohort(
        self,
        *,
        title: str,
        provider_id: str,
        dataset_id: str,
        tissue: str = "",
        species: str = "human",
        sample_limit: int = 0,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.download_cohort(
            title=title,
            provider_id=provider_id,
            dataset_id=dataset_id,
            tissue=tissue,
            species=species,
            sample_limit=sample_limit,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def update_cohort(self, cohort_id: str, *, title: str | None = None) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.update_cohort(cohort_id, title=title)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def delete_cohort(self, cohort_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_cohort(cohort_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def create_session_from_cohort(
        self,
        *,
        cohort_id: str,
        title: str,
        description: str,
        species: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.create_session_from_cohort(
            cohort_id=cohort_id,
            title=title,
            description=description,
            species=clean_text(species or self.settings.default_species) or self.settings.default_species,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        payload["is_active"] = runtime.active_session_id == payload["session_id"]
        return payload

    def create_figure(
        self,
        session_id: str,
        artifact_id: str,
        *,
        figure_type: str,
        title: str = "",
        top_n: int = 10,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.create_figure(
            session_id,
            artifact_id,
            figure_type=figure_type,
            title=title,
            top_n=top_n,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def register_figure_source(self, tool_name: str, result: dict[str, Any]) -> str | None:
        if not isinstance(result, dict):
            return None
        artifact = self._synthetic_artifact_for_tool_result(tool_name, result)
        if artifact is None:
            return None
        options = self.figure_catalog.figure_options(artifact)
        if not options:
            return None
        return self.figure_sources.register(artifact=artifact, source_tool=tool_name)

    def attach_figure_source_id(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        source_id = self.register_figure_source(tool_name, result)
        if not source_id:
            return result
        enriched = dict(result)
        enriched["figure_source_id"] = source_id
        structured = enriched.get("structured_data")
        if isinstance(structured, dict):
            structured_enriched = dict(structured)
            structured_enriched["figure_source_id"] = source_id
            enriched["structured_data"] = structured_enriched
        return enriched

    def list_figure_options(
        self,
        *,
        figure_source_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
    ) -> dict[str, Any]:
        source_artifact, source_info = self._resolve_figure_source_artifact(
            figure_source_id=figure_source_id,
            session_id=session_id,
            artifact_id=artifact_id,
        )
        options = self.figure_catalog.figure_options(source_artifact)
        source_mode = clean_text(source_info.get("mode")) or "unknown"
        next_step_hint = (
            "Call generate_figure with one options[].figure_type and the same figure_source_id."
            if source_mode == "stateless"
            else "Call generate_figure with one options[].figure_type and the same artifact selector (artifact_id with optional session_id)."
        )
        return {
            "summary": f"Found {len(options)} figure option(s) for this source.",
            "source": source_info,
            "option_count": len(options),
            "options": options,
            "next_step_hint": next_step_hint,
            "workspace": self.workspace_gateway.status(),
        }

    def generate_figure(
        self,
        *,
        figure_type: str,
        figure_source_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
        title: str = "",
        top_n: int = 10,
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        source_artifact, source_info = self._resolve_figure_source_artifact(
            figure_source_id=figure_source_id,
            session_id=session_id,
            artifact_id=artifact_id,
        )

        with tempfile.TemporaryDirectory(prefix="gleskit_generate_figure_") as tmp:
            figure_payload = self.figure_catalog.create_figure(
                source_artifact,
                output_dir=Path(tmp),
                figure_type=figure_type,
                title=title,
                top_n=top_n,
            )

            png_path = clean_text(figure_payload.get("png_path"))
            inline_image_bytes = None
            if png_path and Path(png_path).exists():
                inline_image_bytes = Path(png_path).read_bytes()

            inline_file_bytes: dict[str, bytes] = {}
            for raw_path in (figure_payload.get("figure_files") or []):
                token = str(raw_path or "").strip()
                if not token:
                    continue
                file_path = Path(token)
                if not file_path.exists():
                    continue
                try:
                    inline_file_bytes[token] = file_path.read_bytes()
                except OSError:
                    continue
            if png_path and inline_image_bytes and png_path not in inline_file_bytes:
                inline_file_bytes[png_path] = inline_image_bytes

            warnings: list[str] = []
            saved_artifact_id = ""
            saved_session_id = ""
            if clean_text(save_to_session_id):
                try:
                    _, workspace = self.workspace_gateway.require_workspace()
                    created = workspace.capture_artifact(
                        clean_text(save_to_session_id),
                        kind="figure",
                        title=figure_payload["title"],
                        summary=figure_payload["caption"],
                        provider="gleskit_figures",
                        payload=figure_payload,
                        provenance={
                            "source_artifact_id": source_artifact.artifact_id,
                            "source_artifact_kind": source_artifact.kind,
                            "figure_type": figure_payload["figure_type"],
                            "source_mode": source_info["mode"],
                            "figure_source_id": source_info.get("figure_source_id") or "",
                        },
                    )
                    saved_artifact_id = created.artifact_id
                    saved_session_id = created.session_id
                except WorkspaceUnavailableError as exc:
                    warnings.append(
                        "Workspace is unavailable, so the figure was generated but not saved. "
                        f"Reason: {exc}"
                    )
                except Exception as exc:
                    warnings.append(
                        "The figure was generated but could not be saved to the requested session. "
                        f"Reason: {exc}"
                    )

        return {
            "artifact_id": saved_artifact_id,
            "session_id": saved_session_id,
            "kind": "figure",
            "title": figure_payload["title"],
            "summary": figure_payload["caption"],
            "provider": "gleskit_figures",
            "provenance": {
                "source_artifact_id": source_artifact.artifact_id,
                "source_artifact_kind": source_artifact.kind,
                "figure_type": figure_payload["figure_type"],
                "source_mode": source_info["mode"],
                "figure_source_id": source_info.get("figure_source_id") or "",
            },
            "payload": figure_payload,
            "_inline_image_bytes": inline_image_bytes,
            "_inline_file_bytes": inline_file_bytes,
            "saved": bool(saved_artifact_id and saved_session_id),
            "source": source_info,
            "warnings": warnings,
            "workspace": self.workspace_gateway.status(),
        }

    def auto_figures_for_result(
        self,
        tool_name: str,
        result: dict[str, Any],
        *,
        top_n: int = 15,
    ) -> list[dict[str, Any]]:
        if not isinstance(result, dict):
            return []
        artifact = self._synthetic_artifact_for_tool_result(tool_name, result)
        if artifact is None:
            return []

        options = self.figure_catalog.figure_options(artifact)
        if not options:
            return []

        preferred = self._preferred_auto_figure_types(tool_name)
        selected_options: list[dict[str, Any]] = []
        for figure_type in preferred:
            match = next((row for row in options if row.get("figure_type") == figure_type), None)
            if match:
                selected_options.append(match)
        if not selected_options and options:
            selected_options = [options[0]]

        workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
        session_id = clean_text((workspace or {}).get("active_session_id"))
        artifact_id = clean_text((workspace or {}).get("artifact_id"))
        saved = bool((workspace or {}).get("saved")) and bool(session_id) and bool(artifact_id)

        figures: list[dict[str, Any]] = []
        if saved:
            for option in selected_options:
                figure_type = clean_text(option.get("figure_type"))
                if not figure_type:
                    continue
                try:
                    created = self.create_figure(
                        session_id,
                        artifact_id,
                        figure_type=figure_type,
                        top_n=top_n,
                    )
                    png_path = clean_text(((created.get("payload") or {}).get("png_path")))
                    inline_image_bytes = None
                    if png_path and Path(png_path).exists():
                        inline_image_bytes = Path(png_path).read_bytes()
                    figures.append(
                        {
                            **created,
                            "_inline_image_bytes": inline_image_bytes,
                            "_auto_generated": True,
                        }
                    )
                except Exception:
                    continue
            return figures

        def _build_stateless_figure(index: int, figure_type: str) -> tuple[int, dict[str, Any] | None]:
            try:
                with tempfile.TemporaryDirectory(prefix="gleskit_autofig_") as tmp:
                    figure_payload = self.figure_catalog.create_figure(
                        artifact,
                        output_dir=Path(tmp),
                        figure_type=figure_type,
                        top_n=top_n,
                    )
                    png_path = clean_text(figure_payload.get("png_path"))
                    inline_image_bytes = None
                    inline_file_bytes: dict[str, bytes] = {}
                    for raw_path in (figure_payload.get("figure_files") or []):
                        token = str(raw_path or "").strip()
                        if not token:
                            continue
                        file_path = Path(token)
                        if not file_path.exists():
                            continue
                        try:
                            inline_file_bytes[token] = file_path.read_bytes()
                        except OSError:
                            continue
                    if png_path and Path(png_path).exists():
                        inline_image_bytes = Path(png_path).read_bytes()
                        if png_path not in inline_file_bytes:
                            inline_file_bytes[png_path] = inline_image_bytes
                return index, {
                    "artifact_id": "",
                    "session_id": "",
                    "kind": "figure",
                    "title": figure_payload["title"],
                    "summary": figure_payload["caption"],
                    "provider": "gleskit_figures",
                    "provenance": {
                        "source_artifact_kind": artifact.kind,
                        "figure_type": figure_type,
                        "auto_generated": True,
                    },
                    "payload": figure_payload,
                    "_inline_image_bytes": inline_image_bytes,
                    "_inline_file_bytes": inline_file_bytes,
                    "_auto_generated": True,
                }
            except Exception:
                return index, None

        stateless_requests = [
            (index, clean_text(option.get("figure_type")))
            for index, option in enumerate(selected_options)
            if clean_text(option.get("figure_type"))
        ]
        max_workers = max(1, min(len(stateless_requests), int(getattr(self.settings, "local_max_workers", 1) or 1)))
        if max_workers <= 1 or len(stateless_requests) <= 1:
            for index, figure_type in stateless_requests:
                _, figure = _build_stateless_figure(index, figure_type)
                if figure is not None:
                    figures.append(figure)
            return figures

        ordered_figures: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(_build_stateless_figure, index, figure_type): index
                for index, figure_type in stateless_requests
            }
            for future in as_completed(future_map):
                index, figure = future.result()
                if figure is not None:
                    ordered_figures[index] = figure
        for index, _figure_type in stateless_requests:
            figure = ordered_figures.get(index)
            if figure is not None:
                figures.append(figure)
        return figures

    def get_session_report(self, session_id: str, report_id: str | None = None) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.get_session_report(session_id, report_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def create_session_report(
        self,
        session_id: str,
        *,
        body_markdown: str,
        template_id: str,
        title: str = "",
        author: str = "assistant",
        focus: str = "",
        custom_guidance: str = "",
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.create_report(
            session_id,
            body_markdown=body_markdown,
            template_id=template_id,
            title=title,
            author=author,
            focus=focus,
            custom_guidance=custom_guidance,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def update_session_report(
        self,
        session_id: str,
        report_id: str,
        *,
        title: str | None = None,
        body_markdown: str,
        author: str | None = None,
        focus: str | None = None,
        template_id: str | None = None,
        custom_guidance: str | None = None,
        linked_artifact_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.update_report(
            session_id,
            report_id,
            title=title,
            body_markdown=body_markdown,
            author=author,
            focus=focus,
            template_id=template_id,
            custom_guidance=custom_guidance,
            linked_artifact_ids=linked_artifact_ids,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def delete_session_report(self, session_id: str, report_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_report(session_id, report_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def delete_session_reports(self, session_id: str, report_ids: list[str]) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_reports(session_id, report_ids)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_report_templates(self) -> dict[str, Any]:
        payload = list_report_templates_payload()
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def get_report_template(self, template_id: str) -> dict[str, Any]:
        payload = get_report_template_payload(template_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_sessions(self, *, query: str = "", status: str = "") -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_sessions(query=query, status=status or None, active_session_id=runtime.active_session_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def get_session_summary(self, session_id: str) -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.get_session_summary(session_id, active_session_id=runtime.active_session_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_artifacts(self, session_id: str, *, kind: str | None = None) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_artifacts(session_id, kind=kind)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def list_uploads(self, session_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.list_uploads(session_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def save_analysis_manifest_to_session(
        self,
        *,
        session_id: str,
        manifest_path: str,
        kind: str = "analysis",
        title: str = "",
        summary: str = "",
        provider: str = "gleskit_local",
    ) -> dict[str, Any]:
        """Persist one generated JSON manifest without changing the active session.

        The method deliberately only accepts generated handoffs beneath the
        runtime root.  This keeps a path-oriented analysis workflow auditable
        without turning arbitrary user files into workspace artifacts.
        """
        cleaned_session = clean_text(session_id)
        if not cleaned_session:
            raise ProviderError("save_analysis_manifest_to_session requires session_id.")
        candidate = Path(clean_text(manifest_path)).expanduser().resolve()
        handoff_root = (self.settings.runtime_root / "analysis_handoffs").resolve()
        if candidate != handoff_root and handoff_root not in candidate.parents:
            raise ProviderError("Only generated analysis handoff manifests can be saved to a session.")
        if not candidate.exists():
            raise ProviderError(f"Analysis manifest was not found: {candidate}")
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Analysis manifest is not valid JSON: {candidate}") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Analysis manifest must contain a JSON object.")
        _, workspace = self.workspace_gateway.require_workspace()
        created = workspace.capture_artifact(
            cleaned_session,
            kind=clean_text(kind) or "analysis",
            title=clean_text(title) or clean_text(payload.get("label")) or candidate.stem,
            summary=clean_text(summary) or clean_text(payload.get("summary")) or "Generated GLESKit analysis handoff.",
            payload=payload,
            provider=clean_text(provider) or clean_text(payload.get("provider")) or "gleskit_local",
            provenance={
                "manifest_path": str(candidate),
                "analysis": clean_text(payload.get("analysis")) or None,
                "explicit_session_persistence": True,
            },
        )
        return {
            "summary": f"Saved analysis manifest to session {cleaned_session}.",
            "artifact_id": created.artifact_id,
            "session_id": cleaned_session,
            "manifest_path": str(candidate),
            "workspace": self.workspace_gateway.status(),
        }

    def analysis_runtime_status(self) -> dict[str, Any]:
        """Return non-destructive R-worker health information for MCP recovery checks."""
        payload = RJsonScriptRunner.runtime_status()
        payload["workspace"] = self.workspace_gateway.status()
        payload["summary"] = (
            "No active R analysis jobs are registered."
            if not payload.get("active_job_count")
            else f"{payload['active_job_count']} R analysis job(s) are still active."
        )
        return payload

    def cancel_analysis_job(self, job_id: str) -> dict[str, Any]:
        payload = RJsonScriptRunner.cancel_job(job_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def get_artifact(self, session_id: str | None, artifact_id: str) -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.get_artifact(
            session_id,
            artifact_id,
            active_session_id=runtime.active_session_id,
        )
        payload["workspace"] = self.workspace_gateway.status()
        artifact_kind = str(payload.get("kind") or "").strip()
        if artifact_kind == "figure":
            return payload
        if _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS"):
            payload = self._compact_workspace_artifact_for_model(payload)
        source_kind = "upload" if artifact_kind == "upload" else "artifact"
        return self.preview_tool_response(payload, source_kind=source_kind)

    def get_upload(self, session_id: str | None, upload_id: str) -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.get_upload(
            session_id,
            upload_id,
            active_session_id=runtime.active_session_id,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return self.preview_tool_response(payload, source_kind="upload")

    def preview_tool_response(self, payload: dict[str, Any], *, source_kind: str) -> dict[str, Any]:
        max_lines = (
            self.settings.upload_preview_max_lines
            if str(source_kind).strip().lower() == "upload"
            else self.settings.artifact_preview_max_lines
        )
        return preview_model_response(
            payload,
            source_kind="upload" if str(source_kind).strip().lower() == "upload" else "artifact",
            max_lines=max_lines,
        )

    def delete_session_artifacts(self, session_id: str, artifact_ids: list[str]) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_artifacts(session_id, artifact_ids)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def delete_session_uploads(self, session_id: str, upload_ids: list[str]) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_uploads(session_id, upload_ids)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def archive_session(self, session_id: str) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.archive_session(session_id)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def plan_delete_sessions(self, session_ids: list[str]) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.plan_delete_sessions(session_ids)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def delete_sessions(self, session_ids: list[str]) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.delete_sessions(session_ids)
        payload["workspace"] = self.workspace_gateway.status()
        return payload

    def create_session_from_artifacts(
        self,
        *,
        title: str,
        description: str,
        sources: list[dict[str, str]],
        species: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.create_session_from_artifacts(
            title=title,
            description=description,
            sources=sources,
            species=clean_text(species or self.settings.default_species) or self.settings.default_species,
            tags=tags,
        )
        payload["workspace"] = self.workspace_gateway.status()
        payload["is_active"] = runtime.active_session_id == payload["session_id"]
        return payload

    def create_session_upload(
        self,
        session_id: str,
        *,
        data_path: str,
        source_mode: str,
        title: str = "",
        source_path: str = "",
        stored_path: str = "",
        file_name: str = "",
        mime_type: str = "",
        provider: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.create_upload(
            session_id,
            data_path=data_path,
            source_mode=source_mode,
            title=title,
            source_path=source_path,
            stored_path=stored_path,
            file_name=file_name,
            mime_type=mime_type,
            provider=provider,
            provenance=provenance,
        )
        payload["workspace"] = self.workspace_gateway.status()
        return self.preview_tool_response(payload, source_kind="upload")

    def get_report_markdown(self, session_id: str) -> str:
        _, workspace = self.workspace_gateway.require_workspace()
        return workspace.get_report_markdown(session_id)

    def resolve_gene(self, *, query: str, species: str | None = None) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        provider_result = self._resolve_gene_provider_result(query, species=effective_species)
        workspace_info = self._capture_to_active_session(
            kind="gene_resolution",
            title=f"Gene resolution: {clean_text(query)}",
            summary=provider_result.summary,
            provider=provider_result.provider,
            payload=provider_result.payload,
            provenance={"species": effective_species, "query": clean_text(query)},
        )
        return {
            "summary": provider_result.summary,
            "provider": provider_result.provider,
            "structured_data": provider_result.payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def resolve_gene_set(
        self,
        *,
        genes: list[str],
        species: str | None = None,
        limit: int = 300,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        warnings: list[str] = []
        resolution_payload = self._resolve_gene_identity_rows(
            normalize_gene_list(genes, species=effective_species),
            species=effective_species,
            warnings=warnings,
            limit=limit,
        )
        resolution_payload["species"] = effective_species
        resolution_payload["recommended_next_tools"] = [
            "investigate_gene_set",
            "enrich_gene_set",
            "gene_set_disease_context",
            "compare_gene_programs",
        ]
        resolution_payload["usage_guidance"] = (
            "Use this tool only when you need an explicit batch normalization audit. "
            "For ordinary gene-set analysis, prefer investigate_gene_set, enrich_gene_set, "
            "gene_set_disease_context, or compare_gene_programs because those tools normalize inputs internally."
        )

        input_count = len(resolution_payload.get("input_genes", []))
        resolved_count = int(resolution_payload.get("resolved_gene_count", 0))
        resolved_with_entrez_count = int(resolution_payload.get("resolved_with_entrez_count", 0))
        unresolved_count = len(resolution_payload.get("unresolved_genes", []))
        unresolved_without_entrez_count = len(resolution_payload.get("unresolved_without_entrez", []))

        summary = f"Resolved {resolved_count} of {input_count} input gene(s) to canonical identities."
        if unresolved_count:
            summary += f" {unresolved_count} gene(s) could not be resolved."
        if unresolved_without_entrez_count:
            summary += (
                f" {resolved_with_entrez_count} resolved gene(s) have Entrez identifiers available for "
                "disease-enrichment workflows."
            )

        workspace_info = self._capture_to_active_session(
            kind="gene_set_resolution",
            title="Gene set resolution",
            summary=summary,
            provider="gleskit_aggregate",
            payload=resolution_payload,
            provenance={
                "species": effective_species,
                "input_gene_count": input_count,
                "limit": int(limit),
            },
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": summary,
            "provider": "gleskit_aggregate",
            "structured_data": resolution_payload,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def gene_summary(self, *, query: str, species: str | None = None) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        resolution = self._resolve_gene_provider_result(query, species=effective_species)
        matches = resolution.payload.get("matches", [])
        if not matches:
            raise ProviderError(f"Unable to resolve gene '{clean_text(query)}'.")
        best = dict(matches[0])

        warnings: list[str] = []
        ensembl_payload: dict[str, Any] | None = None
        uniprot_payload: dict[str, Any] | None = None
        go_payload: dict[str, Any] | None = None
        sources = [resolution.provider]

        ensembl_gene_id = clean_text(best.get("ensembl_gene_id"))
        if ensembl_gene_id:
            try:
                ensembl_result = self.ensembl.lookup_gene(ensembl_gene_id)
                ensembl_payload = dict(ensembl_result.payload)
                sources.append("ensembl")
            except ProviderError as exc:
                warnings.append(str(exc))

        uniprot_accessions = [clean_text(item) for item in best.get("uniprot_ids", []) if clean_text(item)]
        if uniprot_accessions:
            try:
                uniprot_result = self.uniprot.get_protein(uniprot_accessions[0])
                uniprot_payload = dict(uniprot_result.payload)
                sources.append("uniprot")
            except ProviderError as exc:
                warnings.append(str(exc))
        elif clean_text(best.get("symbol")):
            try:
                protein_search = self.uniprot.search_proteins(clean_text(best.get("symbol")), species=effective_species, size=1)
                protein_matches = protein_search.payload.get("matches", [])
                if protein_matches:
                    protein_result = self.uniprot.get_protein(str(protein_matches[0].get("accession") or ""))
                    uniprot_payload = dict(protein_result.payload)
                    sources.append("uniprot")
            except ProviderError as exc:
                warnings.append(str(exc))

        quickgo_gene_product_id = self._quickgo_gene_product_id(uniprot_payload)
        if quickgo_gene_product_id:
            try:
                go_result = self.quickgo.gene_product_annotations(quickgo_gene_product_id, species=effective_species, limit=8)
                go_payload = dict(go_result.payload)
                sources.append("quickgo")
            except ProviderError as exc:
                warnings.append(str(exc))

        symbol = normalize_gene_token(best.get("symbol"), species=effective_species) or normalize_gene_token(
            query,
            species=effective_species,
        )
        payload = {
            "query": clean_text(query),
            "species": effective_species,
            "gene": {
                "symbol": best.get("symbol"),
                "name": best.get("name"),
                "summary": best.get("summary"),
                "aliases": best.get("aliases", []),
                "ensembl_gene_id": best.get("ensembl_gene_id"),
                "entrez_gene_id": best.get("entrez_gene_id"),
                "uniprot_ids": best.get("uniprot_ids", []),
                "genomic_position": best.get("genomic_position"),
                "biotype": best.get("biotype"),
                "matched_on": best.get("matched_on"),
                "matched_value": best.get("matched_value"),
            },
            "ensembl": ensembl_payload,
            "protein": uniprot_payload,
            "go_annotations": go_payload,
            "sources": sources,
        }
        summary_parts = [f"Assembled a gene summary for {symbol}."]
        if ensembl_payload:
            summary_parts.append("Included Ensembl genomic context.")
        if uniprot_payload:
            summary_parts.append("Included UniProt protein context.")
        if go_payload and go_payload.get("terms"):
            summary_parts.append("Included QuickGO functional annotations.")
        summary = " ".join(summary_parts)
        workspace_info = self._capture_to_active_session(
            kind="gene_summary",
            title=f"Gene summary: {symbol}",
            summary=summary,
            provider="gleskit_aggregate",
            payload=payload,
            provenance={"species": effective_species, "query": clean_text(query), "sources": sources},
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": summary,
            "provider": "gleskit_aggregate",
            "structured_data": payload,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def resolve_protein(self, *, query: str, species: str | None = None) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        provider_result = self.uniprot.search_proteins(query, species=effective_species)
        workspace_info = self._capture_to_active_session(
            kind="protein_resolution",
            title=f"Protein resolution: {clean_text(query)}",
            summary=provider_result.summary,
            provider=provider_result.provider,
            payload=provider_result.payload,
            provenance={"species": effective_species, "query": clean_text(query)},
        )
        return {
            "summary": provider_result.summary,
            "provider": provider_result.provider,
            "structured_data": provider_result.payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def protein_summary(self, *, query: str, species: str | None = None) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        resolution = self.uniprot.search_proteins(query, species=effective_species, size=5)
        matches = resolution.payload.get("matches", [])
        if not matches:
            raise ProviderError(f"Unable to resolve protein '{clean_text(query)}'.")
        best = dict(matches[0])
        accession = clean_text(best.get("accession"))
        if not accession:
            raise ProviderError(f"Top UniProt match for '{clean_text(query)}' did not include an accession.")

        protein_result = self.uniprot.get_protein(accession)
        protein_payload = dict(protein_result.payload)
        warnings: list[str] = []
        linked_gene: dict[str, Any] | None = None
        go_payload: dict[str, Any] | None = None
        chembl_payload: dict[str, Any] | None = None
        linked_gene_provider: str | None = None
        gene_token = clean_text(protein_payload.get("gene_primary"))
        if gene_token:
            try:
                gene_resolution = self._resolve_gene_provider_result(gene_token, species=effective_species)
                gene_matches = gene_resolution.payload.get("matches", [])
                if gene_matches:
                    top_gene = dict(gene_matches[0])
                    linked_gene_provider = gene_resolution.provider
                    linked_gene = {
                        "symbol": top_gene.get("symbol"),
                        "name": top_gene.get("name"),
                        "ensembl_gene_id": top_gene.get("ensembl_gene_id"),
                        "entrez_gene_id": top_gene.get("entrez_gene_id"),
                        "summary": top_gene.get("summary"),
                    }
            except ProviderError as exc:
                warnings.append(str(exc))

        quickgo_gene_product_id = self._quickgo_gene_product_id(protein_payload)
        if quickgo_gene_product_id:
            try:
                go_result = self.quickgo.gene_product_annotations(quickgo_gene_product_id, species=effective_species, limit=8)
                go_payload = dict(go_result.payload)
            except ProviderError as exc:
                warnings.append(str(exc))
        if self._is_human_species(effective_species):
            try:
                chembl_result = self.chembl.target_drug_context(
                    uniprot_accession=accession,
                    gene_symbol=gene_token,
                    limit=5,
                )
                chembl_payload = dict(chembl_result.payload)
            except ProviderError as exc:
                warnings.append(str(exc))

        payload = {
            "query": clean_text(query),
            "species": effective_species,
            "resolution": resolution.payload,
            "protein": protein_payload,
            "linked_gene": linked_gene,
            "go_annotations": go_payload,
            "drug_target_context": chembl_payload,
            "sources": ["uniprot"]
            + ([linked_gene_provider] if linked_gene and linked_gene_provider else [])
            + (["quickgo"] if go_payload else [])
            + (["chembl"] if chembl_payload else []),
        }
        protein_name = clean_text(protein_payload.get("protein_name")) or accession
        summary = f"Assembled a protein summary for {protein_name}."
        if linked_gene and linked_gene.get("symbol"):
            summary += f" Linked gene context was added for {linked_gene['symbol']}."
        if go_payload and go_payload.get("terms"):
            summary += " QuickGO functional annotations were added."
        if chembl_payload and chembl_payload.get("drug_count"):
            summary += f" ChEMBL drug-target context was added for {chembl_payload['drug_count']} drug(s)."
        workspace_info = self._capture_to_active_session(
            kind="protein_summary",
            title=f"Protein summary: {accession}",
            summary=summary,
            provider="gleskit_aggregate",
            payload=payload,
            provenance={"species": effective_species, "query": clean_text(query), "accession": accession},
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": summary,
            "provider": "gleskit_aggregate",
            "structured_data": payload,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def drug_target_context(
        self,
        *,
        gene: str,
        species: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        self._require_human_species(effective_species, tool_name="drug_target_context")
        resolution = self._resolve_gene_provider_result(gene, species=effective_species)
        matches = resolution.payload.get("matches", [])
        if not matches:
            raise ProviderError(f"Unable to resolve gene '{clean_text(gene)}' for ChEMBL lookup.")
        best = dict(matches[0])
        symbol = normalize_gene_token(best.get("symbol"), species=effective_species) or normalize_gene_token(
            gene,
            species=effective_species,
        )
        accession = self._best_uniprot_accession(best, species=effective_species)
        if not accession:
            raise ProviderError(f"Gene '{clean_text(gene)}' did not resolve to a UniProt accession for ChEMBL lookup.")

        provider_result = self.chembl.target_drug_context(
            uniprot_accession=accession,
            gene_symbol=symbol,
            limit=limit,
        )
        payload = dict(provider_result.payload)
        payload["input_gene"] = normalize_gene_token(gene, species=effective_species)
        payload["normalized_gene"] = {
            "symbol": symbol,
            "matched_on": best.get("matched_on"),
            "matched_value": best.get("matched_value"),
            "ensembl_gene_id": best.get("ensembl_gene_id"),
            "uniprot_accession": accession,
        }
        workspace_info = self._capture_to_active_session(
            kind="drug_target_context",
            title=f"Drug target context: {symbol}",
            summary=provider_result.summary,
            provider=provider_result.provider,
            payload=payload,
            provenance={"input_gene": gene, "species": effective_species, "uniprot_accession": accession},
        )
        return {
            "summary": provider_result.summary,
            "provider": provider_result.provider,
            "structured_data": payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def interaction_network(
        self,
        *,
        genes: list[str],
        species: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        cleaned_genes = normalize_gene_list(genes, species=effective_species)
        provider_result = self.stringdb.interaction_network(cleaned_genes, species=effective_species, limit=limit)
        workspace_info = self._capture_to_active_session(
            kind="interaction_network",
            title="Interaction network",
            summary=provider_result.summary,
            provider=provider_result.provider,
            payload=provider_result.payload,
            provenance={"species": effective_species, "genes": cleaned_genes, "limit": limit},
        )
        return {
            "summary": provider_result.summary,
            "provider": provider_result.provider,
            "structured_data": provider_result.payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def pathway_context(
        self,
        *,
        genes: list[str],
        species: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        cleaned_genes = normalize_gene_list(genes, species=effective_species)
        if not cleaned_genes:
            raise ProviderError("At least one gene is required to build pathway context.")

        warnings: list[str] = []
        enrichment_result = self.gprofiler.enrich_genes(
            cleaned_genes,
            species=effective_species,
            limit=max(1, min(int(limit), 15)),
            sources=["REAC"],
        )
        enrichment_payload = dict(enrichment_result.payload)
        reactome_terms = [row for row in enrichment_payload.get("terms", []) if str(row.get("source") or "").upper() == "REAC"]
        pathways: list[dict[str, Any]] = []
        for row in reactome_terms[: max(1, min(int(limit), 10))]:
            pathway_id = clean_text(row.get("term_id"))
            if not pathway_id:
                continue
            try:
                detail = self.reactome.pathway(pathway_id)
                pathway_payload = dict(detail.payload)
                pathway_payload["enrichment_term"] = dict(row)
                pathways.append(pathway_payload)
            except ProviderError as exc:
                warnings.append(str(exc))

        payload = {
            "genes": cleaned_genes,
            "species": effective_species,
            "reactome_enrichment": enrichment_payload,
            "pathways": pathways,
        }
        summary = (
            f"Built pathway context for {len(cleaned_genes)} gene(s) with {len(pathways)} Reactome pathway detail(s)."
        )
        workspace_info = self._capture_to_active_session(
            kind="pathway_context",
            title="Pathway context",
            summary=summary,
            provider="gleskit_aggregate",
            payload=payload,
            provenance={"species": effective_species, "genes": cleaned_genes, "limit": limit},
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": summary,
            "provider": "gleskit_aggregate",
            "structured_data": payload,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def expression_context(
        self,
        *,
        gene: str,
        species: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        resolution = self._resolve_gene_provider_result(gene, species=effective_species)
        matches = resolution.payload.get("matches", [])
        if not matches:
            raise ProviderError(f"Unable to resolve gene '{clean_text(gene)}' for expression context.")
        best = dict(matches[0])

        symbol = normalize_gene_token(best.get("symbol"), species=effective_species) or normalize_gene_token(
            gene,
            species=effective_species,
        )
        ensembl_gene_id = clean_text(best.get("ensembl_gene_id")) or None
        warnings: list[str] = []
        sources = [resolution.provider]
        gtex_payload: dict[str, Any] | None = None
        atlas_payload: dict[str, Any] | None = None

        if self._is_human_species(effective_species):
            try:
                gtex_result = self.gtex.expression_context(symbol or ensembl_gene_id or clean_text(gene), limit=limit)
                gtex_payload = dict(gtex_result.payload)
                sources.append("gtex")
            except ProviderError as exc:
                warnings.append(str(exc))
        else:
            warnings.append(f"GTEx reference expression is currently human-only; skipped for species '{effective_species}'.")

        try:
            atlas_result = self.expression_atlas.reference_expression(
                symbol or ensembl_gene_id or clean_text(gene),
                species=effective_species,
                alternate_identifiers=[ensembl_gene_id] if ensembl_gene_id else [],
                limit=limit,
            )
            atlas_payload = dict(atlas_result.payload)
            sources.append("expression_atlas")
        except ProviderError as exc:
            warnings.append(str(exc))

        if not gtex_payload and not atlas_payload:
            raise ProviderError(
                "Expression context lookup failed after gene normalization. "
                f"Input='{clean_text(gene)}', resolved_symbol='{symbol}', "
                f"ensembl_gene_id='{ensembl_gene_id or 'n/a'}'. "
                + " ".join(warnings)
            )

        payload = {
            "query_gene": normalize_gene_token(gene, species=effective_species),
            "species": effective_species,
            "normalized_gene": {
                "symbol": symbol,
                "name": best.get("name"),
                "summary": best.get("summary"),
                "matched_on": best.get("matched_on"),
                "matched_value": best.get("matched_value"),
                "ensembl_gene_id": ensembl_gene_id,
            },
            "gtex": gtex_payload,
            "expression_atlas": atlas_payload,
            "sources": sources,
        }
        summary_parts = [f"Built expression context for {symbol}."]
        if gtex_payload:
            summary_parts.append(f"Included GTEx tissue medians across {gtex_payload.get('tissue_count', 0)} tissue(s).")
        if atlas_payload:
            experiment = (atlas_payload.get("reference_experiment") or {}).get("accession")
            if experiment:
                summary_parts.append(f"Included Expression Atlas reference experiment {experiment}.")
            else:
                summary_parts.append("Included Expression Atlas reference baseline context.")
            if (atlas_payload.get("query_gene_profile") or {}).get("tissue_count"):
                summary_parts.append("Expression Atlas returned a query-linked expression profile.")
        summary = " ".join(summary_parts)
        workspace_info = self._capture_to_active_session(
            kind="expression_context",
            title=f"Expression context: {symbol}",
            summary=summary,
            provider="gleskit_aggregate",
            payload=payload,
            provenance={"input_gene": gene, "species": effective_species, "symbol": symbol},
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": summary,
            "provider": "gleskit_aggregate",
            "structured_data": payload,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def analyze_rnaseq_result(
        self,
        *,
        result_path: str = "",
        table_text: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        species: str | None = None,
        label: str = "",
        comparison: str = "",
        gene_column: str = "",
        log2_fold_change_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        base_mean_column: str = "",
        dispersion_column: str = "",
        stat_column: str = "",
        focus_genes: list[str] | None = None,
        gene_alias_table_path: str = "",
        gene_alias_table_text: str = "",
        gene_alias_query_column: str = "",
        gene_alias_target_column: str = "",
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 0.0,
        log2_fold_change_threshold: float = 0.0,
        top_n: int = 25,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        resolved_result_path, converted_rds, handoff_manifest = self._normalize_rnaseq_result_path(result_path)
        focus_gene_list = [clean_text(gene) for gene in (focus_genes or []) if clean_text(gene)]
        focus_gene_aliases = self._build_focus_gene_aliases(
            focus_genes=focus_gene_list,
            species=effective_species,
            gene_alias_table_path=gene_alias_table_path,
            gene_alias_table_text=gene_alias_table_text,
            gene_alias_query_column=gene_alias_query_column,
            gene_alias_target_column=gene_alias_target_column,
        )
        handoff_column_map = dict((handoff_manifest.get("column_map") or {}) if isinstance(handoff_manifest, dict) else {})
        parsed = self.rnaseq_backend.analyze_result(
            result_path=resolved_result_path,
            table_text=table_text,
            label=label,
            comparison=comparison,
            gene_column=gene_column or clean_text(handoff_column_map.get("gene")),
            log2_fold_change_column=log2_fold_change_column or clean_text(handoff_column_map.get("log2FoldChange")),
            padj_column=padj_column or clean_text(handoff_column_map.get("padj")),
            p_value_column=p_value_column or clean_text(handoff_column_map.get("pvalue")),
            base_mean_column=base_mean_column or clean_text(handoff_column_map.get("baseMean")),
            dispersion_column=dispersion_column or clean_text(handoff_column_map.get("dispersion")),
            stat_column=stat_column or clean_text(handoff_column_map.get("stat")),
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            padj_threshold=padj_threshold,
            p_value_threshold=p_value_threshold,
            base_mean_threshold=base_mean_threshold,
            log2_fold_change_threshold=log2_fold_change_threshold,
            top_n=top_n,
            focus_genes=focus_gene_list,
            focus_gene_aliases=focus_gene_aliases,
        )
        ranked_signature = [dict(row) for row in (parsed.pop("ranked_signature", []) or []) if isinstance(row, dict)]
        parsed_input = dict(parsed.get("input") or {})
        parsed_input["dispersion_column"] = clean_text(dispersion_column) or None
        parsed_input["result_path"] = clean_text(result_path) or parsed_input.get("result_path")
        parsed_input["focus_genes"] = focus_gene_list
        parsed_input["gene_alias_table_path"] = clean_text(gene_alias_table_path) or None
        if handoff_manifest:
            parsed_input["handoff_manifest_path"] = clean_text(result_path) or None
            parsed_input["handoff_manifest"] = {
                "label": handoff_manifest.get("label"),
                "comparison": handoff_manifest.get("comparison"),
                "result_table_path": ((handoff_manifest.get("files") or {}) if isinstance(handoff_manifest, dict) else {}).get("result_table_path"),
                "column_map": handoff_column_map,
            }
        if converted_rds:
            parsed_input["converted_result_path"] = resolved_result_path
            parsed_input["result_source_format"] = "rds"
            parsed_input["rds_conversion"] = converted_rds
        parsed["input"] = parsed_input

        warnings: list[str] = []
        lead_resolution_limit = 8
        up_genes = normalize_gene_list(parsed["significant_gene_sets"]["upregulated"], species=effective_species)
        down_genes = normalize_gene_list(parsed["significant_gene_sets"]["downregulated"], species=effective_species)
        up_lead_inputs = normalize_gene_list(
            (row.get("gene") for row in parsed["top_genes"]["upregulated"][:lead_resolution_limit]),
            species=effective_species,
        )
        down_lead_inputs = normalize_gene_list(
            (row.get("gene") for row in parsed["top_genes"]["downregulated"][:lead_resolution_limit]),
            species=effective_species,
        )
        up_resolution = self._resolve_gene_tokens(
            up_lead_inputs,
            species=effective_species,
            limit=lead_resolution_limit,
            warnings=warnings,
        )
        down_resolution = self._resolve_gene_tokens(
            down_lead_inputs,
            species=effective_species,
            limit=lead_resolution_limit,
            warnings=warnings,
        )
        up_resolution["resolution_scope"] = "lead_subset"
        up_resolution["resolution_limit"] = lead_resolution_limit
        up_resolution["total_significant_gene_count"] = len(up_genes)
        down_resolution["resolution_scope"] = "lead_subset"
        down_resolution["resolution_limit"] = lead_resolution_limit
        down_resolution["total_significant_gene_count"] = len(down_genes)

        def _up_enrichment_job() -> tuple[dict[str, Any], list[str]]:
            local_warnings: list[str] = []
            return (
                self._safe_enrichment_context(
                    up_genes,
                    species=effective_species,
                    label=f"{clean_text(label) or 'RNA-seq'} upregulated genes",
                    warnings=local_warnings,
                ),
                local_warnings,
            )

        def _down_enrichment_job() -> tuple[dict[str, Any], list[str]]:
            local_warnings: list[str] = []
            return (
                self._safe_enrichment_context(
                    down_genes,
                    species=effective_species,
                    label=f"{clean_text(label) or 'RNA-seq'} downregulated genes",
                    warnings=local_warnings,
                ),
                local_warnings,
            )

        def _ranked_gsea_job() -> tuple[dict[str, Any], list[str]]:
            local_warnings: list[str] = []
            return (
                self._safe_ranked_gsea_context(
                    ranked_signature,
                    species=effective_species,
                    label=f"{clean_text(label) or 'RNA-seq'} ranked signature",
                    warnings=local_warnings,
                ),
                local_warnings,
            )

        def _disease_enrichment_job() -> tuple[dict[str, Any], list[str]]:
            local_warnings: list[str] = []
            return (
                self._safe_gene_set_disease_context(
                    parsed["significant_gene_sets"]["all_significant"],
                    species=effective_species,
                    label=f"{clean_text(label) or 'RNA-seq'} disease enrichment",
                    warnings=local_warnings,
                ),
                local_warnings,
            )

        analysis_jobs = {
            "up_enrichment": _up_enrichment_job,
            "down_enrichment": _down_enrichment_job,
            "ranked_gsea": _ranked_gsea_job,
            "disease_enrichment": _disease_enrichment_job,
        }
        analysis_results: dict[str, dict[str, Any]] = {}
        max_workers = max(1, min(len(analysis_jobs), int(getattr(self.settings, "local_max_workers", 1) or 1)))
        if max_workers <= 1:
            for name, job in analysis_jobs.items():
                payload_result, job_warnings = job()
                analysis_results[name] = payload_result
                warnings.extend(job_warnings)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {executor.submit(job): name for name, job in analysis_jobs.items()}
                for future in as_completed(future_map):
                    name = future_map[future]
                    payload_result, job_warnings = future.result()
                    analysis_results[name] = payload_result
                    warnings.extend(job_warnings)

        up_enrichment = analysis_results["up_enrichment"]
        down_enrichment = analysis_results["down_enrichment"]
        ranked_gsea = analysis_results["ranked_gsea"]
        disease_enrichment = analysis_results["disease_enrichment"]

        lead_gene_map = {
            entry["canonical_symbol"]: entry
            for entry in up_resolution["resolved_entries"] + down_resolution["resolved_entries"]
            if entry.get("canonical_symbol")
        }
        lead_gene_candidates = unique_preserve_order(up_lead_inputs[:2] + down_lead_inputs[:2])[:4]
        lead_genes = [
            str((lead_gene_map.get(gene_symbol) or {}).get("canonical_symbol") or gene_symbol)
            for gene_symbol in lead_gene_candidates
        ]
        disease_rows = self._lead_gene_disease_rows(
            lead_genes,
            lead_gene_map=lead_gene_map,
            warnings=warnings,
            limit=5,
        )

        literature_terms = [
            str(row.get("term_name") or "")
            for row in (
                (up_enrichment.get("terms") or [])[:2]
                + (down_enrichment.get("terms") or [])[:2]
                + (ranked_gsea.get("positive_terms") or [])[:1]
                + (ranked_gsea.get("negative_terms") or [])[:1]
                + (disease_enrichment.get("terms") or [])[:2]
            )
            if isinstance(row, dict)
        ]
        literature_query = build_literature_query(
            genes=lead_genes[:3],
            terms=literature_terms,
            question=clean_text(comparison) or clean_text(label) or "RNA-seq differential expression",
        )
        paper_payload = {"query": literature_query, "paper_count": 0, "papers": []}
        if literature_query:
            try:
                paper_result = self.europepmc.search_papers(literature_query, limit=5)
                paper_payload = dict(paper_result.payload)
            except ProviderError as exc:
                warnings.append(str(exc))

        enrichment_terms = [
            dict(row)
            for row in ((up_enrichment.get("terms") or []) + (down_enrichment.get("terms") or []))
            if isinstance(row, dict)
        ]
        bundle_payload = build_evidence_bundle_payload(
            question=(
                clean_text(comparison)
                or f"What biology is supported by the {clean_text(label) or 'RNA-seq'} differential expression signature?"
            ),
            genes=lead_genes,
            enrichment_terms=enrichment_terms,
            disease_associations=disease_rows,
            papers=[dict(row) for row in (paper_payload.get("papers") or []) if isinstance(row, dict)],
        )

        payload = {
            "provider": "gleskit_aggregate",
            "species": effective_species,
            "input": parsed["input"],
            "table": parsed["table"],
            "summary_stats": parsed["summary"],
            "top_genes": parsed["top_genes"],
            "focus_rows": parsed.get("focus_rows") or [],
            "significant_gene_sets": parsed["significant_gene_sets"],
            "ranked_gene_scores": ranked_signature,
            "gene_score_count": len(ranked_signature),
            "resolved_gene_sets": {
                "upregulated": up_resolution,
                "downregulated": down_resolution,
            },
            "enrichment": {
                "upregulated": up_enrichment,
                "downregulated": down_enrichment,
            },
            "ranked_gsea": ranked_gsea,
            "disease_enrichment": disease_enrichment,
            "disease_associations": {
                "lead_genes": lead_genes,
                "rows": disease_rows,
            },
            "papers": paper_payload,
            "evidence_bundle": bundle_payload,
        }
        artifact_payload = dict(payload)
        artifact_payload["results"] = list(parsed.get("results") or [])
        derived_inputs = self._write_deseq2_handoff_files(
            label=label,
            comparison=comparison,
            counts_path=clean_text(resolved_result_path) or clean_text(result_path) or "",
            species=effective_species,
            structured_data=artifact_payload,
        )
        question_ready_counts = {
            "result_row_count": parsed["summary"].get("row_count"),
            "ranked_gene_count": parsed["summary"].get("ranked_gene_count"),
            "significant_gene_count": parsed["summary"].get("significant_gene_count"),
            "upregulated_gene_count": parsed["summary"].get("upregulated_count"),
            "downregulated_gene_count": parsed["summary"].get("downregulated_count"),
            "padj_threshold": padj_threshold,
            "p_value_threshold": p_value_threshold,
            "log2_fold_change_threshold": log2_fold_change_threshold,
        }
        summary = (
            f"Analyzed RNA-seq differential expression with {parsed['summary']['row_count']} ranked row(s), "
            f"{parsed['summary']['significant_gene_count']} significant gene(s), "
            f"{parsed['summary']['upregulated_count']} upregulated and {parsed['summary']['downregulated_count']} downregulated."
        )
        if up_enrichment.get("term_count"):
            summary += f" Upregulated enrichment returned {up_enrichment['term_count']} term(s)."
        if down_enrichment.get("term_count"):
            summary += f" Downregulated enrichment returned {down_enrichment['term_count']} term(s)."
        if ranked_gsea.get("term_count"):
            summary += f" Ranked GSEA returned {ranked_gsea['term_count']} term(s)."
        if disease_enrichment.get("term_count"):
            summary += f" Disease enrichment returned {disease_enrichment['term_count']} term(s)."
        up_top_term = clean_text(((up_enrichment.get("top_term") or {}) if isinstance(up_enrichment, dict) else {}).get("term_name"))
        if up_top_term:
            summary += f" Top upregulated pathway: {up_top_term}."
        down_top_term = clean_text(((down_enrichment.get("top_term") or {}) if isinstance(down_enrichment, dict) else {}).get("term_name"))
        if down_top_term:
            summary += f" Top downregulated pathway: {down_top_term}."
        focus_rows = [row for row in (payload.get("focus_rows") or []) if isinstance(row, dict) and row.get("matched")]
        if focus_rows:
            focus_bits = []
            for row in focus_rows[:3]:
                metrics = row.get("key_metrics") or {}
                matched_identifier = clean_text(row.get("matched_identifier")) or clean_text(row.get("query_gene"))
                log2fc = metrics.get("log2_fold_change")
                padj_value = metrics.get("padj")
                stat_value = metrics.get("stat")
                fragments = [matched_identifier]
                if log2fc not in (None, ""):
                    fragments.append(f"log2FC={log2fc}")
                if padj_value not in (None, ""):
                    fragments.append(f"padj={padj_value}")
                elif stat_value not in (None, ""):
                    fragments.append(f"stat={stat_value}")
                focus_bits.append(", ".join(fragments))
            if focus_bits:
                summary += " Focus genes: " + "; ".join(focus_bits) + "."
        if converted_rds:
            summary += " The input RDS result was converted internally to a reusable TSV for analysis."
        payload["summary"] = summary
        payload["derived_inputs"] = derived_inputs
        payload["question_ready_counts"] = question_ready_counts
        artifact_payload["summary"] = summary
        artifact_payload["derived_inputs"] = derived_inputs
        artifact_payload["question_ready_counts"] = question_ready_counts
        if _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS"):
            structured_data = self._compact_rnaseq_result_structured_data_for_model(
                artifact_structured_data=artifact_payload,
                derived_inputs=derived_inputs,
                species=effective_species,
                label=label,
                comparison=comparison,
                result_path=resolved_result_path or result_path,
                question_ready_counts=question_ready_counts,
            )
        else:
            structured_data = payload
        workspace_info = self._capture_to_active_session(
            kind="rnaseq_analysis",
            title=clean_text(label) or clean_text(comparison) or "RNA-seq analysis",
            summary=summary,
            provider="gleskit_aggregate",
            payload=artifact_payload,
            provenance={
                "species": effective_species,
                "result_path": clean_text(resolved_result_path) or None,
                "sheet_name": clean_text(sheet_name) or None,
                "comparison": clean_text(comparison) or None,
                "label": clean_text(label) or None,
            },
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": summary,
            "provider": "gleskit_aggregate",
            "structured_data": structured_data,
            "derived_inputs": derived_inputs,
            "manifest_path": derived_inputs.get("manifest_path"),
            "result_table_path": derived_inputs.get("result_table_path"),
            "column_map": derived_inputs.get("column_map"),
            "all_significant_genes_path": derived_inputs.get("all_significant_genes_path"),
            "upregulated_genes_path": derived_inputs.get("upregulated_genes_path"),
            "downregulated_genes_path": derived_inputs.get("downregulated_genes_path"),
            "ranked_gene_scores_path": derived_inputs.get("ranked_gene_scores_path"),
            "result_row_count": derived_inputs.get("result_row_count"),
            "all_significant_gene_count": derived_inputs.get("all_significant_gene_count"),
            "upregulated_gene_count": derived_inputs.get("upregulated_gene_count"),
            "downregulated_gene_count": derived_inputs.get("downregulated_gene_count"),
            "ranked_gene_score_count": derived_inputs.get("ranked_gene_score_count"),
            "question_ready_counts": question_ready_counts,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def _compact_gene_preview(self, genes: Any, *, limit: int = 12) -> list[str]:
        normalized = normalize_gene_list(genes or [], species=None)
        return normalized[: max(1, int(limit))]

    def _compact_deseq2_structured_data_for_model(
        self,
        *,
        artifact_structured_data: dict[str, Any],
        derived_inputs: dict[str, Any],
        species: str,
        label: str,
        comparison: str,
        counts_path: str,
    ) -> dict[str, Any]:
        top_genes = artifact_structured_data.get("top_genes") or {}
        significant_gene_sets = artifact_structured_data.get("significant_gene_sets") or {}
        compact_structured_data: dict[str, Any] = {
            "provider": artifact_structured_data.get("provider"),
            "summary": artifact_structured_data.get("summary"),
            "input": artifact_structured_data.get("input"),
            "counts": artifact_structured_data.get("counts"),
            "summary_stats": artifact_structured_data.get("summary_stats"),
            "top_genes": {
                "upregulated": list((top_genes.get("upregulated") or [])[:10]) if isinstance(top_genes, dict) else [],
                "downregulated": list((top_genes.get("downregulated") or [])[:10]) if isinstance(top_genes, dict) else [],
            },
            "significant_gene_set_preview": {
                "upregulated": self._compact_gene_preview(
                    (significant_gene_sets.get("upregulated") if isinstance(significant_gene_sets, dict) else []) or [],
                    limit=10,
                ),
                "downregulated": self._compact_gene_preview(
                    (significant_gene_sets.get("downregulated") if isinstance(significant_gene_sets, dict) else []) or [],
                    limit=10,
                ),
                "all_significant": self._compact_gene_preview(
                    (significant_gene_sets.get("all_significant") if isinstance(significant_gene_sets, dict) else []) or [],
                    limit=10,
                ),
                "preview_only": True,
            },
            "focus_rows": artifact_structured_data.get("focus_rows"),
            "derived_inputs": derived_inputs,
            "species": species,
            "label": clean_text(label) or None,
            "comparison": clean_text(comparison) or None,
            "upload_path": clean_text(counts_path) or None,
            "handoff_root": derived_inputs.get("handoff_root"),
            "manifest_path": derived_inputs.get("manifest_path"),
            "result_table_path": derived_inputs.get("result_table_path"),
            "column_map": derived_inputs.get("column_map"),
            "all_significant_genes_path": derived_inputs.get("all_significant_genes_path"),
            "upregulated_genes_path": derived_inputs.get("upregulated_genes_path"),
            "downregulated_genes_path": derived_inputs.get("downregulated_genes_path"),
            "ranked_gene_scores_path": derived_inputs.get("ranked_gene_scores_path"),
            "result_row_count": derived_inputs.get("result_row_count"),
            "all_significant_gene_count": derived_inputs.get("all_significant_gene_count"),
            "upregulated_gene_count": derived_inputs.get("upregulated_gene_count"),
            "downregulated_gene_count": derived_inputs.get("downregulated_gene_count"),
            "ranked_gene_score_count": derived_inputs.get("ranked_gene_score_count"),
            "question_ready_counts": artifact_structured_data.get("question_ready_counts"),
            "analysis_route": artifact_structured_data.get("analysis_route"),
            "preparation_summary": artifact_structured_data.get("preparation_summary"),
            "agent_payload_mode": "compact",
        }
        return compact_structured_data

    def _compact_rnaseq_result_structured_data_for_model(
        self,
        *,
        artifact_structured_data: dict[str, Any],
        derived_inputs: dict[str, Any],
        species: str,
        label: str,
        comparison: str,
        result_path: str,
        question_ready_counts: dict[str, Any],
    ) -> dict[str, Any]:
        top_genes = artifact_structured_data.get("top_genes") or {}
        significant_gene_sets = artifact_structured_data.get("significant_gene_sets") or {}
        enrichment = artifact_structured_data.get("enrichment") or {}
        ranked_gsea = artifact_structured_data.get("ranked_gsea") or {}
        disease_enrichment = artifact_structured_data.get("disease_enrichment") or {}
        papers = artifact_structured_data.get("papers") or {}
        resolved_gene_sets = artifact_structured_data.get("resolved_gene_sets") or {}

        compact_resolved_gene_sets: dict[str, Any] = {}
        if isinstance(resolved_gene_sets, dict):
            for direction in ("upregulated", "downregulated"):
                payload = dict(resolved_gene_sets.get(direction) or {})
                compact_resolved_gene_sets[direction] = {
                    "resolved_genes": list(payload.get("resolved_genes") or [])[:8],
                    "resolution_scope": payload.get("resolution_scope"),
                    "resolution_limit": payload.get("resolution_limit"),
                    "total_significant_gene_count": payload.get("total_significant_gene_count"),
                    "resolved_entry_count": len(list(payload.get("resolved_entries") or [])),
                }

        compact_structured_data: dict[str, Any] = {
            "provider": artifact_structured_data.get("provider"),
            "summary": artifact_structured_data.get("summary"),
            "input": artifact_structured_data.get("input"),
            "table": artifact_structured_data.get("table"),
            "summary_stats": artifact_structured_data.get("summary_stats"),
            "top_genes": {
                "upregulated": list((top_genes.get("upregulated") or [])[:10]) if isinstance(top_genes, dict) else [],
                "downregulated": list((top_genes.get("downregulated") or [])[:10]) if isinstance(top_genes, dict) else [],
            },
            "significant_gene_set_preview": {
                "upregulated": self._compact_gene_preview(
                    (significant_gene_sets.get("upregulated") if isinstance(significant_gene_sets, dict) else []) or [],
                    limit=10,
                ),
                "downregulated": self._compact_gene_preview(
                    (significant_gene_sets.get("downregulated") if isinstance(significant_gene_sets, dict) else []) or [],
                    limit=10,
                ),
                "all_significant": self._compact_gene_preview(
                    (significant_gene_sets.get("all_significant") if isinstance(significant_gene_sets, dict) else []) or [],
                    limit=10,
                ),
                "preview_only": True,
            },
            "focus_rows": artifact_structured_data.get("focus_rows"),
            "resolved_gene_sets": compact_resolved_gene_sets,
            "enrichment": {
                "upregulated": {
                    "term_count": (enrichment.get("upregulated") or {}).get("term_count"),
                    "top_terms": self._top_enrichment_terms(dict((enrichment.get("upregulated") or {}))),
                },
                "downregulated": {
                    "term_count": (enrichment.get("downregulated") or {}).get("term_count"),
                    "top_terms": self._top_enrichment_terms(dict((enrichment.get("downregulated") or {}))),
                },
            },
            "ranked_gsea": {
                "term_count": (ranked_gsea or {}).get("term_count"),
                "gene_score_count": (ranked_gsea or {}).get("gene_score_count"),
                "positive_terms": self._top_enrichment_terms(
                    {"terms": list((ranked_gsea.get("positive_terms") or []) if isinstance(ranked_gsea, dict) else [])},
                    limit=3,
                ),
                "negative_terms": self._top_enrichment_terms(
                    {"terms": list((ranked_gsea.get("negative_terms") or []) if isinstance(ranked_gsea, dict) else [])},
                    limit=3,
                ),
            },
            "disease_enrichment": {
                "term_count": (disease_enrichment or {}).get("term_count"),
                "top_terms": self._top_enrichment_terms(dict(disease_enrichment if isinstance(disease_enrichment, dict) else {})),
            },
            "disease_associations": artifact_structured_data.get("disease_associations"),
            "papers": {
                "query": (papers or {}).get("query"),
                "paper_count": (papers or {}).get("paper_count"),
                "papers": list((papers.get("papers") or [])[:3]) if isinstance(papers, dict) else [],
            },
            "derived_inputs": derived_inputs,
            "species": species,
            "label": clean_text(label) or None,
            "comparison": clean_text(comparison) or None,
            "result_path": clean_text(result_path) or None,
            "manifest_path": derived_inputs.get("manifest_path"),
            "result_table_path": derived_inputs.get("result_table_path"),
            "column_map": derived_inputs.get("column_map"),
            "all_significant_genes_path": derived_inputs.get("all_significant_genes_path"),
            "upregulated_genes_path": derived_inputs.get("upregulated_genes_path"),
            "downregulated_genes_path": derived_inputs.get("downregulated_genes_path"),
            "ranked_gene_scores_path": derived_inputs.get("ranked_gene_scores_path"),
            "result_row_count": derived_inputs.get("result_row_count"),
            "all_significant_gene_count": derived_inputs.get("all_significant_gene_count"),
            "upregulated_gene_count": derived_inputs.get("upregulated_gene_count"),
            "downregulated_gene_count": derived_inputs.get("downregulated_gene_count"),
            "ranked_gene_score_count": derived_inputs.get("ranked_gene_score_count"),
            "question_ready_counts": question_ready_counts,
            "agent_payload_mode": "compact",
        }
        return compact_structured_data

    @staticmethod
    def _compact_tabular_inspection_structured_data_for_model(
        structured_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep inspection responses actionable without replaying whole workbook summaries to the model."""

        # Sample names are schema, rather than table contents. A complete
        # bounded list prevents agents from constructing partial metadata for
        # a small raw-count matrix while still keeping wide matrices compact.
        matrix_sample_columns = [
            clean_text(item)
            for item in (structured_data.get("matrix_sample_columns") or [])
            if clean_text(item)
        ]
        max_inline_sample_columns = 64

        def compact_sheet_role(row: dict[str, Any]) -> dict[str, Any]:
            provenance_notes = list(row.get("provenance_notes") or [])
            inspection_note = next(
                (note for note in provenance_notes if str(note).startswith("inspection_preview_rows=")),
                "",
            )
            return {
                "sheet_name": clean_text(row.get("sheet_name")),
                "role": clean_text(row.get("role")),
                "detected_data_type": clean_text(row.get("detected_data_type")),
                "row_identifier_column": clean_text(row.get("row_identifier_column")),
                "candidate_sample_column": clean_text(row.get("candidate_sample_column")),
                "candidate_group_columns": list(row.get("candidate_group_columns") or []),
                "candidate_group_levels": dict(row.get("candidate_group_levels") or {}),
                "candidate_sample_preview": list(row.get("candidate_sample_preview") or [])[:5],
                "matrix_sample_preview": list(row.get("matrix_sample_preview") or [])[:5],
                "header_row": row.get("header_row"),
                "header_confidence": clean_text(row.get("header_confidence")),
                "recommended_arguments": dict(row.get("recommended_arguments") or {}),
                "validation": dict(row.get("validation") or {}),
                "provenance_notes": list(
                    dict.fromkeys([*provenance_notes[:2], inspection_note])
                ),
            }

        recommended_arguments = dict(structured_data.get("recommended_arguments") or {})
        candidate_group_levels = dict(structured_data.get("candidate_group_levels") or {})
        if not candidate_group_levels:
            candidate_group_levels = dict(recommended_arguments.get("candidate_group_levels") or {})
        return {
            "path": clean_text(structured_data.get("path")),
            "detected_data_type": clean_text(structured_data.get("detected_data_type")),
            "row_identifier_column": clean_text(structured_data.get("row_identifier_column")),
            "candidate_sample_column": clean_text(structured_data.get("candidate_sample_column")),
            "candidate_group_columns": list(structured_data.get("candidate_group_columns") or []),
            "candidate_group_levels": candidate_group_levels,
            "candidate_sample_preview": list(structured_data.get("candidate_sample_preview") or [])[:5],
            "matrix_sample_preview": list(structured_data.get("matrix_sample_preview") or [])[:5],
            "matrix_sample_columns": matrix_sample_columns[:max_inline_sample_columns],
            "matrix_sample_column_count": len(matrix_sample_columns),
            "matrix_sample_columns_truncated": len(matrix_sample_columns) > max_inline_sample_columns,
            "candidate_sample_group_map": dict(structured_data.get("candidate_sample_group_map") or {}),
            "candidate_gene_id_columns": list(structured_data.get("candidate_gene_id_columns") or []),
            "header_row": structured_data.get("header_row"),
            "header_confidence": clean_text(structured_data.get("header_confidence")),
            "matrix_orientation": clean_text(structured_data.get("matrix_orientation")),
            "candidate_analysis_families": list(structured_data.get("candidate_analysis_families") or []),
            "recommended_arguments": recommended_arguments,
            "sheet_roles": [
                compact_sheet_role(row)
                for row in list(structured_data.get("sheet_roles") or [])
                if isinstance(row, dict)
            ],
            "ambiguities": list(structured_data.get("ambiguities") or [])[:5],
            "next_step_hints": list(structured_data.get("next_step_hints") or [])[:5],
            "validation": dict(structured_data.get("validation") or {}),
            "warnings": list(structured_data.get("warnings") or [])[:5],
            "one_line_summary": clean_text(structured_data.get("one_line_summary")),
            "shape": dict(structured_data.get("shape") or {}),
            "agent_payload_mode": "compact",
        }

    def _compact_differential_result_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Expose auditable result facts without serializing a whole omics table."""
        mapping_rows = [row for row in payload.get("mapping_audit") or [] if isinstance(row, dict)]
        mapping_counts: dict[str, int] = {}
        for row in mapping_rows:
            status = clean_text(row.get("status")) or "unknown"
            mapping_counts[status] = mapping_counts.get(status, 0) + 1

        def strongest(direction: str) -> list[dict[str, Any]]:
            rows = [
                row for row in payload.get("gene_evidence") or []
                if isinstance(row, dict) and clean_text(row.get("direction")) == direction
            ]
            rows.sort(
                key=lambda row: min(
                    [
                        float(observation.get("padj"))
                        if observation.get("padj") not in {None, ""}
                        else float(observation.get("p_value"))
                        if observation.get("p_value") not in {None, ""}
                        else float("inf")
                        for observation in row.get("observations") or []
                        if isinstance(observation, dict)
                    ]
                    or [float("inf")]
                )
            )
            preview: list[dict[str, Any]] = []
            for row in rows[:20]:
                observation = next(
                    (item for item in row.get("observations") or [] if isinstance(item, dict) and item.get("significant")),
                    None,
                )
                preview.append({
                    "gene": clean_text(row.get("gene")),
                    "direction": direction,
                    "log2_fold_change": observation.get("log2_fold_change") if observation else None,
                    "padj": observation.get("padj") if observation else None,
                    "p_value": observation.get("p_value") if observation else None,
                    "identifier": observation.get("identifier") if observation else None,
                })
            return preview

        return {
            "analysis": clean_text(payload.get("analysis")),
            "provider": clean_text(payload.get("provider")),
            "label": clean_text(payload.get("label")) or None,
            "modality": clean_text(payload.get("modality")),
            "species": clean_text(payload.get("species")),
            "contrast": dict(payload.get("contrast") or {}),
            "result_scope": clean_text(payload.get("result_scope")),
            "input": dict(payload.get("input") or {}),
            "table": {
                key: value
                for key, value in dict(payload.get("table") or {}).items()
                if key in {"column_map", "row_count", "header"}
            },
            "effect_definition": dict((payload.get("input") or {}).get("effect_definition") or payload.get("effect_definition") or {}),
            "significance": dict(payload.get("significance") or {}),
            "background": dict(payload.get("background") or {}),
            "mapping_summary": {
                "identifier_count": len(mapping_rows),
                "status_counts": mapping_counts,
            },
            "signal_counts": {
                "upregulated": len((payload.get("significant_gene_sets") or {}).get("upregulated") or []),
                "downregulated": len((payload.get("significant_gene_sets") or {}).get("downregulated") or []),
                "all_significant": len((payload.get("significant_gene_sets") or {}).get("all_significant") or []),
            },
            "signal_preview": {
                "upregulated": strongest("upregulated"),
                "downregulated": strongest("downregulated"),
            },
            "enrichment": {
                direction: {
                    "not_run": bool((branch or {}).get("not_run")),
                    "reason": clean_text((branch or {}).get("reason")) or None,
                    "top_terms": self._top_enrichment_terms(branch or {}),
                }
                for direction, branch in (payload.get("enrichment") or {}).items()
            },
            "ranked_gsea": {
                "not_run": bool((payload.get("ranked_gsea") or {}).get("not_run")),
                "reason": clean_text((payload.get("ranked_gsea") or {}).get("reason")) or None,
                "top_terms": self._top_enrichment_terms(payload.get("ranked_gsea") or {}),
            },
            "followup_analysis": dict(payload.get("followup_analysis") or {}),
            "derived_inputs": dict(payload.get("derived_inputs") or {}),
            "agent_payload_mode": "compact",
        }

    @staticmethod
    def _compact_gene_level_integration_payload(payload: dict[str, Any]) -> dict[str, Any]:
        rows = [row for row in payload.get("gene_evidence_matrix") or [] if isinstance(row, dict)]
        state_counts: dict[str, int] = {}
        significance_counts: dict[str, int] = {}
        examples: dict[str, list[str]] = {}
        for row in rows:
            state = clean_text(row.get("integration_state")) or "unknown"
            state_counts[state] = state_counts.get(state, 0) + 1
            significance_state = clean_text(row.get("cross_modal_significance_status")) or "unknown"
            significance_counts[significance_state] = significance_counts.get(significance_state, 0) + 1
            examples.setdefault(state, [])
            if len(examples[state]) < 12 and clean_text(row.get("gene")):
                examples[state].append(clean_text(row.get("gene")))
        pathway_rows = [row for row in payload.get("pathway_convergence") or [] if isinstance(row, dict)]
        return {
            "analysis": clean_text(payload.get("analysis")),
            "provider": clean_text(payload.get("provider")),
            "species": clean_text(payload.get("species")),
            "inputs": list(payload.get("inputs") or []),
            "direction_comparable": bool(payload.get("direction_comparable")),
            "manifest_path": clean_text(payload.get("manifest_path")),
            "gene_evidence_summary": {
                "row_count": len(rows),
                "integration_state_counts": state_counts,
                "cross_modal_significance_status_counts": significance_counts,
                "examples_by_state": examples,
            },
            "pathway_assessment": list(payload.get("pathway_assessment") or []),
            "pathway_convergence": pathway_rows[:30],
            "pathway_convergence_count": len(pathway_rows),
            "agent_payload_mode": "compact",
        }

    def _map_differential_identifiers(
        self,
        identifiers: list[str],
        *,
        species: str,
        identifier_mapping: dict[str, list[str]] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Resolve identifiers conservatively; never turn an ambiguous feature into a gene."""
        supplied = identifier_mapping or {}
        out: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        pending: list[str] = []
        for identifier in unique_preserve_order(identifiers):
            mapped = unique_preserve_order(normalize_gene_token(value, species=species) for value in supplied.get(identifier, []) if normalize_gene_token(value, species=species))
            if len(mapped) == 1:
                out[identifier] = {"status": "mapped", "canonical_gene": mapped[0], "source": "supplied_mapping", "candidates": mapped}
                continue
            if len(mapped) > 1:
                out[identifier] = {"status": "ambiguous", "canonical_gene": None, "source": "supplied_mapping", "candidates": mapped}
                continue
            pending.append(identifier)

        local_matches: dict[str, Any] = {}
        resolver = getattr(self.reference_catalog, "resolve_genes", None)
        if pending and callable(resolver):
            try:
                local_matches = dict(resolver(pending, species=species) or {})
            except ProviderError as exc:
                warnings.append(f"Local batch identifier mapping was unavailable: {exc}")

        unresolved = [identifier for identifier in pending if identifier not in local_matches]
        remote_matches: dict[str, Any] = {}
        # A full differential table can contain tens of thousands of rows.  The
        # local reference catalog is the scalable resolver; keep a missing local
        # catalog from turning into thousands of remote requests.
        remote_limit = 1_000
        remote_identifiers = unresolved[:remote_limit]
        if remote_identifiers:
            remote_resolver = getattr(self.mygene, "resolve_genes", None)
            if callable(remote_resolver):
                try:
                    remote_matches = dict(remote_resolver(remote_identifiers, species=species) or {})
                except ProviderError as exc:
                    warnings.append(f"Batch fallback identifier mapping was unavailable: {exc}")
            else:
                warnings.append("Batch fallback identifier mapping is unavailable; unresolved identifiers were retained as unmapped.")
        if len(unresolved) > remote_limit:
            warnings.append(
                f"Skipped remote fallback for {len(unresolved) - remote_limit} unresolved identifiers after local batch mapping; "
                "they remain explicitly unmapped rather than triggering one remote request per row."
            )

        for identifier in pending:
            resolved = local_matches.get(identifier) or remote_matches.get(identifier)
            candidates = unique_preserve_order(
                normalize_gene_token(item.get("symbol"), species=species)
                for item in ((getattr(resolved, "payload", {}) or {}).get("matches", []) if resolved else [])
                if normalize_gene_token(item.get("symbol"), species=species)
            )
            source = clean_text(getattr(resolved, "provider", ""))
            if len(candidates) == 1:
                out[identifier] = {"status": "mapped", "canonical_gene": candidates[0], "source": source or "normalization", "candidates": candidates}
            elif len(candidates) > 1:
                out[identifier] = {"status": "ambiguous", "canonical_gene": None, "source": source or "normalization", "candidates": candidates}
            else:
                out[identifier] = {"status": "unmapped", "canonical_gene": None, "source": source or None, "candidates": []}
        return out, warnings

    def _load_differential_identifier_mapping(
        self,
        *,
        path: str = "",
        text: str = "",
        identifier_column: str = "",
        gene_column: str = "",
    ) -> dict[str, list[str]]:
        if not clean_text(path) and not clean_text(text):
            return {}
        loaded = load_table(path=self._resolve_input_file_selector(path, role="identifier_mapping_path"), text=text, dtype=str, keep_default_na=False)
        frame = loaded.frame
        if frame.empty:
            return {}
        columns = [str(column) for column in frame.columns]
        normalized = {self._column_token(column): column for column in columns}
        query = clean_text(identifier_column) or next((normalized[token] for token in ("identifier", "feature", "protein", "accession", "uniprot", "id") if token in normalized), "")
        target = clean_text(gene_column) or next((normalized[token] for token in ("gene", "genesymbol", "symbol", "hgncsymbol") if token in normalized), "")
        if query not in columns or target not in columns:
            raise ProviderError("Identifier mapping tables require explicit identifier and gene columns when they cannot be detected unambiguously.")
        mapping: dict[str, list[str]] = {}
        for row in table_rows_as_dicts(frame):
            identifier = clean_text(row.get(query))
            gene = clean_text(row.get(target))
            if identifier and gene:
                mapping.setdefault(identifier, []).append(gene)
        return mapping

    def analyze_differential_result(
        self,
        *,
        modality: str,
        positive_condition: str,
        reference_condition: str,
        result_scope: str,
        result_path: str = "",
        table_text: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        species: str | None = None,
        label: str = "",
        identifier_column: str = "",
        effect_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        stat_column: str = "",
        significance_mode: str = "padj",
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        log2_fold_change_threshold: float = 0.0,
        identifier_mapping: dict[str, list[str]] | None = None,
        identifier_mapping_path: str = "",
        identifier_mapping_text: str = "",
        mapping_identifier_column: str = "",
        mapping_gene_column: str = "",
        background_genes: list[str] | None = None,
        include_enrichment: bool = True,
        include_ranked_gsea: bool = True,
        effect_definition: dict[str, Any] | None = None,
        response_detail: str = "full",
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        normalized_modality = clean_text(modality).lower()
        if normalized_modality not in {"transcriptomics", "proteomics"}:
            raise ProviderError("modality must be transcriptomics or proteomics.")
        if not clean_text(positive_condition) or not clean_text(reference_condition):
            raise ProviderError("positive_condition and reference_condition are required; GLESKit will not infer contrast orientation.")
        if result_scope not in {"full_results", "filtered_results"}:
            raise ProviderError("result_scope must be full_results or filtered_results.")
        effective_species = self._effective_species(species)
        parsed = self.differential_result_backend.analyze(
            result_path=self._resolve_input_file_selector(result_path, role="result_path"), table_text=table_text,
            sheet_name=sheet_name, header_row=header_row, skip_rows=skip_rows,
            identifier_column=identifier_column, effect_column=effect_column, padj_column=padj_column,
            p_value_column=p_value_column, stat_column=stat_column, significance_mode=significance_mode,
            padj_threshold=padj_threshold, p_value_threshold=p_value_threshold,
            log2_fold_change_threshold=log2_fold_change_threshold,
            effect_definition=effect_definition,
            positive_condition=positive_condition,
            reference_condition=reference_condition,
        )
        supplied_mapping = dict(identifier_mapping or {})
        supplied_mapping.update(self._load_differential_identifier_mapping(path=identifier_mapping_path, text=identifier_mapping_text, identifier_column=mapping_identifier_column, gene_column=mapping_gene_column))
        mappings, mapping_warnings = self._map_differential_identifiers(
            [row["identifier"] for row in parsed["rows"]],
            species=effective_species,
            identifier_mapping=supplied_mapping,
        )
        by_gene: dict[str, list[dict[str, Any]]] = {}
        for row in parsed["rows"]:
            mapping = mappings[row["identifier"]]
            row["mapping"] = mapping
            gene = mapping.get("canonical_gene")
            if gene:
                by_gene.setdefault(str(gene), []).append(row)
        gene_rows: list[dict[str, Any]] = []
        up: list[str] = []
        down: list[str] = []
        ranked: list[dict[str, Any]] = []
        for gene, observations in sorted(by_gene.items()):
            significant = [item for item in observations if item.get("significant")]
            directions = {"upregulated" if float(item["log2_fold_change"]) > 0 else "downregulated" for item in significant if float(item["log2_fold_change"]) != 0}
            conflict = len(directions) > 1
            direction = next(iter(directions)) if len(directions) == 1 else None
            record = {"gene": gene, "observations": observations, "significant_observation_count": len(significant), "direction": direction, "within_modality_conflict": conflict}
            gene_rows.append(record)
            if direction == "upregulated": up.append(gene)
            elif direction == "downregulated": down.append(gene)
            if len(observations) == 1 and not conflict:
                ranked.append({"gene": gene, "score": observations[0]["log2_fold_change"], "identifier": observations[0]["identifier"]})
        warnings: list[str] = list(mapping_warnings)
        if result_scope == "filtered_results":
            warnings.append("Input is declared filtered_results; no tested/quantified background is implied and over-representation analysis is qualified.")
        inferred_background = unique_preserve_order(by_gene) if result_scope == "full_results" else []
        effective_background = normalize_gene_list(background_genes or inferred_background, species=effective_species)
        if not include_enrichment:
            enrichment = {
                "upregulated": {"not_run": True, "reason": "Enrichment was explicitly deferred; use the returned tested_genes_path with enrich_gene_set.", "terms": []},
                "downregulated": {"not_run": True, "reason": "Enrichment was explicitly deferred; use the returned tested_genes_path with enrich_gene_set.", "terms": []},
            }
        elif result_scope == "filtered_results" and not effective_background:
            enrichment = {
                "upregulated": {"not_run": True, "reason": "A filtered result table requires an explicit tested/quantified background for enrichment.", "terms": []},
                "downregulated": {"not_run": True, "reason": "A filtered result table requires an explicit tested/quantified background for enrichment.", "terms": []},
            }
        else:
            enrichment = {
                "upregulated": self._safe_enrichment_context(up, species=effective_species, label=f"{label or normalized_modality} upregulated", warnings=warnings, background_genes=effective_background),
                "downregulated": self._safe_enrichment_context(down, species=effective_species, label=f"{label or normalized_modality} downregulated", warnings=warnings, background_genes=effective_background),
            }
        if not include_ranked_gsea:
            ranked_gsea = {
                "not_run": True,
                "reason": "Ranked GSEA was explicitly deferred; use the returned ranked_gene_scores_path with ranked_gene_set_gsea.",
                "terms": [],
            }
        elif result_scope == "filtered_results":
            ranked_gsea = {
                "not_run": True,
                "reason": "Ranked GSEA requires a complete ranked result table; a filtered result table does not provide a valid ranked universe.",
                "terms": [],
            }
        else:
            ranked_gsea = self._safe_ranked_gsea_context(
                ranked,
                species=effective_species,
                label=label or normalized_modality,
                warnings=warnings,
            )
        payload = {
            "provider": "gleskit_local_differential", "analysis": "differential_result", "modality": normalized_modality,
            "species": effective_species,
            "contrast": {"positive_condition": clean_text(positive_condition), "reference_condition": clean_text(reference_condition)},
            "result_scope": result_scope, "label": clean_text(label) or None, "input": parsed["input"], "table": parsed["table"],
            "effect_definition": parsed.get("effect_definition") or {},
            "significance": parsed["significance"], "mapping_audit": [{"identifier": identifier, **mapping} for identifier, mapping in mappings.items()], "feature_rows": parsed["rows"],
            "gene_evidence": gene_rows,
            "significant_gene_sets": {"upregulated": up, "downregulated": down, "all_significant": unique_preserve_order(up + down)},
            "ranked_gene_scores": ranked, "enrichment": enrichment, "ranked_gsea": ranked_gsea,
            "background": {"gene_count": len(effective_background), "source": "full_result_table" if result_scope == "full_results" and not background_genes else "caller_supplied" if background_genes else "unavailable"},
            "followup_analysis": {"enrichment_requested": bool(include_enrichment), "ranked_gsea_requested": bool(include_ranked_gsea)},
        }
        derived_root = self.settings.runtime_root / "analysis_handoffs" / "differential_results"
        derived_root.mkdir(parents=True, exist_ok=True)
        handoff_dir = derived_root / f"{self._slugify_path_segment(label or normalized_modality)}-{secrets.token_hex(4)}"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        all_genes_path = handoff_dir / "all_significant_genes.tsv"
        self._write_tsv(all_genes_path, ["gene"], [[gene] for gene in payload["significant_gene_sets"]["all_significant"]])
        upregulated_genes_path = handoff_dir / "upregulated_genes.tsv"
        self._write_tsv(upregulated_genes_path, ["gene"], [[gene] for gene in up])
        downregulated_genes_path = handoff_dir / "downregulated_genes.tsv"
        self._write_tsv(downregulated_genes_path, ["gene"], [[gene] for gene in down])
        tested_genes_path = handoff_dir / "tested_genes.tsv"
        self._write_tsv(tested_genes_path, ["gene"], [[gene] for gene in effective_background])
        ranked_gene_scores_path = handoff_dir / "ranked_gene_scores.tsv"
        self._write_tsv(
            ranked_gene_scores_path,
            ["gene", "score", "identifier"],
            [[row["gene"], row["score"], row["identifier"]] for row in ranked],
        )
        manifest_path = handoff_dir / "differential_result_manifest.json"
        payload["derived_inputs"] = {
            "handoff_root": str(handoff_dir),
            "manifest_path": str(manifest_path),
            "all_significant_genes_path": str(all_genes_path),
            "upregulated_genes_path": str(upregulated_genes_path),
            "downregulated_genes_path": str(downregulated_genes_path),
            "tested_genes_path": str(tested_genes_path),
            "ranked_gene_scores_path": str(ranked_gene_scores_path),
        }
        manifest_path.write_text(dump_json(payload), encoding="utf-8")
        summary = f"Analyzed {len(parsed['rows'])} {normalized_modality} feature row(s); {len(up)} upregulated and {len(down)} downregulated gene-level signal(s) were directionally usable."
        payload["summary"] = summary
        workspace = self._capture_to_session_or_active(
            session_id=save_to_session_id,
            kind="differential_result_analysis",
            title=clean_text(label) or f"{normalized_modality.title()} differential result",
            summary=summary,
            provider="gleskit_local_differential",
            payload=payload,
            provenance={"modality": normalized_modality, "contrast": payload["contrast"], "result_scope": result_scope, "manifest_path": str(manifest_path)},
        )
        detail = clean_text(response_detail).lower() or "compact"
        if detail not in {"compact", "full"}:
            raise ProviderError("response_detail must be 'compact' or 'full'.")
        structured_data = payload if detail == "full" else self._compact_differential_result_payload(payload)
        return {"summary": summary, "provider": "gleskit_local_differential", "structured_data": structured_data, "warnings": warnings + self._workspace_warnings(workspace), "workspace": workspace}

    def preflight_differential_result(
        self,
        *,
        result_path: str,
        modality: str,
        sheet_name: str = "",
        species: str | None = None,
        label: str = "",
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        """Inspect result-table candidates server-side without returning their rows.

        This is intentionally a routing manifest, not an analysis.  It reports
        structural evidence for complete/filtered candidates and only offers an
        automatic background recovery when a filtered identifier set is an
        exact subset of one compatible complete table.
        """
        normalized_modality = clean_text(modality).lower()
        if normalized_modality not in {"transcriptomics", "proteomics"}:
            raise ProviderError("modality must be transcriptomics or proteomics.")
        effective_species = self._effective_species(species)
        resolved_path = self._resolve_input_file_selector(result_path, role="result_path")
        if not resolved_path:
            raise ProviderError("preflight_differential_result requires result_path.")

        if is_excel_workbook(resolved_path):
            workbook = pd.ExcelFile(resolved_path, engine=excel_workbook_engine(resolved_path))
            try:
                available_sheets = list(workbook.sheet_names)
            finally:
                workbook.close()
        else:
            available_sheets = [""]
        requested_sheet = clean_text(sheet_name)
        if requested_sheet and requested_sheet not in available_sheets:
            raise ProviderError(
                f"Workbook sheet '{requested_sheet}' was not found. Available sheets: {', '.join(available_sheets) or 'none'}."
            )

        candidates: list[dict[str, Any]] = []
        for current_sheet in available_sheets:
            try:
                header_row = 1
                inspection_facts: dict[str, Any] = {}
                if current_sheet:
                    inspection_facts = self._inspect_workbook_sheet(
                        path=resolved_path,
                        sheet_name=current_sheet,
                        role_hint="differential_result",
                    )
                    header_row = int(inspection_facts.get("header_row") or 1)
                schema = self.differential_result_backend.inspect(
                    result_path=resolved_path,
                    sheet_name=current_sheet,
                    header_row=header_row,
                )
            except Exception as exc:
                candidates.append({
                    "sheet_name": current_sheet or None,
                    "status": "unreadable",
                    "reason": f"{type(exc).__name__}: {clean_text(exc)}",
                })
                continue
            if not schema.get("differential_result_candidate"):
                schema["scope_assessment"] = "not_a_differential_result_candidate"
            elif schema.get("abundance_columns"):
                schema["scope_assessment"] = "complete_result_candidate"
            else:
                schema["scope_assessment"] = "filtered_result_candidate"
            schema["inspection"] = {
                "detected_data_type": clean_text(inspection_facts.get("detected_data_type")) or None,
                "header_confidence": clean_text(inspection_facts.get("header_confidence")) or None,
                "shape": dict(inspection_facts.get("shape") or {}),
            }
            candidates.append(schema)

        selected = next(
            (
                item for item in candidates
                if clean_text(item.get("sheet_name")) == requested_sheet and item.get("status") != "unreadable"
            ),
            None,
        ) if requested_sheet else None
        viable = [item for item in candidates if item.get("differential_result_candidate")]
        if selected is None and len(viable) == 1:
            selected = viable[0]

        compatible_full_tables: list[dict[str, Any]] = []
        if selected and selected.get("scope_assessment") == "filtered_result_candidate":
            selected_identifier_column = (selected.get("identifier_candidates") or [""])[0]
            try:
                selected_loaded = load_table(
                    path=resolved_path,
                    sheet_name=clean_text(selected.get("sheet_name")),
                    header_row=int(selected.get("header_row") or 1),
                    dtype=str,
                    keep_default_na=False,
                )
                selected_ids = {
                    clean_text(value)
                    for value in selected_loaded.frame.get(selected_identifier_column, pd.Series(dtype=str)).tolist()
                    if clean_text(value)
                }
            except Exception:
                selected_ids = set()
            for candidate in viable:
                if candidate is selected or candidate.get("scope_assessment") != "complete_result_candidate":
                    continue
                candidate_identifier_column = (candidate.get("identifier_candidates") or [""])[0]
                try:
                    candidate_loaded = load_table(
                        path=resolved_path,
                        sheet_name=clean_text(candidate.get("sheet_name")),
                        header_row=int(candidate.get("header_row") or 1),
                        dtype=str,
                        keep_default_na=False,
                    )
                    candidate_ids = {
                        clean_text(value)
                        for value in candidate_loaded.frame.get(candidate_identifier_column, pd.Series(dtype=str)).tolist()
                        if clean_text(value)
                    }
                except Exception:
                    candidate_ids = set()
                shared_ids = selected_ids.intersection(candidate_ids)
                exact_subset = bool(selected_ids) and selected_ids.issubset(candidate_ids)
                selected_stat_types = {
                    kind
                    for kind, columns in (
                        ("padj", selected.get("padj_candidates") or []),
                        ("p_value", selected.get("p_value_candidates") or []),
                    )
                    if columns
                }
                candidate_stat_types = {
                    kind
                    for kind, columns in (
                        ("padj", candidate.get("padj_candidates") or []),
                        ("p_value", candidate.get("p_value_candidates") or []),
                    )
                    if columns
                }
                statistic_compatible = bool(selected_stat_types.intersection(candidate_stat_types))
                structural_recovery_candidate = exact_subset and statistic_compatible
                compatible_full_tables.append({
                    "sheet_name": candidate.get("sheet_name"),
                    "identifier_column": candidate_identifier_column,
                    "filtered_identifier_count": len(selected_ids),
                    "shared_identifier_count": len(shared_ids),
                    "complete_identifier_count": len(candidate_ids),
                    "exact_filtered_subset": exact_subset,
                    "statistic_compatible": statistic_compatible,
                    "structural_recovery_candidate": structural_recovery_candidate,
                    "effect_semantics_status": "requires_explicit_effect_definition",
                    "reason": (
                        "Every filtered identifier is present and a compatible significance statistic exists. "
                        "Use this complete table only after explicitly declaring effect/contrast semantics."
                        if structural_recovery_candidate else
                        "The candidate lacks an exact filtered-identifier subset or a compatible significance statistic, "
                        "so it cannot be silently used as the background."
                    ),
                })

        eligible = [item for item in compatible_full_tables if item.get("structural_recovery_candidate")]
        route = {
            "selected_sheet": selected.get("sheet_name") if selected else None,
            "selected_scope_assessment": selected.get("scope_assessment") if selected else "ambiguous",
            "selected_result_scope": (
                "full_results" if selected and selected.get("scope_assessment") == "complete_result_candidate"
                else "filtered_results" if selected and selected.get("scope_assessment") == "filtered_result_candidate"
                else None
            ),
            "recommended_complete_result_sheet": eligible[0].get("sheet_name") if len(eligible) == 1 else None,
            "recovered_background_sheet": eligible[0].get("sheet_name") if len(eligible) == 1 else None,
            "requires_scope_declaration": selected is None,
            "requires_effect_definition": bool(selected and (selected.get("ratio_candidates") or selected.get("effect_candidates"))),
            "requires_contrast_declaration": True,
        }
        manifest = {
            "analysis": "differential_result_preflight",
            "version": 1,
            "modality": normalized_modality,
            "species": effective_species,
            "input_path": resolved_path,
            "available_sheets": available_sheets,
            "candidates": candidates,
            "compatible_full_tables": compatible_full_tables,
            "route": route,
        }
        handoff_root = self.settings.runtime_root / "analysis_handoffs" / "differential_preflight"
        handoff_root.mkdir(parents=True, exist_ok=True)
        manifest_path = handoff_root / f"{self._slugify_path_segment(label or normalized_modality)}-{secrets.token_hex(4)}.json"
        manifest["manifest_path"] = str(manifest_path)
        manifest_path.write_text(dump_json(manifest), encoding="utf-8")
        summary = (
            f"Preflight inspected {len(candidates)} table candidate(s) for {normalized_modality}. "
            f"Selected route: {route['selected_result_scope'] or 'requires explicit selection'}."
        )
        compact = {
            "analysis": manifest["analysis"],
            "modality": normalized_modality,
            "species": effective_species,
            "manifest_path": str(manifest_path),
            "available_sheets": available_sheets,
            "route": route,
            "candidates": [
                {
                    key: item.get(key)
                    for key in (
                        "sheet_name", "status", "role", "differential_result_candidate", "scope_assessment",
                        "row_count", "column_count", "identifier_candidates", "effect_candidates", "ratio_candidates",
                        "padj_candidates", "p_value_candidates", "abundance_columns", "scope_evidence", "reason",
                    )
                }
                for item in candidates
            ],
            "compatible_full_tables": compatible_full_tables,
        }
        workspace = self._capture_to_session_or_active(
            session_id=save_to_session_id,
            kind="differential_result_preflight",
            title=clean_text(label) or f"{normalized_modality.title()} differential-result preflight",
            summary=summary,
            provider="gleskit_local_differential",
            payload=manifest,
            provenance={"modality": normalized_modality, "input_path": resolved_path},
        )
        return {
            "summary": summary,
            "provider": "gleskit_local_differential",
            "structured_data": compact,
            "warnings": self._workspace_warnings(workspace),
            "workspace": workspace,
        }

    def integrate_gene_level_results(
        self,
        *,
        results: list[dict[str, Any]],
        label: str = "",
        pathway_results: list[dict[str, Any]] | None = None,
        response_detail: str = "full",
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        payloads: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            payload = dict(item.get("structured_data") or item)
            manifest_path = clean_text(item.get("manifest_path") or (payload.get("derived_inputs") or {}).get("manifest_path"))
            if manifest_path:
                resolved = self._resolve_input_file_selector(manifest_path, role="manifest_path")
                try:
                    loaded = json.loads(Path(resolved).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ProviderError(f"Unable to load differential-result manifest: {exc}") from exc
                if isinstance(loaded, dict):
                    payload = loaded
            payloads.append(payload)
        if len(payloads) < 2:
            raise ProviderError("integrate_gene_level_results requires at least two canonical analysis payloads.")
        if any(item.get("analysis") != "differential_result" for item in payloads):
            raise ProviderError("integrate_gene_level_results accepts only analyze_differential_result outputs.")
        species = {clean_text(item.get("species")) for item in payloads}
        if len(species) != 1:
            raise ProviderError("All integrated results must use the same species.")
        orientations = {(clean_text((item.get("contrast") or {}).get("positive_condition")), clean_text((item.get("contrast") or {}).get("reference_condition"))) for item in payloads}
        direction_comparable = len(orientations) == 1
        by_gene: dict[str, dict[str, Any]] = {}
        modality_labels = []
        for index, item in enumerate(payloads, start=1):
            modality_label = clean_text(item.get("label")) or f"{clean_text(item.get('modality'))}_{index}"
            modality_labels.append(modality_label)
            for gene_row in item.get("gene_evidence") or []:
                gene = clean_text(gene_row.get("gene"))
                if gene:
                    by_gene.setdefault(gene, {"gene": gene, "modalities": {}})["modalities"][modality_label] = gene_row
        matrix = []
        for gene, record in sorted(by_gene.items()):
            entries = list(record["modalities"].values())
            directions = {clean_text(entry.get("direction")) for entry in entries if clean_text(entry.get("direction"))}
            significant_labels = [
                modality_label
                for modality_label, entry in record["modalities"].items()
                if int(entry.get("significant_observation_count") or 0) > 0
            ]
            significant_directions = {
                clean_text(entry.get("direction"))
                for entry in entries
                if int(entry.get("significant_observation_count") or 0) > 0 and clean_text(entry.get("direction"))
            }
            conflicts = any(bool(entry.get("within_modality_conflict")) for entry in entries)
            if len(entries) == 1:
                state = "modality_specific"
            elif conflicts:
                state = "within_modality_conflicted"
            elif not direction_comparable:
                state = "orientation_not_comparable"
            elif len(directions) == 1:
                state = "shared_concordant"
            else:
                state = "shared_discordant"
            record["integration_state"] = state
            if not significant_labels:
                record["cross_modal_significance_status"] = "no_significant_signal"
            elif len(significant_labels) == 1:
                record["cross_modal_significance_status"] = "single_modality_significant"
            elif not direction_comparable:
                record["cross_modal_significance_status"] = "significant_orientation_not_comparable"
            elif len(significant_directions) == 1:
                record["cross_modal_significance_status"] = "cross_modality_significant_concordant"
            else:
                record["cross_modal_significance_status"] = "cross_modality_significant_discordant"
            record["significant_modalities"] = significant_labels
            matrix.append(record)
        term_index: dict[tuple[str, str, str], list[str]] = {}
        pathway_assessment: list[dict[str, Any]] = []
        for item, modality_label in zip(payloads, modality_labels, strict=False):
            for direction, branch in (item.get("enrichment") or {}).items():
                branch_terms = [term for term in (branch or {}).get("terms") or [] if isinstance(term, dict)]
                if not branch_terms:
                    branch_terms = [term for term in (branch or {}).get("top_terms") or [] if isinstance(term, dict)]
                pathway_assessment.append({
                    "modality_label": modality_label,
                    "direction": direction,
                    "term_count": len(branch_terms),
                    "source": "canonical_analysis",
                    "status": "available" if branch_terms else "not_available",
                    "reason": clean_text((branch or {}).get("reason")) or None,
                })
                for term in branch_terms:
                    key = (
                        clean_text(term.get("source")),
                        clean_text(term.get("term_id") or term.get("term_name") or term.get("description")),
                        clean_text(direction),
                    )
                    if all(key):
                        term_index.setdefault(key, []).append(modality_label)
        for external in pathway_results or []:
            if not isinstance(external, dict):
                continue
            modality_label = clean_text(external.get("label") or external.get("modality_label"))
            direction = clean_text(external.get("direction"))
            source_payload = dict(external.get("structured_data") or external.get("result") or external)
            terms = [term for term in source_payload.get("terms") or [] if isinstance(term, dict)]
            if not terms:
                terms = [term for term in source_payload.get("top_terms") or [] if isinstance(term, dict)]
            if not modality_label or not direction:
                pathway_assessment.append({
                    "modality_label": modality_label or None,
                    "direction": direction or None,
                    "term_count": len(terms),
                    "source": "external_pathway_result",
                    "status": "not_comparable",
                    "reason": "External pathway results require explicit modality label and direction.",
                })
                continue
            pathway_assessment.append({
                "modality_label": modality_label,
                "direction": direction,
                "term_count": len(terms),
                "source": "external_pathway_result",
                "status": "available" if terms else "not_available",
                "reason": clean_text(source_payload.get("reason")) or None,
            })
            for term in terms:
                key = (
                    clean_text(term.get("source")),
                    clean_text(term.get("term_id") or term.get("term_name") or term.get("description")),
                    direction,
                )
                if all(key):
                    term_index.setdefault(key, []).append(modality_label)
        pathway_convergence = [{"source": key[0], "term_id": key[1], "direction": key[2], "modalities": labels, "shared": len(labels) > 1} for key, labels in sorted(term_index.items())]
        structured = {"provider": "gleskit_local_integration", "analysis": "gene_level_integration", "species": next(iter(species)), "inputs": [{"label": label, "modality": item.get("modality"), "contrast": item.get("contrast"), "result_scope": item.get("result_scope")} for item, label in zip(payloads, modality_labels, strict=False)], "direction_comparable": direction_comparable, "gene_evidence_matrix": matrix, "pathway_convergence": pathway_convergence, "pathway_assessment": pathway_assessment}
        handoff_root = self.settings.runtime_root / "analysis_handoffs" / "gene_level_integration"
        handoff_root.mkdir(parents=True, exist_ok=True)
        manifest_path = handoff_root / f"{self._slugify_path_segment(label or 'integration')}-{secrets.token_hex(4)}.json"
        structured["manifest_path"] = str(manifest_path)
        manifest_path.write_text(dump_json(structured), encoding="utf-8")
        summary = f"Integrated {len(payloads)} modality result(s) into {len(matrix)} gene-level evidence row(s)."
        structured["summary"] = summary
        workspace = self._capture_to_session_or_active(kind="gene_level_integration", title=clean_text(label) or "Gene-level differential-result integration", summary=summary, provider="gleskit_local_integration", payload=structured, provenance={"direction_comparable": direction_comparable, "modality_count": len(payloads), "manifest_path": str(manifest_path)}, session_id=save_to_session_id)
        detail = clean_text(response_detail).lower() or "compact"
        if detail not in {"compact", "full"}:
            raise ProviderError("response_detail must be 'compact' or 'full'.")
        compact = self._compact_gene_level_integration_payload(structured)
        return {"summary": summary, "provider": "gleskit_local_integration", "structured_data": structured if detail == "full" else compact, "warnings": self._workspace_warnings(workspace), "workspace": workspace}

    def build_biomarker_candidate_dossiers(
        self,
        *,
        results: list[dict[str, Any]],
        genes: list[str],
        literature_evidence: list[dict[str, Any]] | None = None,
        pathway_results: list[dict[str, Any]] | None = None,
        label: str = "",
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        """Assemble transparent dossiers for explicitly selected discovery candidates.

        Candidate selection belongs to the investigator/agent after inspecting
        the evidence. This method never computes a weighted biomarker score or
        upgrades an observation to clinical validation.
        """
        requested_genes = unique_preserve_order(clean_text(gene).upper() for gene in genes if clean_text(gene))
        if not requested_genes:
            raise ProviderError("build_biomarker_candidate_dossiers requires at least one explicitly selected gene.")
        integrated = self.integrate_gene_level_results(
            results=results,
            label=label or "Candidate dossier integration",
            pathway_results=pathway_results,
            response_detail="full",
        )["structured_data"]
        matrix_by_gene = {
            clean_text(row.get("gene")).upper(): row
            for row in integrated.get("gene_evidence_matrix") or []
            if isinstance(row, dict) and clean_text(row.get("gene"))
        }

        normalized_literature = [row for row in literature_evidence or [] if isinstance(row, dict)]
        dossiers: list[dict[str, Any]] = []
        for gene in requested_genes:
            matrix_row = matrix_by_gene.get(gene)
            if matrix_row is None:
                dossiers.append({
                    "gene": gene,
                    "evidence_status": "not_present_in_integrated_results",
                    "clinical_validation_status": "not_assessed",
                    "caveats": ["The selected gene was not found among mapped canonical differential-result evidence."],
                })
                continue
            modalities: list[dict[str, Any]] = []
            for modality_label, evidence in sorted((matrix_row.get("modalities") or {}).items()):
                observations = [row for row in evidence.get("observations") or [] if isinstance(row, dict)]
                significant = [row for row in observations if row.get("significant")]
                source_rows = significant or observations
                primary = source_rows[0] if source_rows else {}
                mapping = dict(primary.get("mapping") or {})
                modalities.append({
                    "label": modality_label,
                    "direction": evidence.get("direction"),
                    "within_modality_conflict": bool(evidence.get("within_modality_conflict")),
                    "significant_observation_count": evidence.get("significant_observation_count"),
                    "effect": primary.get("log2_fold_change"),
                    "padj": primary.get("padj"),
                    "p_value": primary.get("p_value"),
                    "identifier": primary.get("identifier"),
                    "mapping_status": mapping.get("status"),
                    "mapping_source": mapping.get("source"),
                })
            literature_hits = []
            for record in normalized_literature:
                record_genes = {
                    clean_text(item).upper()
                    for item in (record.get("genes") or record.get("gene_symbols") or [])
                    if clean_text(item)
                }
                if gene in record_genes:
                    literature_hits.append({
                        "title": clean_text(record.get("title")) or None,
                        "url": clean_text(record.get("url") or record.get("link")) or None,
                        "evidence_type": clean_text(record.get("evidence_type")) or "literature",
                        "comparator": clean_text(record.get("comparator")) or None,
                    })
            caveats = [
                "Discovery evidence only; differential-result evidence does not establish diagnostic performance or clinical validity."
            ]
            if matrix_row.get("integration_state") == "modality_specific":
                caveats.append("Observed in one measured modality only; it is not cross-modal corroboration.")
            elif matrix_row.get("integration_state") == "shared_discordant":
                caveats.append("Observed in multiple modalities with discordant directions.")
            elif matrix_row.get("integration_state") == "orientation_not_comparable":
                caveats.append("Modalities used non-comparable contrast orientations.")
            significance_status = clean_text(matrix_row.get("cross_modal_significance_status"))
            if significance_status == "single_modality_significant":
                caveats.append("Only one modality met its declared significance criterion; the other measurement is not independent cross-modal corroboration.")
            elif significance_status == "no_significant_signal":
                caveats.append("Neither modality met its declared significance criterion for this gene.")
            dossiers.append({
                "gene": gene,
                "evidence_status": matrix_row.get("integration_state"),
                "cross_modal_significance_status": significance_status or None,
                "significant_modalities": list(matrix_row.get("significant_modalities") or []),
                "modalities": modalities,
                "literature_evidence": literature_hits,
                "pathway_context_status": "provided_to_integration" if pathway_results else "not_supplied",
                "clinical_validation_status": "not_assessed",
                "caveats": caveats,
            })
        payload = {
            "analysis": "biomarker_candidate_dossiers",
            "provider": "gleskit_local_integration",
            "label": clean_text(label) or None,
            "selected_gene_count": len(requested_genes),
            "dossiers": dossiers,
            "selection_policy": "explicit_genes_only_no_weighted_score",
            "integration_manifest_path": integrated.get("manifest_path"),
        }
        handoff_root = self.settings.runtime_root / "analysis_handoffs" / "biomarker_candidate_dossiers"
        handoff_root.mkdir(parents=True, exist_ok=True)
        manifest_path = handoff_root / f"{self._slugify_path_segment(label or 'candidate-dossiers')}-{secrets.token_hex(4)}.json"
        payload["manifest_path"] = str(manifest_path)
        manifest_path.write_text(dump_json(payload), encoding="utf-8")
        summary = f"Built transparent discovery-evidence dossiers for {len(dossiers)} selected gene(s)."
        payload["summary"] = summary
        workspace = self._capture_to_session_or_active(
            session_id=save_to_session_id,
            kind="biomarker_candidate_dossiers",
            title=clean_text(label) or "Biomarker candidate dossiers",
            summary=summary,
            provider="gleskit_local_integration",
            payload=payload,
            provenance={"selected_gene_count": len(requested_genes), "manifest_path": str(manifest_path)},
        )
        compact = {
            "analysis": payload["analysis"],
            "manifest_path": str(manifest_path),
            "selected_gene_count": len(dossiers),
            "dossiers": dossiers,
            "selection_policy": payload["selection_policy"],
        }
        return {
            "summary": summary,
            "provider": "gleskit_local_integration",
            "structured_data": compact,
            "warnings": self._workspace_warnings(workspace),
            "workspace": workspace,
        }

    def inspect_tabular_data(
        self,
        *,
        path: str,
        sheet_name: str = "",
        sheet_names: list[str] | None = None,
        role_hint: str = "",
        secondary_path: str = "",
        secondary_sheet_name: str = "",
        secondary_role_hint: str = "",
    ) -> dict[str, Any]:
        resolved_path = self._resolve_input_file_selector(path, role="path")
        if Path(resolved_path).suffix.casefold() == ".rds":
            structured_data = {
                "path": resolved_path,
                "detected_data_type": "deseq2_results",
                "file_format": "rds",
                "row_identifier_column": "",
                "candidate_sample_column": "",
                "candidate_group_columns": [],
                "candidate_covariates": [],
                "candidate_group_levels": {},
                "candidate_sample_preview": [],
                "matrix_sample_preview": [],
                "candidate_sample_group_map": {},
                "candidate_gene_id_columns": [],
                "header_row": None,
                "header_selection_score": None,
                "header_confidence": "high",
                "sheet_roles": [],
                "matrix_orientation": "",
                "candidate_analysis_families": ["analyze_rnaseq_result"],
                "ambiguities": [],
                "next_step_hints": [
                    "This is an RDS differential-expression result. Do not parse it as text.",
                    "Call analyze_rnaseq_result with result_path set to this RDS file; GLESKit will materialize a reusable TSV internally.",
                ],
                "provenance_notes": [f"source_path={resolved_path}", "file_format=rds"],
                "validation": {"status": "ok"},
                "warnings": [],
                "related_inputs": [],
            }
            summary = "Recognized an RDS differential-expression result; route it directly to analyze_rnaseq_result."
            workspace_info = self._capture_to_active_session(
                kind="tabular_inspection",
                title=Path(resolved_path).name or "RDS result",
                summary=summary,
                provider="gleskit_inspection",
                payload=structured_data,
                provenance={"path": resolved_path, "role_hint": clean_text(role_hint) or None},
            )
            warnings = self._workspace_warnings(workspace_info)
            return {
                "summary": summary,
                "provider": "gleskit_inspection",
                "structured_data": structured_data,
                "warnings": warnings,
                "workspace": workspace_info,
            }
        warnings: list[str] = []
        workbook_mode = is_excel_workbook(resolved_path)
        sheet_roles: list[dict[str, Any]] = []
        primary_summary: dict[str, Any]

        if workbook_mode:
            try:
                workbook = pd.ExcelFile(resolved_path, engine=excel_workbook_engine(resolved_path))
            except ImportError as exc:
                raise ProviderError(
                    "Excel inspection requires 'openpyxl' for .xlsx files or 'xlrd' for .xls files."
                ) from exc
            try:
                available_sheets = list(workbook.sheet_names)
                requested_sheets = [clean_text(item) for item in (sheet_names or []) if clean_text(item)]
                if clean_text(sheet_name):
                    requested_sheets = [clean_text(sheet_name)]
                selected_sheets = requested_sheets or available_sheets
                missing_sheets = [item for item in selected_sheets if item not in available_sheets]
                if missing_sheets:
                    raise ProviderError(
                        f"Workbook sheet(s) not found: {', '.join(missing_sheets)}. "
                        f"Available sheets: {', '.join(available_sheets) or 'none'}."
                    )
                for current_sheet in selected_sheets:
                    payload = self._inspect_workbook_sheet(
                        path=resolved_path,
                        sheet_name=current_sheet,
                        role_hint=role_hint,
                    )
                    payload["sheet_name"] = current_sheet
                    sheet_roles.append(payload)
            finally:
                workbook.close()
            primary_summary = max(sheet_roles, key=self._inspection_result_score) if sheet_roles else {
                "detected_data_type": "generic_table",
                "role": "table",
                "candidate_sample_column": "",
                "candidate_group_columns": [],
                "candidate_covariates": [],
                "candidate_gene_id_columns": [],
                "matrix_orientation": "",
                "candidate_analysis_families": ["run_expression_statistics"],
                "ambiguities": ["No readable workbook sheets were inspected."],
                "next_step_hints": [],
                "provenance_notes": [f"source_path={resolved_path}"],
                "warnings": [],
            }
            raw_count_sheets = [row for row in sheet_roles if self._looks_like_sheet_role(row, "raw_counts")]
            metadata_sheets = [row for row in sheet_roles if self._looks_like_sheet_role(row, "metadata")]
            if len(raw_count_sheets) > 1:
                primary_summary["ambiguities"].append(
                    "Multiple workbook sheets look like counts or matrix inputs: "
                    + ", ".join(clean_text(row.get("sheet_name")) for row in raw_count_sheets[:5])
                )
            if len(metadata_sheets) > 1:
                primary_summary["ambiguities"].append(
                    "Multiple workbook sheets look like metadata tables: "
                    + ", ".join(clean_text(row.get("sheet_name")) for row in metadata_sheets[:5])
                )
            if raw_count_sheets and metadata_sheets:
                # A mixed workbook is primarily a DESeq2/counts workflow.
                # Keep the matrix sheet as the top-level result while merging
                # metadata facts into its next-step contract below.
                primary_summary = max(raw_count_sheets, key=self._inspection_result_score)
                workbook_layout = self._infer_rnaseq_workbook_layout(path=resolved_path)
                primary_summary["candidate_analysis_families"] = list(
                    dict.fromkeys(
                        [
                            "inspect_tabular_data",
                            "prepare_rnaseq_inputs",
                            "analyze_deseq2_counts",
                            *list(primary_summary.get("candidate_analysis_families") or []),
                        ]
                    )
                )
                primary_summary["next_step_hints"] = list(
                    dict.fromkeys(
                        [
                            (
                                "This workbook appears to contain both matrix/counts and metadata sheets. "
                                "Inspect the roles, then use prepare_rnaseq_inputs before DESeq2 when the question is about contrasts or DEG counts."
                            ),
                            *list(primary_summary.get("next_step_hints") or []),
                        ]
                    )
                )
                metadata_hint = metadata_sheets[0]
                # The primary sheet is a counts matrix, so metadata facts are
                # more useful than its identifier column for the next DESeq2
                # preparation step.
                primary_summary["candidate_sample_column"] = clean_text(
                    workbook_layout.get("sample_key_column")
                    or metadata_hint.get("candidate_sample_column")
                )
                primary_summary["candidate_group_columns"] = list(
                    workbook_layout.get("candidate_condition_columns")
                    or metadata_hint.get("candidate_group_columns")
                    or []
                )
                primary_summary["candidate_covariates"] = list(metadata_hint.get("candidate_covariates") or [])
                if workbook_layout:
                    primary_summary["recommended_arguments"] = dict(workbook_layout)
                    primary_summary["next_step_hints"] = list(
                        dict.fromkeys(
                            [
                                "Copy the returned counts/metadata sheet names, header rows, gene column, sample key, and count transform into prepare_rnaseq_inputs.",
                                "Choose the condition and optional sample filter from the returned candidate columns using the exact variable named in the question.",
                                *list(primary_summary.get("next_step_hints") or []),
                            ]
                        )
                    )
            primary_summary["provenance_notes"].append(f"inspected_sheet_count={len(sheet_roles)}")
            for row in sheet_roles:
                primary_summary["provenance_notes"].append(
                    f"sheet_role:{clean_text(row.get('sheet_name'))}={clean_text(row.get('detected_data_type'))}"
                )
        else:
            inspection = inspect_tabular_file(
                Path(resolved_path),
                role_hint=role_hint,
                provenance={},
            )
            primary_summary = self._summarize_inspection_result(
                inspection,
                source_path=resolved_path,
            )
            # Delimited files have one unambiguous parser header row, but still
            # need the same confidence contract as workbook inspections.
            primary_summary["header_row"] = 1
            primary_summary["header_selection_score"] = int(self._inspection_result_score(primary_summary))
            header_score = int(primary_summary["header_selection_score"])
            primary_summary["header_confidence"] = (
                "high" if header_score >= 60 else "medium" if header_score >= 35 else "low"
            )
            if primary_summary["header_confidence"] == "low":
                primary_summary.setdefault("ambiguities", []).append(
                    "The parsed header has low structural confidence; provide explicit column names before analysis."
                )

        related_inputs: list[dict[str, Any]] = []
        if clean_text(secondary_path):
            resolved_secondary_path = self._resolve_input_file_selector(secondary_path, role="secondary_path")
            if is_excel_workbook(resolved_secondary_path):
                workbook = pd.ExcelFile(
                    resolved_secondary_path,
                    engine=excel_workbook_engine(resolved_secondary_path),
                )
                try:
                    selected_secondary_sheet = clean_text(secondary_sheet_name) or workbook.sheet_names[0]
                finally:
                    workbook.close()
                secondary_summary = self._inspect_workbook_sheet(
                    path=resolved_secondary_path,
                    sheet_name=selected_secondary_sheet,
                    role_hint=secondary_role_hint,
                )
            else:
                secondary_summary = self._summarize_inspection_result(
                    inspect_tabular_file(
                        Path(resolved_secondary_path),
                        role_hint=secondary_role_hint,
                        provenance={},
                    ),
                    source_path=resolved_secondary_path,
                    sheet_name=secondary_sheet_name,
                )
            related_inputs.append(
                {
                    "path": resolved_secondary_path,
                    "role_hint": clean_text(secondary_role_hint),
                    "inspection": secondary_summary,
                }
            )

        all_families = list(
            dict.fromkeys(
                [
                    *list(primary_summary.get("candidate_analysis_families") or []),
                    *[
                        family
                        for row in sheet_roles
                        for family in list(row.get("candidate_analysis_families") or [])
                    ],
                ]
            )
        )
        if not all_families:
            all_families = ["run_expression_statistics"]
        structured_data = {
            "path": resolved_path,
            "detected_data_type": clean_text(primary_summary.get("detected_data_type")) or "generic_table",
            "row_identifier_column": clean_text(primary_summary.get("row_identifier_column")),
            "candidate_sample_column": clean_text(primary_summary.get("candidate_sample_column")),
            "candidate_group_columns": list(primary_summary.get("candidate_group_columns") or []),
            "candidate_covariates": list(primary_summary.get("candidate_covariates") or []),
            "candidate_group_levels": dict(primary_summary.get("candidate_group_levels") or {}),
            "candidate_sample_preview": list(primary_summary.get("candidate_sample_preview") or []),
            "matrix_sample_preview": list(primary_summary.get("matrix_sample_preview") or []),
            "matrix_sample_columns": list(primary_summary.get("matrix_sample_columns") or []),
            "candidate_sample_group_map": dict(primary_summary.get("candidate_sample_group_map") or {}),
            "candidate_gene_id_columns": list(primary_summary.get("candidate_gene_id_columns") or []),
            "header_row": primary_summary.get("header_row"),
            "header_selection_score": primary_summary.get("header_selection_score"),
            "header_confidence": primary_summary.get("header_confidence"),
            "sheet_roles": sheet_roles,
            "matrix_orientation": clean_text(primary_summary.get("matrix_orientation")),
            "candidate_analysis_families": all_families,
            "recommended_arguments": dict(primary_summary.get("recommended_arguments") or {}),
            "ambiguities": list(primary_summary.get("ambiguities") or []),
            "next_step_hints": list(primary_summary.get("next_step_hints") or []),
            "provenance_notes": list(primary_summary.get("provenance_notes") or []),
            "validation": dict(primary_summary.get("validation") or {}),
            "warnings": list(primary_summary.get("warnings") or []),
            "one_line_summary": clean_text(primary_summary.get("one_line_summary")),
            "shape": dict(primary_summary.get("shape") or {}),
            "related_inputs": related_inputs,
        }
        warnings.extend(list(structured_data.get("warnings") or []))
        summary = clean_text(primary_summary.get("one_line_summary"))
        if workbook_mode:
            summary = summary or f"Inspected workbook with {len(sheet_roles)} sheet(s)."
        else:
            summary = summary or "Inspected tabular input."
        if structured_data["ambiguities"]:
            summary += f" Found {len(structured_data['ambiguities'])} ambiguity hint(s)."
        workspace_info = self._capture_to_active_session(
            kind="tabular_inspection",
            title=Path(resolved_path).name or "Tabular inspection",
            summary=summary,
            provider="gleskit_inspection",
            payload=structured_data,
            provenance={"path": resolved_path, "role_hint": clean_text(role_hint) or None},
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        compact_structured_data = self._compact_tabular_inspection_structured_data_for_model(structured_data)
        return {
            "summary": summary,
            "provider": "gleskit_inspection",
            "structured_data": compact_structured_data,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def prepare_rnaseq_inputs(
        self,
        *,
        counts_path: str = "",
        source_path: str = "",
        counts_sheet_name: str = "",
        counts_header_row: int | None = None,
        counts_skip_rows: int = 0,
        metadata_path: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int | None = None,
        metadata_skip_rows: int = 0,
        gene_column: str = "",
        sample_key_column: str = "",
        condition_column: str = "",
        sample_group_map: dict[str, str] | None = None,
        sample_filter_column: str = "",
        sample_filter_values: list[str] | None = None,
        design_columns: list[str] | None = None,
        count_transform: str = "none",
        label: str = "",
    ) -> dict[str, Any]:
        source_selector = clean_text(counts_path) or clean_text(source_path)
        if not source_selector:
            raise ProviderError("prepare_rnaseq_inputs requires counts_path or source_path.")
        resolved_counts_path = self._resolve_input_file_selector(source_selector, role="counts_path")
        workbook_layout: dict[str, Any] = {}
        if is_excel_workbook(resolved_counts_path):
            workbook_layout = self._infer_rnaseq_workbook_layout(path=resolved_counts_path)
            if not clean_text(counts_sheet_name):
                counts_sheet_name = clean_text(workbook_layout.get("counts_sheet_name"))
            if counts_header_row is None and workbook_layout.get("counts_header_row") is not None:
                counts_header_row = int(workbook_layout["counts_header_row"])
            # Only infer a metadata sheet when the caller did not provide a
            # separate metadata input. Explicit paths and sheet names remain
            # authoritative.
            if not clean_text(metadata_path) and not clean_text(metadata_sheet_name):
                metadata_sheet_name = clean_text(workbook_layout.get("metadata_sheet_name"))
            if metadata_header_row is None and not clean_text(metadata_path) and workbook_layout.get("metadata_header_row") is not None:
                metadata_header_row = int(workbook_layout["metadata_header_row"])
        resolved_metadata_path = self._resolve_input_file_selector(
            clean_text(metadata_path) or (resolved_counts_path if clean_text(metadata_sheet_name) else ""),
            role="metadata_path",
        )
        if resolved_metadata_path and sample_group_map:
            raise ProviderError(
                "Provide either metadata_path/metadata_sheet_name or sample_group_map, not both.",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Use metadata when it exists; use sample_group_map only when the raw-count matrix has no metadata table.",
            )

        counts_loaded = load_table(
            path=resolved_counts_path,
            sheet_name=counts_sheet_name,
            header_row=int(counts_header_row or 1),
            skip_rows=counts_skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        counts_frame = counts_loaded.frame.copy()
        counts_frame.columns = [clean_text(column) or str(column) for column in counts_frame.columns]
        resolved_gene_column = (
            clean_text(gene_column)
            or clean_text(workbook_layout.get("gene_column"))
            or str(counts_frame.columns[0])
        )
        if resolved_gene_column not in counts_frame.columns:
            raise ProviderError(
                f"Gene column '{resolved_gene_column}' was not found in the counts table. "
                f"Available columns: {', '.join(map(str, counts_frame.columns[:10]))}."
            )
        sample_columns = [str(column) for column in counts_frame.columns if str(column) != resolved_gene_column]
        if not sample_columns:
            raise ProviderError("The counts table must contain at least one sample column beyond the gene column.")

        metadata_from_group_map = not bool(resolved_metadata_path)
        if metadata_from_group_map:
            if not sample_group_map:
                raise ProviderError(
                    "prepare_rnaseq_inputs requires metadata_path, metadata_sheet_name, or an exact sample_group_map.",
                    error_code="invalid_input",
                    retryable=False,
                    suggested_next_step="Provide a metadata table, or inspect the count matrix and pass every matrix_sample_columns entry in sample_group_map.",
                )
            resolved_sample_key_column = clean_text(sample_key_column) or "sample"
            resolved_condition_column = clean_text(condition_column) or "condition"
            metadata_frame = self._metadata_from_sample_group_map(
                sample_columns=sample_columns,
                sample_group_map=sample_group_map,
                sample_column=resolved_sample_key_column,
                condition_column=resolved_condition_column,
            )
            use_workbook_metadata = False
        else:
            metadata_loaded = load_table(
                path=resolved_metadata_path,
                sheet_name=metadata_sheet_name,
                header_row=int(metadata_header_row or 1),
                skip_rows=metadata_skip_rows,
                dtype=str,
                keep_default_na=False,
            )
            metadata_frame = metadata_loaded.frame.copy()
            metadata_frame.columns = [clean_text(column) or str(column) for column in metadata_frame.columns]
            use_workbook_metadata = not clean_text(metadata_path) or resolved_metadata_path == resolved_counts_path
            resolved_sample_key_column = (
                clean_text(sample_key_column)
                or (clean_text(workbook_layout.get("sample_key_column")) if use_workbook_metadata else "")
                or str(metadata_frame.columns[0])
            )
            resolved_condition_column = clean_text(condition_column)

        if resolved_sample_key_column not in metadata_frame.columns:
            raise ProviderError(
                f"Sample key column '{resolved_sample_key_column}' was not found in the metadata table. "
                f"Available columns: {', '.join(map(str, metadata_frame.columns[:10]))}."
            )
        if resolved_condition_column and resolved_condition_column not in metadata_frame.columns:
            raise ProviderError(
                f"Condition column '{resolved_condition_column}' was not found in the metadata table. "
                f"Available columns: {', '.join(map(str, metadata_frame.columns[:10]))}."
            )

        metadata_filtered = metadata_frame.copy()
        filter_values = [clean_text(item) for item in (sample_filter_values or []) if clean_text(item)]
        if clean_text(sample_filter_column):
            if sample_filter_column not in metadata_filtered.columns:
                raise ProviderError(
                    f"Sample filter column '{sample_filter_column}' was not found in the metadata table."
                )
            if not filter_values:
                raise ProviderError("sample_filter_values must be provided when sample_filter_column is used.")
            metadata_filtered = metadata_filtered[
                metadata_filtered[sample_filter_column].astype(str).isin(filter_values)
            ].copy()
            if metadata_filtered.empty:
                raise ProviderError("The requested sample filter removed every metadata row.")

        gene_ids = counts_frame[resolved_gene_column].astype(str).str.strip()
        if gene_ids.eq("").any():
            raise ProviderError("The counts table contains empty gene identifiers.")
        if gene_ids.duplicated().any():
            duplicates = gene_ids[gene_ids.duplicated()].tolist()
            raise ProviderError(
                "The counts table contains duplicate gene identifiers: " + ", ".join(duplicates[:5])
            )
        counts_values = counts_frame.drop(columns=[resolved_gene_column]).copy()
        if counts_values.empty:
            raise ProviderError("The counts table must contain at least one sample column beyond the gene column.")
        counts_values.columns = [clean_text(column) or str(column) for column in counts_values.columns]

        metadata_sample_ids = metadata_filtered[resolved_sample_key_column].astype(str).str.strip()
        if metadata_sample_ids.eq("").any():
            raise ProviderError("The metadata table contains empty sample identifiers.")
        canonical_metadata_map: dict[str, str] = {}
        for sample_id in metadata_sample_ids.tolist():
            token = self._canonical_sample_token(sample_id)
            if not token:
                continue
            if token in canonical_metadata_map and canonical_metadata_map[token] != sample_id:
                raise ProviderError(
                    "The metadata table contains ambiguous sample identifiers after canonical normalization."
                )
            canonical_metadata_map[token] = sample_id

        matched_count_columns: list[str] = []
        matched_metadata_ids: list[str] = []
        unmatched_count_columns: list[str] = []
        for column in counts_values.columns:
            token = self._canonical_sample_token(column)
            matched_metadata = canonical_metadata_map.get(token)
            if not matched_metadata:
                unmatched_count_columns.append(str(column))
                continue
            matched_count_columns.append(str(column))
            matched_metadata_ids.append(matched_metadata)
        if not matched_count_columns:
            raise ProviderError(
                "No counts columns matched the metadata sample identifiers. "
                "Check sample_key_column or inspect the inputs first with inspect_tabular_data."
            )

        # Count/result workbooks often carry summary statistics beside sample
        # columns. Match real samples first, then validate and transform only
        # that aligned matrix so non-count annotations cannot poison DESeq2.
        transformed_counts, transform_notes = self._apply_count_transform(
            counts_values.loc[:, matched_count_columns].copy(),
            count_transform=count_transform,
        )
        transformed_counts.insert(0, resolved_gene_column, gene_ids.tolist())
        metadata_aligned = (
            metadata_filtered.assign(**{resolved_sample_key_column: metadata_sample_ids})
            .set_index(resolved_sample_key_column)
            .loc[matched_metadata_ids]
            .reset_index()
        )

        resolved_design_columns = [clean_text(item) for item in (design_columns or []) if clean_text(item)]
        if resolved_condition_column and resolved_condition_column not in resolved_design_columns:
            resolved_design_columns.append(resolved_condition_column)
        resolved_design_columns = [column for column in resolved_design_columns if column in metadata_aligned.columns]
        recommended_design_formula = "~ 1"
        if resolved_design_columns:
            recommended_design_formula = "~ " + " + ".join(dict.fromkeys(resolved_design_columns))

        root = self._prepared_rnaseq_root(
            label=label,
            source_path=resolved_counts_path,
        )
        sanitized_counts_path = root / "sanitized_counts.tsv"
        sanitized_metadata_path = root / "sanitized_metadata.tsv"
        prepared_manifest_path = root / "prepared_manifest.json"
        transformed_counts.to_csv(sanitized_counts_path, sep="\t", index=False)
        metadata_aligned.to_csv(sanitized_metadata_path, sep="\t", index=False)

        counts_inspection = self._summarize_inspection_result(
            inspect_tabular_file(
                sanitized_counts_path,
                role_hint="counts",
                provenance={},
            ),
            source_path=str(sanitized_counts_path),
        )
        metadata_inspection = self._summarize_inspection_result(
            inspect_tabular_file(
                sanitized_metadata_path,
                role_hint="metadata",
                provenance={},
            ),
            source_path=str(sanitized_metadata_path),
        )
        manifest = {
            "prepared_manifest_version": 1,
            "prepared_manifest_path": str(prepared_manifest_path),
            "source_path": resolved_counts_path,
            "counts_path": str(sanitized_counts_path),
            "metadata_path": str(sanitized_metadata_path),
            "counts_source_path": resolved_counts_path,
            "metadata_source_path": resolved_metadata_path or "sample_group_map",
            "counts_sheet_name": clean_text(counts_sheet_name) or None,
            "metadata_sheet_name": clean_text(metadata_sheet_name) or None,
            "resolved_columns": {
                "gene_column": resolved_gene_column,
                "sample_key_column": resolved_sample_key_column,
                "condition_column": resolved_condition_column or None,
                "design_columns": resolved_design_columns,
            },
            "applied_filters": {
                "sample_filter_column": clean_text(sample_filter_column) or None,
                "sample_filter_values": filter_values,
                "excluded_count_columns": unmatched_count_columns,
            },
            "applied_transform": transform_notes,
            "sample_count": int(len(matched_count_columns)),
            "feature_count": int(len(transformed_counts.index)),
            "recommended_design_formula": recommended_design_formula,
            "provenance_notes": [
                note
                for note in (
                    f"counts_source={resolved_counts_path}",
                    f"metadata_source={resolved_metadata_path or 'sample_group_map'}",
                    f"counts_sheet_name={counts_sheet_name}" if clean_text(counts_sheet_name) else "",
                    f"metadata_sheet_name={metadata_sheet_name}" if clean_text(metadata_sheet_name) else "",
                )
                if note
            ],
            "inspection_summary": {
                "counts": counts_inspection,
                "metadata": metadata_inspection,
            },
        }
        self._write_json_file(prepared_manifest_path, manifest)
        structured_data = {
            "prepared_manifest_path": str(prepared_manifest_path),
            "sanitized_counts_path": str(sanitized_counts_path),
            "sanitized_metadata_path": str(sanitized_metadata_path),
            "resolved_columns": dict(manifest["resolved_columns"]),
            "applied_filters": dict(manifest["applied_filters"]),
            "applied_transform": dict(manifest["applied_transform"]),
            "sample_count": int(manifest["sample_count"]),
            "feature_count": int(manifest["feature_count"]),
            "recommended_design_formula": recommended_design_formula,
            "provenance_notes": list(manifest["provenance_notes"]),
            "inspection_summary": dict(manifest["inspection_summary"]),
        }
        summary = (
            f"Prepared RNA-seq inputs for {structured_data['feature_count']:,} features across "
            f"{structured_data['sample_count']:,} samples."
        )
        if clean_text(resolved_condition_column):
            summary += f" Recommended design formula: {recommended_design_formula}."
        workspace_info = self._capture_to_active_session(
            kind="prepared_rnaseq_inputs",
            title=clean_text(label) or "Prepared RNA-seq inputs",
            summary=summary,
            provider="gleskit_preprocessing",
            payload=structured_data,
            provenance={
                "counts_source_path": resolved_counts_path,
                "metadata_source_path": resolved_metadata_path or "sample_group_map",
                "count_transform": clean_text(count_transform) or "none",
            },
        )
        warnings = self._workspace_warnings(workspace_info)
        return {
            "summary": summary,
            "provider": "gleskit_preprocessing",
            "structured_data": structured_data,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def analyze_deseq2_counts(
        self,
        *,
        counts_path: str = "",
        prepared_manifest_path: str = "",
        counts_sheet_name: str = "",
        counts_header_row: int = 1,
        counts_skip_rows: int = 0,
        sample_metadata_path: str = "",
        sample_metadata_sheet_name: str = "",
        sample_metadata_header_row: int = 1,
        sample_metadata_skip_rows: int = 0,
        sample_metadata_text: str = "",
        sample_group_map: dict[str, str] | None = None,
        species: str | None = None,
        label: str = "",
        comparison: str = "",
        gene_column: str = "",
        sample_column: str = "sample",
        condition_column: str = "condition",
        condition_a: str = "",
        condition_b: str = "",
        design_formula: str = "",
        min_count: int = 10,
        top_n: int = 25,
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 0.0,
        significance_mode: str = "padj",
        log2_fold_change_threshold: float = 0.0,
        lfc_shrinkage: str = "none",
        focus_genes: list[str] | None = None,
        execution_timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        requested_counts_path = clean_text(counts_path)
        requested_sample_metadata_path = clean_text(sample_metadata_path)
        input_counts_path = requested_counts_path
        input_sample_metadata_path = requested_sample_metadata_path
        resolved_prepared_manifest_path = clean_text(prepared_manifest_path)
        prepared_manifest = self._load_prepared_rnaseq_manifest(resolved_prepared_manifest_path)
        analysis_route = "direct_deseq2"
        auto_selected_design_formula = ""
        preparation_summary: dict[str, Any] = {
            "prepared_manifest_path": None,
            "counts_source_path": requested_counts_path or None,
            "metadata_source_path": requested_sample_metadata_path or None,
            "applied_filters": {},
            "applied_transform": {"count_transform": "none"},
        }
        if prepared_manifest:
            input_counts_path = clean_text(prepared_manifest.get("counts_path") or input_counts_path)
            input_sample_metadata_path = clean_text(prepared_manifest.get("metadata_path") or input_sample_metadata_path)
            resolved_columns = dict(prepared_manifest.get("resolved_columns") or {})
            sample_column = clean_text(resolved_columns.get("sample_key_column")) or sample_column
            condition_column = clean_text(resolved_columns.get("condition_column")) or condition_column
            gene_column = clean_text(resolved_columns.get("gene_column")) or gene_column
            if not clean_text(design_formula):
                design_formula = clean_text(prepared_manifest.get("recommended_design_formula")) or design_formula
            # An intercept-only manifest is valid for generic preprocessing, but
            # cannot estimate the explicit contrast supplied to this tool. The
            # contrast column is an unambiguous design term in that case.
            normalized_design = "".join(clean_text(design_formula).split()).lower()
            if (
                normalized_design in {"", "~1", "1"}
                and clean_text(condition_column)
                and clean_text(condition_a)
                and clean_text(condition_b)
            ):
                design_formula = f"~ {clean_text(condition_column)}"
                auto_selected_design_formula = design_formula
            counts_sheet_name = ""
            sample_metadata_sheet_name = ""
            counts_header_row = 1
            sample_metadata_header_row = 1
            counts_skip_rows = 0
            sample_metadata_skip_rows = 0
            analysis_route = "prepared_manifest_deseq2"
            preparation_summary = {
                "prepared_manifest_path": clean_text(
                    prepared_manifest.get("prepared_manifest_path") or resolved_prepared_manifest_path
                )
                or None,
                "counts_source_path": clean_text(prepared_manifest.get("counts_source_path") or requested_counts_path)
                or None,
                "metadata_source_path": clean_text(
                    prepared_manifest.get("metadata_source_path") or requested_sample_metadata_path
                )
                or None,
                "resolved_columns": resolved_columns,
                "applied_filters": dict(prepared_manifest.get("applied_filters") or {}),
                "applied_transform": dict(prepared_manifest.get("applied_transform") or {}),
                "sample_count": prepared_manifest.get("sample_count"),
                "feature_count": prepared_manifest.get("feature_count"),
                "recommended_design_formula": clean_text(prepared_manifest.get("recommended_design_formula")) or None,
                "auto_selected_design_formula": auto_selected_design_formula or None,
                "inspection_summary": dict(prepared_manifest.get("inspection_summary") or {}),
                "provenance_notes": list(prepared_manifest.get("provenance_notes") or []),
            }
        if sample_group_map and (prepared_manifest or clean_text(input_sample_metadata_path) or clean_text(sample_metadata_text)):
            raise ProviderError(
                "Provide sample_group_map only when no prepared, file, or inline sample metadata is available.",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Use the existing metadata source, or remove it and provide one exact sample_group_map.",
            )
        if sample_group_map:
            preparation_summary["metadata_source_path"] = "sample_group_map"
        if not clean_text(input_counts_path):
            raise ProviderError(
                "analyze_deseq2_counts requires counts_path, or prepared_manifest_path that resolves to sanitized counts."
            )
        with ExitStack() as stack:
            normalized_counts_path = self._resolve_input_file_selector(input_counts_path, role="counts_path")
            counts_frame_for_group_map: pd.DataFrame | None = None
            if is_excel_workbook(normalized_counts_path):
                counts_table = load_table(
                    path=normalized_counts_path,
                    sheet_name=counts_sheet_name,
                    header_row=counts_header_row,
                    skip_rows=counts_skip_rows,
                    dtype=str,
                    keep_default_na=False,
                )
                counts_frame_for_group_map = counts_table.frame.copy()
                normalized_counts_path = stack.enter_context(TemporaryDelimitedTable(counts_table.frame))
            elif sample_group_map:
                counts_frame_for_group_map = load_table(
                    path=normalized_counts_path,
                    header_row=counts_header_row,
                    skip_rows=counts_skip_rows,
                    dtype=str,
                    keep_default_na=False,
                ).frame.copy()

            normalized_metadata_path = self._resolve_input_file_selector(
                input_sample_metadata_path,
                role="sample_metadata_path",
            )
            if normalized_metadata_path and is_excel_workbook(normalized_metadata_path):
                metadata_table = load_table(
                    path=normalized_metadata_path,
                    sheet_name=sample_metadata_sheet_name,
                    header_row=sample_metadata_header_row,
                    skip_rows=sample_metadata_skip_rows,
                    dtype=str,
                    keep_default_na=False,
                )
                normalized_metadata_path = stack.enter_context(TemporaryDelimitedTable(metadata_table.frame))

            # R's readers rename a blank first header differently from pandas. Let
            # the DESeq2 backend select its first identifier column instead of
            # forwarding the inspection-only `column_1` alias.
            gene_column_token = "".join(ch for ch in clean_text(gene_column).lower() if ch.isalnum())
            if gene_column_token in {"column1", "column01", "unnamed0", "blankgeneidcolumn"}:
                gene_column = ""
            if sample_group_map:
                if counts_frame_for_group_map is None or counts_frame_for_group_map.empty:
                    raise ProviderError(
                        "Could not load the raw-count matrix needed to validate sample_group_map.",
                        error_code="invalid_input",
                        retryable=False,
                        suggested_next_step="Inspect the count matrix and supply a valid counts_path with its header row.",
                    )
                counts_frame_for_group_map.columns = [
                    clean_text(column) or str(column) for column in counts_frame_for_group_map.columns
                ]
                resolved_gene_column = clean_text(gene_column) or str(counts_frame_for_group_map.columns[0])
                if resolved_gene_column not in counts_frame_for_group_map.columns:
                    raise ProviderError(
                        f"Gene column '{resolved_gene_column}' was not found in the counts table.",
                        error_code="invalid_input",
                        retryable=False,
                        suggested_next_step="Use the exact row_identifier_column returned by inspect_tabular_data.",
                    )
                generated_sample_column = clean_text(sample_column) or "sample"
                generated_condition_column = clean_text(condition_column) or "condition"
                generated_metadata = self._metadata_from_sample_group_map(
                    sample_columns=[
                        str(column)
                        for column in counts_frame_for_group_map.columns
                        if str(column) != resolved_gene_column
                    ],
                    sample_group_map=sample_group_map,
                    sample_column=generated_sample_column,
                    condition_column=generated_condition_column,
                )
                normalized_metadata_path = stack.enter_context(TemporaryDelimitedTable(generated_metadata))
                sample_metadata_text = ""
                sample_column = generated_sample_column
                condition_column = generated_condition_column

            deseq_kwargs: dict[str, Any] = {
                "counts_path": normalized_counts_path,
                "sample_metadata_path": normalized_metadata_path,
                "sample_metadata_text": sample_metadata_text,
                "label": label,
                "comparison": comparison,
                "gene_column": gene_column,
                "sample_column": sample_column,
                "condition_column": condition_column,
                "condition_a": condition_a,
                "condition_b": condition_b,
                "design_formula": design_formula,
                "min_count": min_count,
                "top_n": top_n,
                "padj_threshold": padj_threshold,
                "log2_fold_change_threshold": log2_fold_change_threshold,
                "lfc_shrinkage": lfc_shrinkage,
            }
            if float(p_value_threshold) != 0.05:
                deseq_kwargs["p_value_threshold"] = p_value_threshold
            if float(base_mean_threshold) != 0.0:
                deseq_kwargs["base_mean_threshold"] = base_mean_threshold
            if clean_text(significance_mode).lower() not in {"", "padj"}:
                deseq_kwargs["significance_mode"] = significance_mode
            if execution_timeout_seconds is not None:
                deseq_kwargs["timeout_seconds"] = max(1, int(execution_timeout_seconds))
            parsed = self.deseq2_backend.analyze_counts(
                **deseq_kwargs,
            )

        summary = clean_text(parsed.get("summary")) or "Ran DESeq2 analysis."
        derived_inputs = self._write_deseq2_handoff_files(
            label=label,
            comparison=comparison,
            counts_path=clean_text(input_counts_path) or clean_text(normalized_counts_path) or "",
            species=effective_species,
            structured_data=dict(parsed),
        )
        focus_gene_list = [clean_text(gene) for gene in (focus_genes or []) if clean_text(gene)]
        focus_rows: list[dict[str, Any]] = []
        if focus_gene_list:
            focus_gene_aliases = self._build_focus_gene_aliases(
                focus_genes=focus_gene_list,
                species=effective_species,
            )
            focus_result = self.rnaseq_backend.analyze_result(
                result_path=clean_text(derived_inputs.get("result_table_path") or ""),
                label=clean_text(label) or clean_text(comparison) or "DESeq2 focus",
                comparison=clean_text(comparison),
                gene_column=clean_text((derived_inputs.get("column_map") or {}).get("gene")),
                log2_fold_change_column=clean_text((derived_inputs.get("column_map") or {}).get("log2FoldChange")),
                padj_column=clean_text((derived_inputs.get("column_map") or {}).get("padj")),
                p_value_column=clean_text((derived_inputs.get("column_map") or {}).get("pvalue")),
                base_mean_column=clean_text((derived_inputs.get("column_map") or {}).get("baseMean")),
                dispersion_column=clean_text((derived_inputs.get("column_map") or {}).get("dispersion")),
                stat_column=clean_text((derived_inputs.get("column_map") or {}).get("stat")),
                padj_threshold=padj_threshold,
                p_value_threshold=p_value_threshold,
                base_mean_threshold=base_mean_threshold,
                log2_fold_change_threshold=log2_fold_change_threshold,
                focus_genes=focus_gene_list,
                focus_gene_aliases=focus_gene_aliases,
            )
            focus_rows = [row for row in (focus_result.get("focus_rows") or []) if isinstance(row, dict)]
            if focus_rows:
                parsed["focus_rows"] = focus_rows

        ordered_structured_data: dict[str, Any] = {}
        for key in ("provider", "summary", "input", "counts", "summary_stats"):
            if key in parsed:
                ordered_structured_data[key] = parsed[key]
        ordered_structured_data["derived_inputs"] = derived_inputs
        for key, value in parsed.items():
            if key in ordered_structured_data:
                continue
            ordered_structured_data[key] = value
        artifact_structured_data = ordered_structured_data
        artifact_structured_data["species"] = effective_species
        artifact_structured_data["label"] = clean_text(label) or None
        artifact_structured_data["comparison"] = clean_text(comparison) or None
        artifact_structured_data["upload_path"] = requested_counts_path or None
        artifact_structured_data["input"] = {
            **dict(artifact_structured_data.get("input") or {}),
            "counts_path": clean_text(input_counts_path) or clean_text(normalized_counts_path) or None,
            "counts_sheet_name": clean_text(counts_sheet_name) or None,
            "counts_header_row": int(counts_header_row),
            "counts_skip_rows": int(counts_skip_rows),
            "sample_metadata_path": (
                None
                if sample_group_map
                else clean_text(input_sample_metadata_path) or clean_text(normalized_metadata_path) or None
            ),
            "sample_metadata_source": "sample_group_map" if sample_group_map else (
                "inline" if clean_text(sample_metadata_text) else "file"
            ),
            "sample_group_map_sample_count": len(sample_group_map or {}),
            "sample_metadata_sheet_name": clean_text(sample_metadata_sheet_name) or None,
            "sample_metadata_header_row": int(sample_metadata_header_row),
            "sample_metadata_skip_rows": int(sample_metadata_skip_rows),
            "lfc_shrinkage": clean_text(lfc_shrinkage).lower() or "none",
            "focus_genes": focus_gene_list,
            "prepared_manifest_path": clean_text(
                preparation_summary.get("prepared_manifest_path") or resolved_prepared_manifest_path
            )
            or None,
        }
        summary_stats = dict(artifact_structured_data.get("summary_stats") or {})
        counts_summary = dict(artifact_structured_data.get("counts") or {})
        question_ready_counts = {
            "tested_gene_count": summary_stats.get("filtered_gene_count")
            or counts_summary.get("filtered_row_count")
            or derived_inputs.get("result_row_count"),
            "significant_gene_count": summary_stats.get("significant_gene_count"),
            "padj_threshold": padj_threshold,
            "p_value_threshold": p_value_threshold,
            "base_mean_threshold": base_mean_threshold,
            "significance_mode": clean_text(significance_mode).lower() or "padj",
            "log2_fold_change_threshold": log2_fold_change_threshold,
            "min_count": min_count,
            "comparison": clean_text(comparison)
            or clean_text(counts_summary.get("contrast_label"))
            or None,
            "subset_summary": dict(preparation_summary.get("applied_filters") or {}),
        }
        artifact_structured_data["question_ready_counts"] = question_ready_counts
        artifact_structured_data["analysis_route"] = analysis_route
        artifact_structured_data["preparation_summary"] = preparation_summary
        if focus_rows:
            focus_bits: list[str] = []
            for row in focus_rows[:3]:
                metrics = row.get("key_metrics") or {}
                matched_identifier = clean_text(row.get("matched_identifier")) or clean_text(row.get("query_gene"))
                fragments = [matched_identifier]
                if metrics.get("log2_fold_change") not in (None, ""):
                    fragments.append(f"log2FC={metrics.get('log2_fold_change')}")
                if metrics.get("padj") not in (None, ""):
                    fragments.append(f"padj={metrics.get('padj')}")
                elif metrics.get("stat") not in (None, ""):
                    fragments.append(f"stat={metrics.get('stat')}")
                focus_bits.append(", ".join(fragments))
            if focus_bits:
                summary += " Focus genes: " + "; ".join(focus_bits) + "."
                artifact_structured_data["summary"] = summary

        if _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS"):
            compact_structured_data = self._compact_deseq2_structured_data_for_model(
                artifact_structured_data=artifact_structured_data,
                derived_inputs=derived_inputs,
                species=effective_species,
                label=label,
                comparison=comparison,
                counts_path=counts_path,
            )
        else:
            compact_structured_data = {
                "provider": artifact_structured_data.get("provider"),
                "summary": artifact_structured_data.get("summary"),
                "input": artifact_structured_data.get("input"),
                "counts": artifact_structured_data.get("counts"),
                "summary_stats": artifact_structured_data.get("summary_stats"),
                "top_genes": artifact_structured_data.get("top_genes"),
                "significant_gene_sets": artifact_structured_data.get("significant_gene_sets"),
                "focus_rows": artifact_structured_data.get("focus_rows"),
                "derived_inputs": derived_inputs,
                "species": effective_species,
                "label": clean_text(label) or None,
                "comparison": clean_text(comparison) or None,
                "upload_path": requested_counts_path or None,
                "handoff_root": derived_inputs.get("handoff_root"),
                "manifest_path": derived_inputs.get("manifest_path"),
                "result_table_path": derived_inputs.get("result_table_path"),
                "column_map": derived_inputs.get("column_map"),
                "all_significant_genes_path": derived_inputs.get("all_significant_genes_path"),
                "upregulated_genes_path": derived_inputs.get("upregulated_genes_path"),
                "downregulated_genes_path": derived_inputs.get("downregulated_genes_path"),
                "ranked_gene_scores_path": derived_inputs.get("ranked_gene_scores_path"),
                "result_row_count": derived_inputs.get("result_row_count"),
                "all_significant_gene_count": derived_inputs.get("all_significant_gene_count"),
                "upregulated_gene_count": derived_inputs.get("upregulated_gene_count"),
                "downregulated_gene_count": derived_inputs.get("downregulated_gene_count"),
                "ranked_gene_score_count": derived_inputs.get("ranked_gene_score_count"),
                "question_ready_counts": question_ready_counts,
                "analysis_route": analysis_route,
                "preparation_summary": preparation_summary,
            }

        qc_payload = {
            "species": effective_species,
            "input": artifact_structured_data.get("input") or {},
            "counts": artifact_structured_data.get("counts") or {},
            "summary_stats": artifact_structured_data.get("summary_stats") or {},
            "warnings": [],
        }

        artifact_specs = [
            {
                "kind": "deseq2_results",
                "title": clean_text(label) or clean_text(comparison) or "DESeq2 results",
                "summary": summary,
                "provider": "deseq2",
                "payload": artifact_structured_data,
                "provenance": {
                    "species": effective_species,
                    "counts_path": requested_counts_path or clean_text(input_counts_path) or None,
                    "counts_sheet_name": clean_text(counts_sheet_name) or None,
                    "sample_metadata_path": requested_sample_metadata_path or clean_text(input_sample_metadata_path) or None,
                    "sample_metadata_sheet_name": clean_text(sample_metadata_sheet_name) or None,
                    "sample_column": clean_text(sample_column) or "sample",
                    "condition_column": clean_text(condition_column) or "condition",
                    "condition_a": clean_text(condition_a) or None,
                    "condition_b": clean_text(condition_b) or None,
                    "design_formula": clean_text(design_formula) or None,
                    "lfc_shrinkage": clean_text(lfc_shrinkage).lower() or "none",
                    "prepared_manifest_path": clean_text(
                        preparation_summary.get("prepared_manifest_path") or resolved_prepared_manifest_path
                    )
                    or None,
                },
            },
            {
                "kind": "deseq2_qc",
                "title": clean_text(label) or clean_text(comparison) or "DESeq2 QC",
                "summary": f"QC summary for {clean_text(label) or clean_text(comparison) or 'DESeq2 analysis'}.",
                "provider": "deseq2",
                "payload": qc_payload,
                "provenance": {
                    "species": effective_species,
                    "counts_path": requested_counts_path or clean_text(input_counts_path) or None,
                    "counts_sheet_name": clean_text(counts_sheet_name) or None,
                    "sample_metadata_path": requested_sample_metadata_path or clean_text(input_sample_metadata_path) or None,
                    "sample_metadata_sheet_name": clean_text(sample_metadata_sheet_name) or None,
                },
            },
        ]
        workspace_info = self._capture_artifacts_to_active_session(artifact_specs)
        warnings = self._workspace_warnings(workspace_info)
        return {
            "summary": summary,
            "provider": "deseq2",
            "structured_data": compact_structured_data,
            "derived_inputs": derived_inputs,
            "handoff_root": derived_inputs.get("handoff_root"),
            "manifest_path": derived_inputs.get("manifest_path"),
            "result_table_path": derived_inputs.get("result_table_path"),
            "column_map": derived_inputs.get("column_map"),
            "all_significant_genes_path": derived_inputs.get("all_significant_genes_path"),
            "upregulated_genes_path": derived_inputs.get("upregulated_genes_path"),
            "downregulated_genes_path": derived_inputs.get("downregulated_genes_path"),
            "ranked_gene_scores_path": derived_inputs.get("ranked_gene_scores_path"),
            "result_row_count": derived_inputs.get("result_row_count"),
            "all_significant_gene_count": derived_inputs.get("all_significant_gene_count"),
            "upregulated_gene_count": derived_inputs.get("upregulated_gene_count"),
            "downregulated_gene_count": derived_inputs.get("downregulated_gene_count"),
            "ranked_gene_score_count": derived_inputs.get("ranked_gene_score_count"),
            "question_ready_counts": question_ready_counts,
            "analysis_route": analysis_route,
            "preparation_summary": preparation_summary,
            "warnings": warnings,
            "workspace": workspace_info,
            "artifacts": {
                "result": {
                    "title": artifact_specs[0]["title"],
                    "artifact_id": (workspace_info.get("artifact_ids") or [None])[0],
                },
                "qc": {
                    "title": artifact_specs[1]["title"],
                    "artifact_id": (workspace_info.get("artifact_ids") or [None, None])[1]
                    if len(workspace_info.get("artifact_ids") or []) > 1
                    else None,
                },
            },
        }

    def run_log_transform(
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
        analysis_axis: str = "columns",
        base: float = 2.0,
        offset: float = 1.0,
        preset: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        matrix, resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
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
        transformed_matrix, result = self.expression_matrix_backend.run_log_transform(
            matrix,
            base=base,
            offset=offset,
            preset=preset,
        )
        derived_inputs = self._persist_expression_matrix_output(
            matrix=transformed_matrix,
            step_name="log_transform",
            label=label,
            source_path=resolved_matrix_path,
        )
        provenance = {
            **dict(result.get("input") or {}),
            "base": float(base),
            "offset": float(offset),
            "preset": clean_text(preset) or "custom",
            "transformation": "log_transform",
            "matrix_path": clean_text(resolved_matrix_path) or None,
            "analysis_axis": normalized_analysis_axis,
            "stored_matrix_path": clean_text(derived_inputs.get("matrix_path")) or None,
        }
        captured = self._capture_expression_matrix_analysis(
            step_name="log_transform",
            result=result,
            label=label,
            provenance=provenance,
            artifact_kind="log_transform",
            derived_inputs=derived_inputs,
        )
        return captured

    def _run_expression_matrix_step(
        self,
        *,
        step_name: str,
        matrix: Any,
        analysis_axis: str = "columns",
        label: str = "",
        provenance: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        normalized_analysis_axis = self._normalize_analysis_axis(analysis_axis)
        derived_inputs: dict[str, Any] | None = None
        question_ready: dict[str, Any] | None = None
        if step_name == "pca":
            result = self.expression_matrix_backend.run_pca(
                matrix,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                n_components=int(kwargs.get("n_components", 2)),
                scale=bool(kwargs.get("scale", True)),
                top_n_loadings=int(kwargs.get("top_n_loadings", 15)),
                force=bool(kwargs.get("force", False)),
            )
            extra_provenance = {
                "analysis_axis": normalized_analysis_axis,
                "n_components": max(1, int(kwargs.get("n_components", 2))),
                "scale": bool(kwargs.get("scale", True)),
                "top_n_loadings": max(1, int(kwargs.get("top_n_loadings", 15))),
                "force": bool(kwargs.get("force", False)),
            }
            variance_ratio = list(((result.get("components") or {}).get("explained_variance_ratio") or []))
            question_ready = {
                "component_count": len(variance_ratio),
                "pc1_explained_variance_ratio": float(variance_ratio[0]) if variance_ratio else None,
                "pc1_explained_variance_percent": (float(variance_ratio[0]) * 100.0) if variance_ratio else None,
            }
        elif step_name == "hierarchical_clustering":
            result = self.expression_matrix_backend.run_hierarchical_clustering(
                matrix,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                cluster_count=int(kwargs.get("cluster_count", 3)),
                linkage=clean_text(kwargs.get("linkage", "")) or "average",
                metric=clean_text(kwargs.get("metric", "")) or "correlation",
            )
            extra_provenance = {
                "analysis_axis": normalized_analysis_axis,
                "cluster_count": max(1, int(kwargs.get("cluster_count", 3))),
                "linkage": clean_text(kwargs.get("linkage", "")) or "average",
                "metric": clean_text(kwargs.get("metric", "")) or "correlation",
            }
            derived_inputs = self._persist_cluster_labels_output(
                cluster_labels=dict(result.get("cluster_labels") or {}),
                clusters=list(result.get("clusters") or []),
                step_name="hierarchical_clustering",
                label=label,
                source_path=str((result.get("input") or {}).get("path") or ""),
            )
            question_ready = {
                "cluster_count": int(result.get("cluster_count") or 0),
                "cluster_sizes": [
                    {
                        "cluster": row.get("cluster"),
                        "size": row.get("size"),
                    }
                    for row in list(result.get("clusters") or [])
                    if isinstance(row, dict)
                ],
                "cluster_labels_path": derived_inputs.get("cluster_labels_path"),
            }
        elif step_name == "correlation":
            secondary_matrix = kwargs.get("secondary_matrix")
            normalized_secondary_analysis_axis = self._normalize_analysis_axis(
                clean_text(kwargs.get("secondary_analysis_axis", "columns")) or "columns"
            )
            result = self.expression_matrix_backend.run_correlation(
                matrix,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                method=clean_text(kwargs.get("method", "")) or "pearson",
                secondary_matrix=secondary_matrix,
                secondary_value_column=clean_text(kwargs.get("secondary_value_column", "")),
                secondary_alignment_axis="features",
                group_labels=[str(value) for value in kwargs.get("group_labels", [])] if isinstance(kwargs.get("group_labels"), list) else None,
                group_summary_stat=clean_text(kwargs.get("group_summary_stat", "")) or "mean",
                top_n=int(kwargs.get("top_n", 25)),
            )
            extra_provenance = {
                "analysis_axis": normalized_analysis_axis,
                "method": clean_text(kwargs.get("method", "")) or "pearson",
                "top_n": max(1, int(kwargs.get("top_n", 25))),
                "group_summary_stat": clean_text(kwargs.get("group_summary_stat", "")) or "mean",
            }
            if secondary_matrix is not None:
                extra_provenance.update(
                    {
                        "secondary_source_kind": getattr(secondary_matrix, "source_kind", ""),
                        "secondary_path": getattr(secondary_matrix, "path", "") or None,
                        "secondary_row_id_column": getattr(secondary_matrix, "row_id_column", "") or None,
                        "secondary_analysis_axis": normalized_secondary_analysis_axis,
                        "secondary_value_column": clean_text(kwargs.get("secondary_value_column", "")) or None,
                    }
                )
        elif step_name == "consensus_clustering":
            result = self.expression_matrix_backend.run_consensus_clustering(
                matrix,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                cluster_count=int(kwargs.get("cluster_count", 3)),
                iterations=int(kwargs.get("iterations", 50)),
                sample_fraction=float(kwargs.get("sample_fraction", 0.7)),
                feature_fraction=float(kwargs.get("feature_fraction", 1.0)),
                random_seed=int(kwargs.get("random_seed", 0)),
                metric=clean_text(kwargs.get("metric", "")) or "correlation",
                linkage=clean_text(kwargs.get("linkage", "")) or "average",
            )
            extra_provenance = {
                "analysis_axis": normalized_analysis_axis,
                "cluster_count": max(1, int(kwargs.get("cluster_count", 3))),
                "iterations": max(1, int(kwargs.get("iterations", 50))),
                "sample_fraction": float(kwargs.get("sample_fraction", 0.7)),
                "feature_fraction": float(kwargs.get("feature_fraction", 1.0)),
                "random_seed": int(kwargs.get("random_seed", 0)),
                "metric": clean_text(kwargs.get("metric", "")) or "correlation",
                "linkage": clean_text(kwargs.get("linkage", "")) or "average",
            }
            derived_inputs = self._persist_cluster_labels_output(
                cluster_labels=dict(result.get("cluster_labels") or {}),
                clusters=list(result.get("clusters") or []),
                step_name="consensus_clustering",
                label=label,
                source_path=str((result.get("input") or {}).get("path") or ""),
            )
            question_ready = {
                "cluster_count": int(result.get("cluster_count") or 0),
                "cluster_sizes": [
                    {
                        "cluster": row.get("cluster"),
                        "size": row.get("size"),
                    }
                    for row in list(result.get("clusters") or [])
                    if isinstance(row, dict)
                ],
                "cluster_labels_path": derived_inputs.get("cluster_labels_path"),
                "mean_stability": ((result.get("consistency_summary") or {}).get("mean_stability")),
            }
        else:
            raise ValueError(
                "Unsupported expression matrix step. Use pca, hierarchical_clustering, correlation, or consensus_clustering."
            )

        return self._capture_expression_matrix_analysis(
            step_name=step_name,
            result=result,
            label=label,
            provenance={
                **dict(provenance or {}),
                **extra_provenance,
            },
            derived_inputs=derived_inputs,
            question_ready=question_ready,
        )

    def run_expression_pca(
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
        analysis_axis: str = "columns",
        n_components: int = 2,
        scale: bool = True,
        top_n_loadings: int = 15,
        force: bool = False,
        label: str = "",
    ) -> dict[str, Any]:
        matrix, _resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
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
        return self._run_expression_matrix_step(
            step_name="pca",
            matrix=matrix,
            analysis_axis=normalized_analysis_axis,
            label=label,
            n_components=n_components,
            scale=scale,
            top_n_loadings=top_n_loadings,
            force=force,
        )

    def run_expression_hierarchical_clustering(
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
        analysis_axis: str = "columns",
        cluster_count: int = 3,
        linkage: str = "average",
        metric: str = "correlation",
        label: str = "",
    ) -> dict[str, Any]:
        matrix, _resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
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
        return self._run_expression_matrix_step(
            step_name="hierarchical_clustering",
            matrix=matrix,
            analysis_axis=normalized_analysis_axis,
            label=label,
            cluster_count=cluster_count,
            linkage=linkage,
            metric=metric,
        )

    def run_expression_correlation(
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
        analysis_axis: str = "columns",
        method: str = "pearson",
        secondary_matrix_path: str = "",
        secondary_table_text: str = "",
        secondary_row_id_column: str = "",
        secondary_delimiter: str = "",
        secondary_sheet_name: str = "",
        secondary_header_row: int = 1,
        secondary_skip_rows: int = 0,
        secondary_transpose: bool = False,
        secondary_analysis_axis: str = "columns",
        secondary_value_column: str = "",
        group_labels: list[str] | None = None,
        group_summary_stat: str = "mean",
        top_n: int = 25,
        label: str = "",
    ) -> dict[str, Any]:
        matrix, _resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
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
        secondary_matrix = None
        if clean_text(secondary_matrix_path) or clean_text(secondary_table_text) or clean_text(secondary_value_column):
            secondary_matrix, _resolved_secondary_path, _normalized_secondary_axis = self._load_expression_matrix_input(
                matrix_path=secondary_matrix_path,
                table_text=secondary_table_text,
                row_id_column=secondary_row_id_column,
                delimiter=secondary_delimiter,
                sheet_name=secondary_sheet_name,
                header_row=secondary_header_row,
                skip_rows=secondary_skip_rows,
                transpose=secondary_transpose,
                analysis_axis=secondary_analysis_axis,
                role="secondary_matrix_path",
            )
        return self._run_expression_matrix_step(
            step_name="correlation",
            matrix=matrix,
            analysis_axis=normalized_analysis_axis,
            label=label,
            method=method,
            secondary_matrix=secondary_matrix,
            secondary_value_column=secondary_value_column,
            secondary_analysis_axis=self._normalize_analysis_axis(secondary_analysis_axis),
            group_labels=group_labels,
            group_summary_stat=group_summary_stat,
            top_n=top_n,
        )

    def run_expression_consensus_clustering(
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
        analysis_axis: str = "columns",
        cluster_count: int = 3,
        iterations: int = 50,
        sample_fraction: float = 0.7,
        feature_fraction: float = 1.0,
        random_seed: int = 0,
        metric: str = "correlation",
        linkage: str = "average",
        label: str = "",
    ) -> dict[str, Any]:
        matrix, _resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
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
        return self._run_expression_matrix_step(
            step_name="consensus_clustering",
            matrix=matrix,
            analysis_axis=normalized_analysis_axis,
            label=label,
            cluster_count=cluster_count,
            iterations=iterations,
            sample_fraction=sample_fraction,
            feature_fraction=feature_fraction,
            random_seed=random_seed,
            metric=metric,
            linkage=linkage,
        )

    def run_supervised_expression_model(
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
        analysis_axis: str = "columns",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        sample_column: str = "sample",
        target_column: str = "",
        task: str = "classification",
        model_family: str = "logistic_regression",
        split_strategy: str = "train_test",
        group_column: str = "",
        test_size: float = 0.3,
        random_seed: int = 0,
        top_n_features: int = 25,
        label: str = "",
    ) -> dict[str, Any]:
        matrix, resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
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
        resolved_metadata_path = self._resolve_input_file_selector(metadata_path, role="metadata_path")
        result = self.supervised_expression_backend.run_supervised_model(
            matrix,
            metadata_path=resolved_metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
            sample_column=sample_column,
            target_column=target_column,
            task=clean_text(task).lower() or "classification",  # type: ignore[arg-type]
            model_family=clean_text(model_family).lower() or "logistic_regression",  # type: ignore[arg-type]
            split_strategy=clean_text(split_strategy).lower() or "train_test",  # type: ignore[arg-type]
            group_column=group_column,
            test_size=test_size,
            random_seed=random_seed,
            top_n_features=top_n_features,
        )
        workspace_info = self._capture_to_active_session(
            kind="supervised_expression_model",
            title=clean_text(label) or self._expression_matrix_title("supervised_expression_model", source_path=matrix.path),
            summary=str(result.get("summary") or ""),
            provider="supervised_expression",
            payload=result,
            provenance={
                "matrix_path": clean_text(resolved_matrix_path) or None,
                "metadata_path": clean_text(resolved_metadata_path) or None,
                "analysis_axis": normalized_analysis_axis,
                "target_column": clean_text(target_column) or None,
                "task": clean_text(task).lower() or "classification",
                "model_family": clean_text(model_family).lower() or "logistic_regression",
                "split_strategy": clean_text(split_strategy).lower() or "train_test",
            },
        )
        return {
            "summary": str(result.get("summary") or ""),
            "provider": "supervised_expression",
            "structured_data": result,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def _analyze_pooled_stratified_deseq2_endpoint(
        self,
        *,
        analysis: str,
        counts_path: str,
        sample_metadata_path: str,
        species: str | None,
        counts_sheet_name: str,
        counts_header_row: int,
        sample_metadata_sheet_name: str,
        sample_metadata_header_row: int,
        gene_column: str,
        sample_key_column: str,
        stratum_column: str,
        sex_column: str,
        target_strata: list[str] | None,
        reference_strata: list[str] | None,
        condition_a: str,
        condition_b: str,
        feature_annotation_path: str,
        feature_annotation_sheet_name: str,
        feature_annotation_header_row: int,
        feature_annotation_key_column: str,
        feature_annotation_group_column: str,
        feature_annotation_group_value: str,
        padj_threshold: float,
        base_mean_threshold: float,
        absolute_log2_fold_change_threshold: float,
        lfc_shrinkage: str,
        test: str,
        focus_genes: list[str] | None,
        label: str,
    ) -> dict[str, Any]:
        """Run a target-strata DESeq2 endpoint without fabricating a reference comparison."""
        workflow_started_at = time.monotonic()
        execution_budget_seconds = self._eval_execution_timeout_seconds()
        effective_species = self._effective_species(species)
        resolved_counts_path = self._resolve_input_file_selector(counts_path, role="counts_path")
        resolved_metadata_path = self._resolve_input_file_selector(sample_metadata_path, role="sample_metadata_path")
        metadata = load_table(
            path=resolved_metadata_path,
            sheet_name=sample_metadata_sheet_name,
            header_row=sample_metadata_header_row,
            dtype=str,
            keep_default_na=False,
        )
        metadata_frame = metadata.frame
        required_columns = [sample_key_column, stratum_column, sex_column]
        missing = [column for column in required_columns if clean_text(column) and column not in metadata_frame.columns]
        if missing:
            raise ProviderError(
                "Stratified DESeq2 metadata columns were not found: " + ", ".join(missing) + ". "
                "Use exact labels returned by inspect_tabular_data."
            )
        if stratum_column not in metadata_frame.columns or sex_column not in metadata_frame.columns:
            raise ProviderError(
                "Stratified DESeq2 requires exact stratum and condition columns. "
                f"Available columns: {', '.join(map(str, metadata_frame.columns[:16]))}."
            )
        strata_levels = unique_preserve_order(metadata_frame[stratum_column].tolist())
        target = [clean_text(value) for value in (target_strata or []) if clean_text(value)]
        if not target and analysis == "focus_gene_lfc":
            # A focus-gene question without a named biological subset is an
            # overall contrast, not a request to invent one stratum at a time.
            target = list(strata_levels)
        if not target:
            raise ProviderError(
                "target_strata must contain the exact metadata levels named by the question. "
                f"Available levels for {stratum_column}: {', '.join(strata_levels)}."
            )
        unknown = [level for level in target if level not in strata_levels]
        if unknown:
            raise ProviderError(
                "Stratified DESeq2 strata were not found in metadata: " + ", ".join(unknown) + ". "
                f"Available levels for {stratum_column}: {', '.join(strata_levels)}."
            )

        annotation_groups: dict[str, str] = {}
        resolved_annotation_path = self._resolve_input_file_selector(
            feature_annotation_path,
            role="feature_annotation_path",
        )
        if resolved_annotation_path:
            annotation = load_table(
                path=resolved_annotation_path,
                sheet_name=feature_annotation_sheet_name,
                header_row=feature_annotation_header_row,
                dtype=str,
                keep_default_na=False,
            )
            annotation_key = feature_annotation_key_column or str(annotation.frame.columns[0])
            annotation_group = feature_annotation_group_column
            if annotation_key not in annotation.frame.columns or annotation_group not in annotation.frame.columns:
                raise ProviderError(
                    "Feature annotation requires exact key and group columns. "
                    f"Available columns: {', '.join(map(str, annotation.frame.columns[:12]))}."
                )
            for value, group in zip(annotation.frame[annotation_key], annotation.frame[annotation_group], strict=False):
                gene_key = self._canonical_sample_token(normalize_gene_token(value, species=effective_species))
                group_value = clean_text(group)
                if gene_key and group_value:
                    annotation_groups[gene_key] = group_value
        if analysis == "annotation_group_lfc_test" and not annotation_groups:
            raise ProviderError(
                "annotation_group_lfc_test requires feature_annotation_path with exact key and group columns."
            )

        remaining_seconds: float | None = None
        if execution_budget_seconds is not None:
            remaining_seconds = execution_budget_seconds - (time.monotonic() - workflow_started_at)
            if remaining_seconds < 1:
                raise ProviderError(
                    "Stratified DESeq2 exhausted its evaluation budget before the pooled contrast started.",
                    error_code="tool_timeout",
                    retryable=False,
                    suggested_next_step="Increase the episode tool budget before retrying.",
                    diagnostic=f"remaining_seconds={remaining_seconds:.1f}",
                )
        prepared = self.prepare_rnaseq_inputs(
            counts_path=resolved_counts_path,
            counts_sheet_name=counts_sheet_name,
            counts_header_row=counts_header_row,
            metadata_path=resolved_metadata_path,
            metadata_sheet_name=sample_metadata_sheet_name,
            metadata_header_row=sample_metadata_header_row,
            gene_column=gene_column,
            sample_key_column=sample_key_column,
            condition_column=sex_column,
            sample_filter_column=stratum_column,
            sample_filter_values=target,
            design_columns=[sex_column],
            label=f"{label or 'Stratified DESeq2'}: pooled target strata",
        )
        prepared_manifest_path = clean_text((prepared.get("structured_data") or {}).get("prepared_manifest_path"))
        deseq_timeout_seconds: int | None = None
        if execution_budget_seconds is not None:
            remaining_seconds = execution_budget_seconds - (time.monotonic() - workflow_started_at)
            if remaining_seconds < 1:
                raise ProviderError(
                    "Stratified DESeq2 exhausted its evaluation budget while preparing the pooled contrast.",
                    error_code="tool_timeout",
                    retryable=False,
                    suggested_next_step="Increase the episode tool budget before retrying.",
                    diagnostic=f"remaining_seconds={remaining_seconds:.1f}",
                )
            deseq_timeout_seconds = max(1, int(remaining_seconds))
        deseq_result = self.analyze_deseq2_counts(
            prepared_manifest_path=prepared_manifest_path,
            species=effective_species,
            label=f"{label or 'M vs F'}: pooled target strata",
            comparison=f"{condition_a} vs {condition_b} ({', '.join(target)})",
            condition_a=condition_a,
            condition_b=condition_b,
            significance_mode="padj",
            padj_threshold=padj_threshold,
            base_mean_threshold=base_mean_threshold,
            log2_fold_change_threshold=absolute_log2_fold_change_threshold,
            lfc_shrinkage=lfc_shrinkage,
            execution_timeout_seconds=deseq_timeout_seconds,
        )
        result_path = clean_text((deseq_result.get("derived_inputs") or {}).get("result_table_path"))
        result_table = load_table(path=result_path, dtype=str, keep_default_na=False)
        records = result_table.frame.to_dict(orient="records")

        def finite_metric(row: dict[str, Any], column: str) -> float | None:
            try:
                value = float(row.get(column))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        normalized_records: list[dict[str, Any]] = []
        for row in records:
            gene = clean_text(row.get("gene"))
            gene_key = self._canonical_sample_token(normalize_gene_token(gene, species=effective_species))
            lfc = finite_metric(row, "log2_fold_change")
            if not gene_key or lfc is None:
                continue
            normalized_records.append(
                {
                    "gene": gene,
                    "gene_key": gene_key,
                    "log2_fold_change": lfc,
                    "base_mean": finite_metric(row, "base_mean"),
                    "p_value": finite_metric(row, "p_value"),
                    "padj": finite_metric(row, "padj"),
                    "annotation_group": annotation_groups.get(gene_key),
                }
            )

        input_payload = {
            "stratum_column": stratum_column,
            "sex_column": sex_column,
            "condition_a": condition_a,
            "condition_b": condition_b,
            "target_strata": target,
            "reference_strata_ignored": [clean_text(value) for value in (reference_strata or []) if clean_text(value)],
            "feature_annotation_path": resolved_annotation_path or None,
            "feature_annotation_group_column": feature_annotation_group_column,
            "feature_annotation_group_value": feature_annotation_group_value,
            "padj_threshold": float(padj_threshold),
            "base_mean_threshold": float(base_mean_threshold),
            "absolute_log2_fold_change_threshold": float(absolute_log2_fold_change_threshold),
            "lfc_shrinkage": lfc_shrinkage,
            "execution_budget_seconds": execution_budget_seconds,
            "execution_timeout_seconds": deseq_timeout_seconds,
        }
        per_stratum = [
            {
                "strata": target,
                "role": "target",
                "result_path": result_path,
                "prepared_manifest_path": prepared_manifest_path,
                "execution_timeout_seconds": deseq_timeout_seconds,
            }
        ]

        if analysis == "focus_gene_lfc":
            requested_focus_genes = [clean_text(gene) for gene in (focus_genes or []) if clean_text(gene)]
            if not requested_focus_genes:
                raise ProviderError("focus_gene_lfc requires at least one focus_genes value.")
            focus_rows: list[dict[str, Any]] = []
            for query_gene in requested_focus_genes:
                query_key = self._canonical_sample_token(normalize_gene_token(query_gene, species=effective_species))
                matched = next((row for row in normalized_records if row["gene_key"] == query_key), None)
                if matched is None:
                    raise ProviderError(
                        f"Focus gene '{query_gene}' was not present in the pooled DESeq2 result table."
                    )
                focus_rows.append(
                    {
                        "query_gene": query_gene,
                        "matched_identifier": matched["gene"],
                        "log2_fold_change": matched["log2_fold_change"],
                        "base_mean": matched["base_mean"],
                        "p_value": matched["p_value"],
                        "padj": matched["padj"],
                        "annotation_group": matched["annotation_group"],
                        "meets_base_mean_threshold": (
                            matched["base_mean"] is not None and matched["base_mean"] > float(base_mean_threshold)
                        ),
                        "meets_absolute_log2_fold_change_threshold": (
                            abs(float(matched["log2_fold_change"])) > float(absolute_log2_fold_change_threshold)
                        ),
                    }
                )
            result_payload = {"focus_rows": focus_rows, "focus_gene_count": len(focus_rows)}
            summary = f"Returned pooled M-versus-F log2 fold changes for {len(focus_rows)} requested gene(s)."
        elif analysis == "significant_gene_count":
            significant_rows = [
                row
                for row in normalized_records
                if row["padj"] is not None
                and row["padj"] < float(padj_threshold)
                and row["base_mean"] is not None
                and row["base_mean"] > float(base_mean_threshold)
                and abs(float(row["log2_fold_change"])) > float(absolute_log2_fold_change_threshold)
            ]
            result_payload = {
                "significant_gene_count": len(significant_rows),
                "tested_gene_count": len(normalized_records),
                "significance_mode": "padj",
                "padj_threshold": float(padj_threshold),
                "base_mean_threshold": float(base_mean_threshold),
                "absolute_log2_fold_change_threshold": float(absolute_log2_fold_change_threshold),
            }
            summary = (
                f"Counted {len(significant_rows)} pooled target-strata genes passing padj, baseMean, and absolute-LFC filters."
            )
        elif analysis == "annotation_group_lfc_test":
            group_token = clean_text(feature_annotation_group_value).casefold()
            selected_values = [
                float(row["log2_fold_change"])
                for row in normalized_records
                if clean_text(row["annotation_group"]).casefold() == group_token
            ]
            other_values = [
                float(row["log2_fold_change"])
                for row in normalized_records
                if clean_text(row["annotation_group"])
                and clean_text(row["annotation_group"]).casefold() != group_token
            ]
            if len(selected_values) < 2 or len(other_values) < 2:
                raise ProviderError(
                    "Annotation-group LFC comparison requires at least two finite LFC values in both groups. "
                    f"selected_count={len(selected_values)}, other_count={len(other_values)}."
                )
            test_token = clean_text(test).lower() or "welch"
            if test_token not in {"t", "welch"}:
                raise ProviderError("annotation_group_lfc_test supports test='t' or test='welch'.")
            statistic, p_value = stats.ttest_ind(
                selected_values,
                other_values,
                equal_var=test_token == "t",
                nan_policy="omit",
            )
            result_payload = {
                "statistic_name": "t",
                "statistic": float(statistic),
                "p_value": float(p_value),
                "test": test_token,
                "selected_annotation_group": feature_annotation_group_value,
                "selected_lfc_count": len(selected_values),
                "other_annotated_lfc_count": len(other_values),
                "selected_lfc_mean": float(sum(selected_values) / len(selected_values)),
                "other_annotated_lfc_mean": float(sum(other_values) / len(other_values)),
            }
            summary = (
                f"Compared {len(selected_values)} {feature_annotation_group_value} LFCs with "
                f"{len(other_values)} other annotated LFCs using a {test_token} t-test."
            )
        else:  # Defensive guard for callers that bypass the public normalizer.
            raise ProviderError(f"Unsupported pooled stratified DESeq2 analysis '{analysis}'.")

        structured_data = {
            "analysis": f"stratified_deseq2_{analysis}",
            "provider": "gleskit_stratified_deseq2",
            "input": input_payload,
            "result": result_payload,
            "per_stratum": per_stratum,
        }
        workspace_info = self._capture_to_active_session(
            kind="stratified_deseq2",
            title=clean_text(label) or "Stratified DESeq2 pooled endpoint",
            summary=summary,
            provider="gleskit_stratified_deseq2",
            payload=structured_data,
            provenance=input_payload,
        )
        return {
            "summary": summary,
            "provider": "gleskit_stratified_deseq2",
            "structured_data": structured_data,
            "question_ready": result_payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def analyze_stratified_deseq2(
        self,
        *,
        counts_path: str,
        sample_metadata_path: str,
        species: str | None = None,
        counts_sheet_name: str = "",
        counts_header_row: int = 1,
        sample_metadata_sheet_name: str = "",
        sample_metadata_header_row: int = 1,
        gene_column: str = "",
        sample_key_column: str = "",
        stratum_column: str = "celltype",
        sex_column: str = "sex",
        target_strata: list[str] | None = None,
        reference_strata: list[str] | None = None,
        condition_a: str = "M",
        condition_b: str = "F",
        feature_annotation_path: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "gene_biotype",
        feature_annotation_group_value: str = "protein_coding",
        analysis: str = "lfc_distribution_test",
        padj_threshold: float = 0.05,
        focus_genes: list[str] | None = None,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 10.0,
        absolute_log2_fold_change_threshold: float = 0.5,
        lfc_shrinkage: str = "apeglm",
        test: str = "welch",
        label: str = "",
    ) -> dict[str, Any]:
        """Run an explicit stratified DESeq2 analysis.

        ``lfc_distribution_test`` preserves the original per-stratum workflow.
        The endpoint modes run one pooled contrast over ``target_strata`` so that
        gene-level, count, and annotation-group questions do not spend time on
        unrelated reference strata.
        """
        analysis_token = clean_text(analysis).lower() or "lfc_distribution_test"
        endpoint_aliases = {
            "lfc_distribution_test": "lfc_distribution_test",
            "distribution_test": "lfc_distribution_test",
            "focus_gene_lfc": "focus_gene_lfc",
            "focus_gene": "focus_gene_lfc",
            "significant_gene_count": "significant_gene_count",
            "annotation_group_lfc_test": "annotation_group_lfc_test",
            "annotation_group_test": "annotation_group_lfc_test",
        }
        normalized_analysis = endpoint_aliases.get(analysis_token)
        if normalized_analysis is None:
            raise ProviderError(
                "analysis must be one of: lfc_distribution_test, focus_gene_lfc, "
                "significant_gene_count, annotation_group_lfc_test."
            )
        if normalized_analysis != "lfc_distribution_test":
            return self._analyze_pooled_stratified_deseq2_endpoint(
                analysis=normalized_analysis,
                counts_path=counts_path,
                sample_metadata_path=sample_metadata_path,
                species=species,
                counts_sheet_name=counts_sheet_name,
                counts_header_row=counts_header_row,
                sample_metadata_sheet_name=sample_metadata_sheet_name,
                sample_metadata_header_row=sample_metadata_header_row,
                gene_column=gene_column,
                sample_key_column=sample_key_column,
                stratum_column=stratum_column,
                sex_column=sex_column,
                target_strata=target_strata,
                reference_strata=reference_strata,
                condition_a=condition_a,
                condition_b=condition_b,
                feature_annotation_path=feature_annotation_path,
                feature_annotation_sheet_name=feature_annotation_sheet_name,
                feature_annotation_header_row=feature_annotation_header_row,
                feature_annotation_key_column=feature_annotation_key_column,
                feature_annotation_group_column=feature_annotation_group_column,
                feature_annotation_group_value=feature_annotation_group_value,
                padj_threshold=padj_threshold,
                base_mean_threshold=base_mean_threshold,
                absolute_log2_fold_change_threshold=absolute_log2_fold_change_threshold,
                lfc_shrinkage=lfc_shrinkage,
                test=test,
                focus_genes=focus_genes,
                label=label,
            )
        workflow_started_at = time.monotonic()
        execution_budget_seconds = self._eval_execution_timeout_seconds()
        effective_species = self._effective_species(species)
        resolved_counts_path = self._resolve_input_file_selector(counts_path, role="counts_path")
        resolved_metadata_path = self._resolve_input_file_selector(sample_metadata_path, role="sample_metadata_path")
        metadata = load_table(
            path=resolved_metadata_path,
            sheet_name=sample_metadata_sheet_name,
            header_row=sample_metadata_header_row,
            dtype=str,
            keep_default_na=False,
        )
        metadata_frame = metadata.frame
        required_columns = [sample_key_column, stratum_column, sex_column]
        missing = [column for column in required_columns if clean_text(column) and column not in metadata_frame.columns]
        if missing:
            raise ProviderError(
                "Stratified DESeq2 metadata columns were not found: " + ", ".join(missing) + ". "
                "Use exact labels returned by inspect_tabular_data."
            )
        strata_levels = unique_preserve_order(metadata_frame[stratum_column].tolist())
        target = [clean_text(value) for value in (target_strata or []) if clean_text(value)]
        reference = [clean_text(value) for value in (reference_strata or []) if clean_text(value)]
        if not target:
            raise ProviderError("target_strata must contain at least one exact metadata level.")
        if not reference:
            reference = [level for level in strata_levels if level not in set(target)]
        selected_strata = unique_preserve_order(target + reference)
        unknown = [level for level in selected_strata if level not in strata_levels]
        if unknown:
            raise ProviderError(
                "Stratified DESeq2 strata were not found in metadata: " + ", ".join(unknown) + ". "
                f"Available levels for {stratum_column}: {', '.join(strata_levels)}."
            )
        if not set(target).isdisjoint(reference):
            raise ProviderError("target_strata and reference_strata must be disjoint.")

        protein_coding_ids: set[str] | None = None
        resolved_annotation_path = self._resolve_input_file_selector(
            feature_annotation_path,
            role="feature_annotation_path",
        )
        if resolved_annotation_path:
            annotation = load_table(
                path=resolved_annotation_path,
                sheet_name=feature_annotation_sheet_name,
                header_row=feature_annotation_header_row,
                dtype=str,
                keep_default_na=False,
            )
            annotation_key = feature_annotation_key_column or str(annotation.frame.columns[0])
            annotation_group = feature_annotation_group_column
            if annotation_key not in annotation.frame.columns or annotation_group not in annotation.frame.columns:
                raise ProviderError(
                    "Feature annotation requires exact key and group columns. "
                    f"Available columns: {', '.join(map(str, annotation.frame.columns[:12]))}."
                )
            protein_coding_ids = {
                self._canonical_sample_token(normalize_gene_token(value, species=effective_species))
                for value, group in zip(annotation.frame[annotation_key], annotation.frame[annotation_group], strict=False)
                if clean_text(group).casefold() == clean_text(feature_annotation_group_value).casefold()
            }

        filtered_counts_path = resolved_counts_path
        prefilter_summary: dict[str, Any] = {
            "applied": False,
            "feature_count_before": None,
            "feature_count_after": None,
        }
        if protein_coding_ids is not None:
            counts_table = load_table(path=resolved_counts_path, dtype=str, keep_default_na=False)
            counts_frame = counts_table.frame.copy()
            resolved_counts_gene_column = clean_text(gene_column) or str(counts_frame.columns[0])
            if resolved_counts_gene_column not in counts_frame.columns:
                raise ProviderError(
                    f"Counts gene column '{resolved_counts_gene_column}' was not found while applying the feature filter."
                )
            keep_features = counts_frame[resolved_counts_gene_column].map(
                lambda value: self._canonical_sample_token(normalize_gene_token(value, species=effective_species))
                in protein_coding_ids
            )
            filtered_counts = counts_frame.loc[keep_features].copy()
            if filtered_counts.empty:
                raise ProviderError(
                    "Protein-coding feature filtering removed every gene before stratified DESeq2. "
                    "Check the annotation key column and gene identifiers."
                )
            filter_root = self._prepared_rnaseq_root(
                label=f"{label or 'Stratified DESeq2'} protein-coding filter",
                source_path=resolved_counts_path,
            )
            filtered_counts_file = filter_root / "protein_coding_counts.tsv"
            filtered_counts.to_csv(filtered_counts_file, sep="\t", index=False)
            filtered_counts_path = str(filtered_counts_file)
            prefilter_summary = {
                "applied": True,
                "feature_count_before": int(len(counts_frame.index)),
                "feature_count_after": int(len(filtered_counts.index)),
                "counts_path": filtered_counts_path,
                "annotation_group_value": clean_text(feature_annotation_group_value),
            }

        per_stratum: list[dict[str, Any]] = []
        target_values: list[float] = []
        reference_values: list[float] = []
        for stratum_index, stratum in enumerate(selected_strata):
            stratum_timeout_seconds: int | None = None
            if execution_budget_seconds is not None:
                elapsed_seconds = time.monotonic() - workflow_started_at
                remaining_seconds = execution_budget_seconds - elapsed_seconds
                remaining_strata = len(selected_strata) - stratum_index
                if remaining_seconds < remaining_strata:
                    raise ProviderError(
                        "Stratified DESeq2 exhausted its evaluation budget before all strata could be analyzed.",
                        error_code="tool_timeout",
                        retryable=False,
                        suggested_next_step="Increase the episode tool budget or reduce the requested strata.",
                        diagnostic=(
                            f"remaining_seconds={remaining_seconds:.1f}, remaining_strata={remaining_strata}, "
                            f"completed_strata={stratum_index}"
                        ),
                    )
                # Allocate the remaining budget across unstarted strata. This
                # avoids applying a hidden per-stratum cap that can consume the
                # whole episode before the composite statistic is available.
                stratum_timeout_seconds = max(1, int(remaining_seconds // remaining_strata))
            prepared = self.prepare_rnaseq_inputs(
                counts_path=filtered_counts_path,
                counts_sheet_name=counts_sheet_name,
                counts_header_row=counts_header_row,
                metadata_path=resolved_metadata_path,
                metadata_sheet_name=sample_metadata_sheet_name,
                metadata_header_row=sample_metadata_header_row,
                gene_column=gene_column,
                sample_key_column=sample_key_column,
                condition_column=sex_column,
                sample_filter_column=stratum_column,
                sample_filter_values=[stratum],
                design_columns=[sex_column],
                label=f"{label or 'Stratified DESeq2'}: {stratum}",
            )
            prepared_manifest_path = clean_text(
                (prepared.get("structured_data") or {}).get("prepared_manifest_path")
            )
            analysis = self.analyze_deseq2_counts(
                prepared_manifest_path=prepared_manifest_path,
                species=effective_species,
                label=f"{label or 'M vs F'}: {stratum}",
                comparison=f"{condition_a} vs {condition_b} ({stratum})",
                condition_a=condition_a,
                condition_b=condition_b,
                significance_mode="p_value",
                p_value_threshold=p_value_threshold,
                base_mean_threshold=0.0,
                log2_fold_change_threshold=0.0,
                lfc_shrinkage=lfc_shrinkage,
                execution_timeout_seconds=stratum_timeout_seconds,
            )
            result_path = clean_text((analysis.get("derived_inputs") or {}).get("result_table_path"))
            result_table = load_table(path=result_path, dtype=str, keep_default_na=False)
            rows: list[float] = []
            for row in result_table.frame.to_dict(orient="records"):
                gene = normalize_gene_token(row.get("gene"), species=effective_species)
                if protein_coding_ids is not None and self._canonical_sample_token(gene) not in protein_coding_ids:
                    continue
                try:
                    p_value = float(row.get("p_value"))
                    base_mean = float(row.get("base_mean"))
                    lfc = float(row.get("log2_fold_change"))
                except (TypeError, ValueError):
                    continue
                if not (p_value < float(p_value_threshold)):
                    continue
                if base_mean <= float(base_mean_threshold):
                    continue
                if abs(lfc) <= float(absolute_log2_fold_change_threshold):
                    continue
                rows.append(lfc)
            if stratum in target:
                target_values.extend(rows)
            else:
                reference_values.extend(rows)
            per_stratum.append(
                {
                    "stratum": stratum,
                    "role": "target" if stratum in target else "reference",
                    "filtered_lfc_count": len(rows),
                    "result_path": result_path,
                    "prepared_manifest_path": prepared_manifest_path,
                    "execution_timeout_seconds": stratum_timeout_seconds,
                }
            )

        if not target_values or not reference_values:
            raise ProviderError(
                "Stratified DESeq2 produced no comparable filtered LFC values in one group. "
                f"target_count={len(target_values)}, reference_count={len(reference_values)}."
            )
        test_token = clean_text(test).lower() or "welch"
        if test_token not in {"t", "welch"}:
            raise ProviderError("Stratified DESeq2 currently supports test='t' or test='welch'.")
        statistic, p_value = stats.ttest_ind(
            target_values,
            reference_values,
            equal_var=test_token == "t",
            nan_policy="omit",
        )
        structured_data = {
            "analysis": "stratified_deseq2_lfc_test",
            "provider": "gleskit_stratified_deseq2",
            "input": {
                "stratum_column": stratum_column,
                "sex_column": sex_column,
                "condition_a": condition_a,
                "condition_b": condition_b,
                "target_strata": target,
                "reference_strata": reference,
                "feature_annotation_path": resolved_annotation_path or None,
                "feature_annotation_group_value": feature_annotation_group_value,
                "protein_coding_prefilter": prefilter_summary,
                "p_value_threshold": float(p_value_threshold),
                "base_mean_threshold": float(base_mean_threshold),
                "absolute_log2_fold_change_threshold": float(absolute_log2_fold_change_threshold),
                "lfc_shrinkage": lfc_shrinkage,
                "test": test_token,
                "execution_budget_seconds": execution_budget_seconds,
            },
            "result": {
                "statistic_name": "t",
                "statistic": float(statistic),
                "p_value": float(p_value),
                "target_lfc_count": len(target_values),
                "reference_lfc_count": len(reference_values),
                "target_lfc_mean": float(sum(target_values) / len(target_values)),
                "reference_lfc_mean": float(sum(reference_values) / len(reference_values)),
            },
            "per_stratum": per_stratum,
        }
        summary = (
            f"Compared {len(target_values)} filtered target-stratum LFCs with {len(reference_values)} "
            f"filtered reference-stratum LFCs using a {test_token} t-test."
        )
        workspace_info = self._capture_to_active_session(
            kind="stratified_deseq2",
            title=clean_text(label) or "Stratified DESeq2 LFC test",
            summary=summary,
            provider="gleskit_stratified_deseq2",
            payload=structured_data,
            provenance=structured_data["input"],
        )
        return {
            "summary": summary,
            "provider": "gleskit_stratified_deseq2",
            "structured_data": structured_data,
            "question_ready": structured_data["result"],
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def evaluate_cluster_agreement(
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
        analysis_axis: str = "columns",
        cluster_count: int = 3,
        iterations: int = 50,
        train_fraction: float = 0.7,
        metric: str = "correlation",
        linkage: str = "average",
        random_seed: int = 0,
        label: str = "",
    ) -> dict[str, Any]:
        matrix, resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
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
        result = self.supervised_expression_backend.evaluate_cluster_agreement(
            matrix,
            analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
            cluster_count=cluster_count,
            iterations=iterations,
            train_fraction=train_fraction,
            metric=metric,
            linkage=linkage,
            random_seed=random_seed,
        )
        agreement_block = dict(result.get("agreement") or {})
        evaluation_scope = dict(result.get("evaluation_scope") or {})
        question_ready = {
            "agreement_count": agreement_block.get("count"),
            "agreement_rate": agreement_block.get("rate"),
            "agreement_count_test_only": agreement_block.get("count_test_only"),
            "agreement_rate_test_only": agreement_block.get("rate_test_only"),
            "test_sample_count": agreement_block.get("test_sample_count"),
            "label_transfer_model": evaluation_scope.get("label_transfer_model"),
            "train_count": evaluation_scope.get("train_count"),
            "test_count": evaluation_scope.get("test_count"),
        }
        artifact_payload = {**dict(result), "question_ready": question_ready}
        if _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS"):
            structured_data = {
                "summary": artifact_payload.get("summary"),
                "provider": artifact_payload.get("provider"),
                "analysis": artifact_payload.get("analysis"),
                "input": artifact_payload.get("input"),
                "evaluation_scope": evaluation_scope,
                "agreement": {
                    "count": agreement_block.get("count"),
                    "rate": agreement_block.get("rate"),
                    "count_test_only": agreement_block.get("count_test_only"),
                    "rate_test_only": agreement_block.get("rate_test_only"),
                    "test_sample_count": agreement_block.get("test_sample_count"),
                },
                "train": {
                    "consistency_summary": ((artifact_payload.get("train") or {}) if isinstance(artifact_payload.get("train"), dict) else {}).get("consistency_summary"),
                },
                "test": {
                    "consistency_summary": ((artifact_payload.get("test") or {}) if isinstance(artifact_payload.get("test"), dict) else {}).get("consistency_summary"),
                },
                "question_ready": question_ready,
            }
        else:
            structured_data = artifact_payload
        workspace_info = self._capture_to_active_session(
            kind="cluster_agreement",
            title=clean_text(label) or self._expression_matrix_title("cluster_agreement", source_path=matrix.path),
            summary=str(result.get("summary") or ""),
            provider="supervised_expression",
            payload=artifact_payload,
            provenance={
                "matrix_path": clean_text(resolved_matrix_path) or None,
                "analysis_axis": normalized_analysis_axis,
                "cluster_count": int(cluster_count),
                "iterations": int(iterations),
                "train_fraction": float(train_fraction),
                "metric": clean_text(metric) or "correlation",
                "linkage": clean_text(linkage) or "average",
                "random_seed": int(random_seed),
            },
        )
        return {
            "summary": str(result.get("summary") or ""),
            "provider": "supervised_expression",
            "structured_data": structured_data,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
            "question_ready": question_ready,
            "agreement_count_test_only": question_ready.get("agreement_count_test_only"),
            "agreement_rate_test_only": question_ready.get("agreement_rate_test_only"),
        }

    def run_expression_statistics(
        self,
        *,
        analysis: str = "distribution_summary",
        source_path: str = "",
        table_text: str = "",
        row_id_column: str = "",
        delimiter: str = "",
        sheet_name: str = "",
        header_row: int = 1,
        skip_rows: int = 0,
        transpose: bool = False,
        analysis_axis: str = "columns",
        target_axis: str = "features",
        summary_stat: str = "mean",
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        metadata_key_column: str = "",
        metadata_group_column: str = "",
        metadata_group_value: str = "",
        metadata_group_values: list[str] | None = None,
        sample_group_map: dict[str, str] | None = None,
        group_a: str = "",
        group_b: str = "",
        annotation_path: str = "",
        annotation_table_text: str = "",
        annotation_delimiter: str = "",
        annotation_sheet_name: str = "",
        annotation_header_row: int = 1,
        annotation_skip_rows: int = 0,
        annotation_key_column: str = "",
        annotation_value_column: str = "",
        feature_annotation_path: str = "",
        feature_annotation_table_text: str = "",
        feature_annotation_delimiter: str = "",
        feature_annotation_sheet_name: str = "",
        feature_annotation_header_row: int = 1,
        feature_annotation_skip_rows: int = 0,
        feature_annotation_key_column: str = "",
        feature_annotation_group_column: str = "",
        feature_annotation_group_value: str = "",
        method: str = "pearson",
        value_column: str = "",
        scope: str = "all_values",
        group_column: str = "",
        key_column: str = "",
        pair_column: str = "",
        annotation_group_column: str = "",
        annotation_group_value: str = "",
        test: str = "welch",
        observation_unit: str = "flattened_values",
        value_transform: str = "none",
        threshold: float = 0.0,
        threshold_operator: str = "<",
        pseudocount: float = 1.0,
        success_count: int = 0,
        trial_count: int = 0,
        null_probability: float = 0.5,
        alpha: float = 0.05,
        correction_methods: list[str] | None = None,
        missing_value_policy: str = "error",
        label: str = "",
    ) -> dict[str, Any]:
        normalized_analysis = clean_text(analysis).lower() or "distribution_summary"
        resolved_source_path = self._resolve_input_file_selector(source_path, role="source_path")
        resolved_annotation_path = self._resolve_input_file_selector(annotation_path, role="annotation_path")
        resolved_metadata_path = self._resolve_input_file_selector(metadata_path, role="metadata_path")
        resolved_feature_annotation_path = self._resolve_input_file_selector(
            feature_annotation_path,
            role="feature_annotation_path",
        )
        normalized_analysis_axis = self._normalize_analysis_axis(analysis_axis)
        if normalized_analysis == "matrix_annotation_correlation":
            result = self.expression_statistics_backend.matrix_annotation_correlation(
                matrix_path=resolved_source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                target_axis=target_axis,  # type: ignore[arg-type]
                summary_stat=summary_stat,  # type: ignore[arg-type]
                annotation_path=resolved_annotation_path,
                annotation_table_text=annotation_table_text,
                annotation_delimiter=annotation_delimiter,
                annotation_sheet_name=annotation_sheet_name,
                annotation_header_row=annotation_header_row,
                annotation_skip_rows=annotation_skip_rows,
                annotation_key_column=annotation_key_column,
                annotation_value_column=annotation_value_column,
                annotation_group_column=annotation_group_column,
                annotation_group_value=annotation_group_value or group_column,
                metadata_path=resolved_metadata_path,
                metadata_table_text=metadata_table_text,
                metadata_delimiter=metadata_delimiter,
                metadata_sheet_name=metadata_sheet_name,
                metadata_header_row=metadata_header_row,
                metadata_skip_rows=metadata_skip_rows,
                metadata_key_column=metadata_key_column or key_column,
                metadata_group_column=metadata_group_column,
                metadata_group_value=metadata_group_value,
                method=method,  # type: ignore[arg-type]
            )
        elif normalized_analysis == "distribution_summary":
            result = self.expression_statistics_backend.distribution_summary(
                source_path=resolved_source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                value_column=value_column,
                scope=scope,  # type: ignore[arg-type]
            )
        elif normalized_analysis == "group_test":
            result = self.expression_statistics_backend.group_test_from_table(
                source_path=resolved_source_path,
                table_text=table_text,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                value_column=value_column,
                group_column=group_column,
                key_column=key_column,
                annotation_path=resolved_annotation_path,
                annotation_table_text=annotation_table_text,
                annotation_delimiter=annotation_delimiter,
                annotation_sheet_name=annotation_sheet_name,
                annotation_header_row=annotation_header_row,
                annotation_skip_rows=annotation_skip_rows,
                annotation_key_column=annotation_key_column,
                annotation_group_column=annotation_group_column,
                test=test,  # type: ignore[arg-type]
            )
        elif normalized_analysis == "featurewise_group_test":
            result = self.expression_statistics_backend.featurewise_group_test(
                source_path=resolved_source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                group_column=group_column,
                key_column=key_column,
                pair_column=pair_column,
                annotation_path=resolved_annotation_path,
                annotation_table_text=annotation_table_text,
                annotation_delimiter=annotation_delimiter,
                annotation_sheet_name=annotation_sheet_name,
                annotation_header_row=annotation_header_row,
                annotation_skip_rows=annotation_skip_rows,
                annotation_key_column=annotation_key_column,
                annotation_group_column=annotation_group_column,
                test=test,  # type: ignore[arg-type]
                alpha=alpha,
            )
        elif normalized_analysis == "matrix_featurewise_group_test":
            result = self.expression_statistics_backend.matrix_featurewise_group_test(
                matrix_path=resolved_source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                metadata_path=resolved_metadata_path or resolved_annotation_path,
                metadata_table_text=metadata_table_text or annotation_table_text,
                metadata_delimiter=metadata_delimiter or annotation_delimiter,
                metadata_sheet_name=metadata_sheet_name or annotation_sheet_name,
                metadata_header_row=metadata_header_row if metadata_path or metadata_table_text else annotation_header_row,
                metadata_skip_rows=metadata_skip_rows if metadata_path or metadata_table_text else annotation_skip_rows,
                metadata_key_column=metadata_key_column or key_column or annotation_key_column,
                metadata_group_column=metadata_group_column or group_column,
                sample_group_map=sample_group_map,
                group_a=group_a,
                group_b=group_b,
                feature_annotation_path=resolved_feature_annotation_path,
                feature_annotation_table_text=feature_annotation_table_text,
                feature_annotation_delimiter=feature_annotation_delimiter,
                feature_annotation_sheet_name=feature_annotation_sheet_name,
                feature_annotation_header_row=feature_annotation_header_row,
                feature_annotation_skip_rows=feature_annotation_skip_rows,
                feature_annotation_key_column=feature_annotation_key_column,
                feature_annotation_group_column=feature_annotation_group_column,
                feature_annotation_group_value=feature_annotation_group_value,
                test=test,  # type: ignore[arg-type]
                value_transform=value_transform,  # type: ignore[arg-type]
                missing_value_policy=missing_value_policy,  # type: ignore[arg-type]
                alpha=alpha,
            )
        elif normalized_analysis == "matrix_group_fold_change_summary":
            result = self.expression_statistics_backend.matrix_group_fold_change_summary(
                matrix_path=resolved_source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                metadata_path=resolved_metadata_path or resolved_annotation_path,
                metadata_table_text=metadata_table_text or annotation_table_text,
                metadata_delimiter=metadata_delimiter or annotation_delimiter,
                metadata_sheet_name=metadata_sheet_name or annotation_sheet_name,
                metadata_header_row=metadata_header_row if metadata_path or metadata_table_text else annotation_header_row,
                metadata_skip_rows=metadata_skip_rows if metadata_path or metadata_table_text else annotation_skip_rows,
                metadata_key_column=metadata_key_column or key_column or annotation_key_column,
                metadata_group_column=metadata_group_column or group_column,
                group_a=group_a,
                group_b=group_b,
                feature_annotation_path=resolved_feature_annotation_path,
                feature_annotation_table_text=feature_annotation_table_text,
                feature_annotation_delimiter=feature_annotation_delimiter,
                feature_annotation_sheet_name=feature_annotation_sheet_name,
                feature_annotation_header_row=feature_annotation_header_row,
                feature_annotation_skip_rows=feature_annotation_skip_rows,
                feature_annotation_key_column=feature_annotation_key_column,
                feature_annotation_group_column=feature_annotation_group_column,
                feature_annotation_group_value=feature_annotation_group_value,
                summary_stat=summary_stat,  # type: ignore[arg-type]
            )
        elif normalized_analysis == "matrix_group_test":
            result = self.expression_statistics_backend.matrix_group_test(
                matrix_path=resolved_source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                metadata_path=resolved_metadata_path or resolved_annotation_path,
                metadata_table_text=metadata_table_text or annotation_table_text,
                metadata_delimiter=metadata_delimiter or annotation_delimiter,
                metadata_sheet_name=metadata_sheet_name or annotation_sheet_name,
                metadata_header_row=metadata_header_row if metadata_path or metadata_table_text else annotation_header_row,
                metadata_skip_rows=metadata_skip_rows if metadata_path or metadata_table_text else annotation_skip_rows,
                metadata_key_column=metadata_key_column or key_column or annotation_key_column,
                metadata_group_column=metadata_group_column or group_column,
                metadata_group_values=metadata_group_values,
                sample_group_map=sample_group_map,
                feature_annotation_path=resolved_feature_annotation_path,
                feature_annotation_table_text=feature_annotation_table_text,
                feature_annotation_delimiter=feature_annotation_delimiter,
                feature_annotation_sheet_name=feature_annotation_sheet_name,
                feature_annotation_header_row=feature_annotation_header_row,
                feature_annotation_skip_rows=feature_annotation_skip_rows,
                feature_annotation_key_column=feature_annotation_key_column,
                feature_annotation_group_column=feature_annotation_group_column,
                feature_annotation_group_value=feature_annotation_group_value,
                test=test,  # type: ignore[arg-type]
                observation_unit=observation_unit,  # type: ignore[arg-type]
                value_transform=value_transform,  # type: ignore[arg-type]
                pseudocount=pseudocount,
                alpha=alpha,
            )
        elif normalized_analysis == "threshold_count":
            result = self.expression_statistics_backend.threshold_count(
                source_path=resolved_source_path,
                table_text=table_text,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                value_column=value_column,
                threshold=threshold,
                operator=threshold_operator,
            )
        elif normalized_analysis == "matrix_log2_fold_change_distribution":
            result = self.expression_statistics_backend.matrix_log2_fold_change_distribution(
                matrix_path=resolved_source_path,
                table_text=table_text,
                row_id_column=row_id_column,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                transpose=transpose,
                analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
                metadata_path=resolved_metadata_path or resolved_annotation_path,
                metadata_table_text=metadata_table_text or annotation_table_text,
                metadata_delimiter=metadata_delimiter or annotation_delimiter,
                metadata_sheet_name=metadata_sheet_name or annotation_sheet_name,
                metadata_header_row=metadata_header_row if metadata_path or metadata_table_text else annotation_header_row,
                metadata_skip_rows=metadata_skip_rows if metadata_path or metadata_table_text else annotation_skip_rows,
                metadata_key_column=metadata_key_column or key_column or annotation_key_column,
                metadata_group_column=metadata_group_column or group_column,
                metadata_group_values=metadata_group_values,
                feature_annotation_path=resolved_feature_annotation_path,
                feature_annotation_table_text=feature_annotation_table_text,
                feature_annotation_delimiter=feature_annotation_delimiter,
                feature_annotation_sheet_name=feature_annotation_sheet_name,
                feature_annotation_header_row=feature_annotation_header_row,
                feature_annotation_skip_rows=feature_annotation_skip_rows,
                feature_annotation_key_column=feature_annotation_key_column,
                feature_annotation_group_column=feature_annotation_group_column,
                feature_annotation_group_value=feature_annotation_group_value,
            )
        elif normalized_analysis == "proportion_interval":
            result = self.expression_statistics_backend.proportion_interval(
                success_count=success_count,
                trial_count=trial_count,
                alpha=alpha,
            )
        elif normalized_analysis == "exact_binomial_test":
            result = self.expression_statistics_backend.exact_binomial_test(
                success_count=success_count,
                trial_count=trial_count,
                null_probability=null_probability,
                alpha=alpha,
            )
        elif normalized_analysis == "multiple_testing_summary":
            result = self.expression_statistics_backend.multiple_testing_summary(
                source_path=resolved_source_path,
                table_text=table_text,
                delimiter=delimiter,
                sheet_name=sheet_name,
                header_row=header_row,
                skip_rows=skip_rows,
                p_value_column=value_column,
                alpha=alpha,
                methods=correction_methods,
            )
        else:
            raise ValueError(
                "Unsupported expression statistics analysis. Use matrix_annotation_correlation, "
                "distribution_summary, group_test, featurewise_group_test, matrix_featurewise_group_test, "
                "matrix_group_fold_change_summary, matrix_group_test, matrix_log2_fold_change_distribution, "
                "threshold_count, proportion_interval, exact_binomial_test, or "
                "multiple_testing_summary."
            )

        artifact_kind = f"expression_statistics_{normalized_analysis}"
        artifact_payload = result
        tool_payload = self._compact_expression_statistics_result(result)
        route_warnings: list[str] = []
        if resolved_source_path:
            try:
                source_inspection = inspect_tabular_file(Path(resolved_source_path), role_hint="", provenance={})
                source_type = clean_text(source_inspection.data_type).lower()
            except Exception:
                source_type = ""
            if source_type in {"deseq2_results", "ranked_signature"} and normalized_analysis in {
                "matrix_annotation_correlation",
                "distribution_summary",
                "group_test",
                "featurewise_group_test",
                "matrix_featurewise_group_test",
                "matrix_group_fold_change_summary",
                "matrix_group_test",
                "matrix_log2_fold_change_distribution",
                "threshold_count",
            }:
                route_warnings.append(
                    "The inspected source looks like a DEG/result table rather than a raw matrix/statistics input. "
                    "Use analyze_rnaseq_result or compare_differential_expression_results when the question is really about DE contrasts or DEG overlaps."
                )
        if route_warnings:
            tool_payload["route_warnings"] = route_warnings
        workspace_info = self._capture_to_active_session(
            kind=artifact_kind,
            title=clean_text(label) or self._expression_statistics_title(normalized_analysis),
            summary=str(result.get("summary") or ""),
            provider=str(result.get("provider") or "expression_statistics"),
            payload=artifact_payload,
            provenance={
                "analysis": normalized_analysis,
                **dict(result.get("input") or {}),
            },
        )
        return {
            "summary": str(tool_payload.get("summary") or result.get("summary") or ""),
            "provider": str(result.get("provider") or "expression_statistics"),
            "structured_data": tool_payload,
            "warnings": route_warnings + self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def analyze_single_cell_rnaseq(
        self,
        *,
        analysis: str = "summary",
        h5ad_path: str = "",
        deg_table_path: str = "",
        gene: str = "",
        padj_threshold: float = 0.05,
        gene_column: str = "",
        cell_type_column: str = "",
        padj_column: str = "",
        p_value_column: str = "",
        log2_fold_change_column: str = "",
        sheet_names: list[str] | None = None,
        sample_column: str = "",
        condition_column: str = "",
        cell_type_values: list[str] | None = None,
        layer: str = "",
        use_raw: bool = False,
        obs_preview_limit: int = 10,
        var_preview_limit: int = 10,
        label: str = "",
    ) -> dict[str, Any]:
        normalized_analysis = clean_text(analysis).lower() or "summary"
        resolved_h5ad_path = self._resolve_input_file_selector(h5ad_path, role="h5ad_path")
        resolved_deg_table_path = self._resolve_input_file_selector(deg_table_path, role="deg_table_path")
        if normalized_analysis == "summary":
            result = self.single_cell_backend.summarize_h5ad(
                h5ad_path=resolved_h5ad_path,
                obs_preview_limit=obs_preview_limit,
                var_preview_limit=var_preview_limit,
            )
        elif normalized_analysis == "cell_type_deg_counts":
            result = self.single_cell_backend.cell_type_deg_counts(
                deg_table_path=resolved_deg_table_path,
                padj_threshold=padj_threshold,
                gene_column=gene_column,
                cell_type_column=cell_type_column,
                padj_column=padj_column,
                p_value_column=p_value_column,
                log2_fold_change_column=log2_fold_change_column,
                sheet_names=sheet_names,
            )
        elif normalized_analysis == "gene_cell_type_lookup":
            result = self.single_cell_backend.gene_cell_type_lookup(
                deg_table_path=resolved_deg_table_path,
                gene=gene,
                gene_column=gene_column,
                cell_type_column=cell_type_column,
                padj_column=padj_column,
                p_value_column=p_value_column,
                log2_fold_change_column=log2_fold_change_column,
                padj_threshold=padj_threshold,
                sheet_names=sheet_names,
            )
        elif normalized_analysis == "pseudobulk":
            dataset = self.single_cell_backend.build_pseudobulk_dataset(
                h5ad_path=resolved_h5ad_path,
                sample_column=sample_column,
                condition_column=condition_column,
                cell_type_column=cell_type_column,
                cell_type_values=cell_type_values,
                layer=layer,
                use_raw=use_raw,
            )
            result = self._single_cell_pseudobulk_result(
                h5ad_path=resolved_h5ad_path,
                sample_column=sample_column,
                condition_column=condition_column,
                cell_type_column=cell_type_column,
                cell_type_values=cell_type_values or [],
                layer=layer,
                use_raw=use_raw,
                dataset=dataset,
            )
        else:
            raise ValueError(
                "Unsupported single-cell analysis. Use summary, cell_type_deg_counts, "
                "gene_cell_type_lookup, or pseudobulk."
            )

        artifact_kind = f"single_cell_{normalized_analysis}"
        workspace_info = self._capture_to_active_session(
            kind=artifact_kind,
            title=clean_text(label) or self._single_cell_title(normalized_analysis),
            summary=str(result.get("summary") or ""),
            provider=str(result.get("provider") or "single_cell"),
            payload=result,
            provenance={
                "analysis": normalized_analysis,
                **dict(result.get("input") or {}),
            },
        )
        response = {
            "summary": str(result.get("summary") or ""),
            "provider": str(result.get("provider") or "single_cell"),
            "structured_data": result,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }
        if isinstance(result.get("question_ready"), dict):
            response["question_ready"] = dict(result.get("question_ready") or {})
        return response

    def cluster_marker_analysis(
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
        analysis_axis: str = "columns",
        cluster_labels: dict[str, Any] | list[dict[str, Any]] | list[Any] | None = None,
        cluster_labels_path: str = "",
        top_n: int = 25,
        test: str = "welch",
        padj_threshold: float = 0.05,
        species: str | None = None,
        enrich_top_markers: bool = False,
        enrichment_database: str = "GO_BP",
        enrichment_limit: int = 10,
        label: str = "",
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        resolved_cluster_labels = self._load_cluster_labels_payload(
            cluster_labels=cluster_labels,
            cluster_labels_path=cluster_labels_path,
        )
        result = self.single_cell_backend.cluster_marker_analysis(
            matrix_path=matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=analysis_axis,
            cluster_labels=resolved_cluster_labels,
            top_n=top_n,
            test=test,
            padj_threshold=padj_threshold,
            species=effective_species,
        )

        warnings: list[str] = []
        cluster_enrichment: list[dict[str, Any]] = []
        if enrich_top_markers:
            for row in result.get("cluster_markers", []):
                if not isinstance(row, dict):
                    continue
                cluster_id = str(row.get("cluster") or "").strip()
                significant_marker_genes = normalize_gene_list(
                    (result.get("significant_marker_gene_sets") or {}).get(cluster_id) or [],
                    species=effective_species,
                )
                marker_genes = significant_marker_genes or normalize_gene_list(
                    (result.get("marker_gene_sets") or {}).get(cluster_id) or [],
                    species=effective_species,
                )
                if not marker_genes:
                    continue
                try:
                    payload, provider_name, summary = self._pathway_enrichment_context(
                        marker_genes,
                        species=effective_species,
                        label=f"{clean_text(label) or 'Cluster markers'} cluster {cluster_id}",
                        warnings=warnings,
                        database=enrichment_database,
                    )
                    cluster_enrichment.append(
                        {
                            "cluster": cluster_id,
                            "provider": provider_name,
                            "summary": summary,
                            "structured_data": payload,
                        }
                    )
                except ProviderError as exc:
                    warnings.append(str(exc))

        payload = dict(result)
        payload["species"] = effective_species
        payload["label"] = clean_text(label) or None
        derived_inputs = self._persist_cluster_marker_gene_sets(
            marker_gene_sets=dict(payload.get("marker_gene_sets") or {}),
            label=label,
            source_path=str((payload.get("input") or {}).get("matrix_path") or ""),
        )
        significant_marker_inputs = self._persist_cluster_marker_gene_sets(
            marker_gene_sets=dict(payload.get("significant_marker_gene_sets") or {}),
            label=label,
            source_path=str((payload.get("input") or {}).get("matrix_path") or ""),
            stem_prefix="cluster_markers_significant",
            output_key_prefix="significant_marker_gene_sets",
        )
        derived_inputs.update(significant_marker_inputs)
        payload["derived_inputs"] = derived_inputs
        if cluster_enrichment:
            payload["cluster_enrichment"] = cluster_enrichment
        question_ready = self._build_cluster_marker_question_ready(
            payload=payload,
            cluster_enrichment=cluster_enrichment,
            enrichment_database=enrichment_database,
        )
        payload["question_ready"] = question_ready
        workspace_info = self._capture_to_active_session(
            kind="single_cell_cluster_marker_analysis",
            title=clean_text(label) or self._single_cell_title("cluster_marker_analysis"),
            summary=str(result.get("summary") or ""),
            provider="single_cell",
            payload=payload,
            provenance={
                "species": effective_species,
                "top_n": int(top_n),
                "test": clean_text(test).lower() or "welch",
                "padj_threshold": float(padj_threshold),
                "cluster_labels_path": clean_text(cluster_labels_path) or None,
                "enrich_top_markers": bool(enrich_top_markers),
                "enrichment_database": clean_text(enrichment_database).upper() or "GO_BP",
            },
        )
        structured_data = (
            self._compact_cluster_marker_payload_for_model(payload)
            if _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS")
            else payload
        )
        return {
            "summary": str(result.get("summary") or ""),
            "provider": "single_cell",
            "structured_data": structured_data,
            "derived_inputs": derived_inputs,
            "question_ready": question_ready,
            "warnings": warnings + self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def compare_differential_expression_results(
        self,
        *,
        results: list[dict[str, Any]],
        gene_source: str = "all_significant",
        label: str = "",
    ) -> dict[str, Any]:
        comparison_payload = self._compare_differential_expression_results_payload(
            results=results,
            gene_source=gene_source,
        )
        workspace_info = self._capture_to_active_session(
            kind="differential_expression_comparison",
            title=clean_text(label) or "Differential expression comparison",
            summary=str(comparison_payload.get("summary") or ""),
            provider="gleskit_aggregate",
            payload=comparison_payload,
            provenance={
                "gene_source": clean_text(gene_source) or "all_significant",
                "result_count": len(results or []),
            },
        )
        return {
            "summary": str(comparison_payload.get("summary") or ""),
            "provider": "gleskit_aggregate",
            "structured_data": comparison_payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def run_pipeline(
        self,
        *,
        source: dict[str, Any],
        steps: list[dict[str, Any]] | None = None,
        label: str = "",
    ) -> dict[str, Any]:
        normalized_source = dict(source or {})
        explicit_source_kind = clean_text(normalized_source.get("kind")).lower()
        requested_steps = [dict(step) for step in (steps or []) if isinstance(step, dict)]
        if not requested_steps:
            raise ValueError("Provide at least one pipeline step.")

        def _infer_pipeline_source_kind() -> str:
            source_kind = explicit_source_kind
            if source_kind in {"raw_counts", "counts"}:
                return "raw_counts"
            if source_kind in {"expression_matrix", "matrix"}:
                return "matrix"
            if source_kind in {"single_cell", "scrna", "single_cell_rnaseq"}:
                return "single_cell"
            if source_kind in {"rnaseq_result", "de_table", "rnaseq_result_table"}:
                return "rnaseq_result_table"
            if source_kind == "gene_list":
                return "genes"
            if source_kind == "ranked_signature":
                return "ranked_gene_scores"
            if source_kind:
                return source_kind

            if clean_text(normalized_source.get("h5ad_path") or normalized_source.get("deg_table_path")):
                return "single_cell"
            if clean_text(normalized_source.get("result_path")):
                return "rnaseq_result_table"
            if normalized_source.get("genes") or normalized_source.get("gene_list"):
                return "genes"
            if normalized_source.get("gene_scores") or normalized_source.get("ranked_gene_scores"):
                return "ranked_gene_scores"
            if clean_text(normalized_source.get("query") or normalized_source.get("text_query")):
                return "query"
            if clean_text(
                normalized_source.get("matrix_path")
                or normalized_source.get("counts_path")
                or normalized_source.get("table_text")
            ):
                return "matrix"

            step_tools = {
                clean_text(
                    step.get("tool")
                    or step.get("kind")
                    or step.get("name")
                ).lower()
                for step in requested_steps
                if isinstance(step, dict)
            }
            matrix_step_tools = {
                "inspect_tabular_data",
                "prepare_rnaseq_inputs",
                "run_log_transform",
                "run_expression_pca",
                "run_expression_hierarchical_clustering",
                "run_expression_correlation",
                "run_expression_consensus_clustering",
                "run_supervised_expression_model",
                "evaluate_cluster_agreement",
                "run_expression_statistics",
            }
            gene_step_tools = {
                "gene_summary",
                "interaction_network",
                "pathway_context",
                "gene_disease_associations",
                "investigate_gene_set",
                "enrich_gene_set",
                "gene_set_disease_context",
            }
            if step_tools & matrix_step_tools:
                return "matrix"
            if "prepare_rnaseq_inputs" in step_tools:
                return "raw_counts"
            if "analyze_deseq2_counts" in step_tools:
                return "raw_counts"
            if "analyze_single_cell_rnaseq" in step_tools:
                return "single_cell"
            if "analyze_rnaseq_result" in step_tools:
                return "rnaseq_result_table"
            if "ranked_gene_set_gsea" in step_tools:
                return "ranked_gene_scores"
            if step_tools & gene_step_tools:
                return "genes"
            if "search_papers" in step_tools:
                return "query"
            return ""

        source_kind = _infer_pipeline_source_kind()
        if not source_kind:
            raise ValueError(
                "Pipeline source.kind is required or must be inferable from source fields or the pipeline stages."
            )
        normalized_source["kind"] = source_kind
        source_has_explicit_seed = bool(
            normalized_source.get("genes")
            or normalized_source.get("gene_list")
            or normalized_source.get("gene_scores")
            or normalized_source.get("ranked_gene_scores")
            or clean_text(normalized_source.get("query") or normalized_source.get("text_query"))
            or clean_text(normalized_source.get("h5ad_path") or normalized_source.get("deg_table_path"))
            or clean_text(normalized_source.get("result_path"))
            or clean_text(
                normalized_source.get("matrix_path")
                or normalized_source.get("counts_path")
                or normalized_source.get("table_text")
            )
        )

        def _normalize_step(raw_step: dict[str, Any]) -> tuple[str, dict[str, Any], str, str]:
            step = dict(raw_step)
            step_label = clean_text(step.get("label") or label)
            step_id = clean_text(step.get("id"))
            params: dict[str, Any] = {}
            raw_params = step.get("params")
            if isinstance(raw_params, dict):
                params.update(raw_params)

            reserved_keys = {"tool", "kind", "name", "label", "params", "id"}
            shorthand_key = ""
            tool = clean_text(step.get("tool") or step.get("kind") or step.get("name"))
            if not tool:
                candidate_keys = [key for key in step.keys() if key not in reserved_keys]
                if len(candidate_keys) == 1:
                    candidate_key = candidate_keys[0]
                    candidate_value = step.get(candidate_key)
                    if isinstance(candidate_value, dict):
                        shorthand_key = candidate_key
                        tool = clean_text(candidate_key)
                        params.update(dict(candidate_value))
            extra_params = {
                key: value
                for key, value in step.items()
                if key not in reserved_keys and key != shorthand_key and key != tool
            }
            if extra_params:
                merged = dict(extra_params)
                merged.update(params)
                params = merged
            return tool, params, step_label, step_id

        def _source_matrix_source() -> tuple[dict[str, Any], Any | None]:
            matrix_path = clean_text(normalized_source.get("matrix_path") or normalized_source.get("counts_path"))
            table_text = str(normalized_source.get("table_text") or "")
            if not matrix_path and not clean_text(table_text):
                return {}, None
            matrix, resolved_matrix_path, normalized_analysis_axis = self._load_expression_matrix_input(
                matrix_path=matrix_path,
                table_text=table_text,
                row_id_column=clean_text(normalized_source.get("row_id_column") or ""),
                delimiter=clean_text(normalized_source.get("delimiter") or ""),
                sheet_name=clean_text(normalized_source.get("sheet_name") or ""),
                transpose=bool(normalized_source.get("transpose", False)),
                analysis_axis=clean_text(normalized_source.get("analysis_axis") or "columns") or "columns",
            )
            return {
                "matrix_path": resolved_matrix_path,
                "table_text": table_text,
                "row_id_column": clean_text(normalized_source.get("row_id_column") or ""),
                "delimiter": clean_text(normalized_source.get("delimiter") or ""),
                "sheet_name": clean_text(normalized_source.get("sheet_name") or ""),
                "transpose": bool(normalized_source.get("transpose", False)),
                "analysis_axis": normalized_analysis_axis,
            }, matrix

        source_matrix_info, current_matrix = _source_matrix_source()
        current_state: dict[str, Any]
        current_state = {
            "kind": "matrix" if current_matrix is not None else source_kind,
            "data": current_matrix,
            "source": normalized_source,
            "species": clean_text(normalized_source.get("species") or self.settings.default_species) or self.settings.default_species,
            "matrix_transformed": False,
        }
        if source_kind in {"genes", "gene_list"}:
            genes = normalize_gene_list(
                normalized_source.get("genes") or [],
                species=current_state["species"],
            )
            if not genes and source_has_explicit_seed and explicit_source_kind in {"genes", "gene_list"}:
                raise ValueError("Pipeline source.genes must contain at least one gene.")
            current_state = {
                "kind": "genes",
                "data": genes,
                "source": normalized_source,
                "species": current_state["species"],
            }
        elif source_kind in {"ranked_gene_scores", "ranked_signature"}:
            raw_scores = normalized_source.get("gene_scores") or normalized_source.get("ranked_gene_scores") or []
            normalized_scores: list[dict[str, Any]] = []
            seen: dict[str, float] = {}
            pipeline_species = current_state["species"]
            if isinstance(raw_scores, dict):
                items = raw_scores.items()
                for gene, raw_score in items:
                    token = normalize_gene_token(gene, species=pipeline_species)
                    if not token:
                        continue
                    try:
                        value = float(raw_score)
                    except (TypeError, ValueError):
                        continue
                    current = seen.get(token)
                    if current is None or abs(value) > abs(current):
                        seen[token] = value
            else:
                for row in raw_scores or []:
                    if not isinstance(row, dict):
                        continue
                    token = normalize_gene_token(row.get("gene"), species=pipeline_species)
                    if not token:
                        continue
                    try:
                        value = float(row.get("score"))
                    except (TypeError, ValueError):
                        continue
                    current = seen.get(token)
                    if current is None or abs(value) > abs(current):
                        seen[token] = value
            normalized_scores = [{"gene": gene, "score": score} for gene, score in seen.items()]
            normalized_scores.sort(key=lambda row: abs(float(row["score"])), reverse=True)
            if not normalized_scores and source_has_explicit_seed and explicit_source_kind in {"ranked_signature", "ranked_gene_scores"}:
                raise ValueError("Pipeline source.gene_scores must contain at least one usable score.")
            current_state = {
                "kind": "ranked_gene_scores",
                "data": normalized_scores,
                "source": normalized_source,
                "species": pipeline_species,
            }
        elif source_kind in {"query", "text_query"}:
            query = clean_text(normalized_source.get("query"))
            if not query and source_has_explicit_seed and explicit_source_kind in {"query", "text_query"}:
                raise ValueError("Pipeline source.query must not be empty.")
            current_state = {
                "kind": "query",
                "data": query,
                "source": normalized_source,
                "species": current_state["species"],
            }
        elif source_kind == "single_cell":
            current_state = {
                "kind": "single_cell",
                "data": {
                    "h5ad_path": clean_text(normalized_source.get("h5ad_path")),
                    "deg_table_path": clean_text(normalized_source.get("deg_table_path")),
                },
                "source": normalized_source,
                "species": current_state["species"],
            }
        elif source_kind == "rnaseq_result_table":
            current_state = {
                "kind": "rnaseq_result_table",
                "data": {
                    "result_path": clean_text(normalized_source.get("result_path")),
                    "table_text": str(normalized_source.get("table_text") or ""),
                    "sheet_name": clean_text(normalized_source.get("sheet_name")),
                },
                "source": normalized_source,
                "species": current_state["species"],
            }
        elif current_matrix is None and not (
            clean_text(normalized_source.get("counts_path"))
            or clean_text(normalized_source.get("matrix_path"))
            or clean_text(normalized_source.get("result_path"))
            or clean_text(normalized_source.get("h5ad_path"))
        ):
            raise ValueError(
                "Pipeline source must provide a matrix_path, counts_path, result_path, h5ad_path, genes, ranked_gene_scores, or query."
            )

        def _current_gene_list(*, gene_source: str = "all_significant") -> list[str]:
            kind = str(current_state.get("kind") or "")
            data = current_state.get("data")
            current_species = clean_text(current_state.get("species") or normalized_source.get("species") or self.settings.default_species) or self.settings.default_species
            if kind == "genes":
                return normalize_gene_list(data or [], species=current_species)
            if kind == "gene_summary_result" and isinstance(data, dict):
                gene_payload = data.get("gene") if isinstance(data.get("gene"), dict) else {}
                gene_symbol = clean_text(gene_payload.get("symbol") if isinstance(gene_payload, dict) else "")
                return normalize_gene_list([gene_symbol or clean_text(data.get("query"))], species=current_species)
            if kind == "interaction_network_result" and isinstance(data, dict):
                return normalize_gene_list(
                    data.get("identifiers")
                    or data.get("genes")
                    or data.get("input_genes")
                    or [],
                    species=current_species,
                )
            if kind == "pathway_context_result" and isinstance(data, dict):
                return normalize_gene_list(data.get("genes") or [], species=current_species)
            if kind == "gene_disease_associations_result" and isinstance(data, dict):
                return normalize_gene_list(
                    [
                        data.get("input_gene"),
                        data.get("approved_symbol") or data.get("gene_symbol"),
                    ],
                    species=current_species,
                )
            if kind == "investigate_gene_set_result" and isinstance(data, dict):
                return normalize_gene_list(data.get("resolved_genes") or data.get("input_genes") or [], species=current_species)
            if kind == "expression_context_result" and isinstance(data, dict):
                gene_payload = data.get("gene") if isinstance(data.get("gene"), dict) else {}
                gene_symbol = clean_text(gene_payload.get("symbol") if isinstance(gene_payload, dict) else "")
                return normalize_gene_list([gene_symbol or clean_text(data.get("query"))], species=current_species)
            if kind in {"deseq2_result", "rna_seq_result"} and isinstance(data, dict):
                if gene_source == "upregulated":
                    return normalize_gene_list((data.get("significant_gene_sets") or {}).get("upregulated") or [], species=current_species)
                if gene_source == "downregulated":
                    return normalize_gene_list((data.get("significant_gene_sets") or {}).get("downregulated") or [], species=current_species)
                if gene_source == "top_upregulated":
                    return normalize_gene_list((row.get("gene") for row in (data.get("top_genes") or {}).get("upregulated") or []), species=current_species)
                if gene_source == "top_downregulated":
                    return normalize_gene_list((row.get("gene") for row in (data.get("top_genes") or {}).get("downregulated") or []), species=current_species)
                if gene_source in {"all_significant", "significant"}:
                    return normalize_gene_list((data.get("significant_gene_sets") or {}).get("all_significant") or [], species=current_species)
                raise ValueError(
                    "Unsupported gene_source for a DESeq2 or RNA-seq result. Use upregulated, downregulated, top_upregulated, top_downregulated, or all_significant."
                )
            if kind in {"enrichment_result", "disease_enrichment_result"} and isinstance(data, dict):
                genes = normalize_gene_list(data.get("genes") or data.get("input_genes") or [], species=current_species)
                if genes:
                    return genes
            if kind == "differential_expression_comparison_result" and isinstance(data, dict):
                return normalize_gene_list(data.get("shared_across_all_genes_preview") or [], species=current_species)
            if kind == "ranked_gene_scores" and isinstance(data, list):
                return normalize_gene_list((row.get("gene") for row in data if isinstance(row, dict)), species=current_species)
            raise ValueError("This pipeline step requires a gene list, DESeq2 result, or ranked gene scores.")

        def _current_ranked_scores() -> list[dict[str, Any]]:
            kind = str(current_state.get("kind") or "")
            data = current_state.get("data")
            if kind == "ranked_gene_scores" and isinstance(data, list):
                return [dict(row) for row in data if isinstance(row, dict)]
            if kind in {"deseq2_result", "rna_seq_result"} and isinstance(data, dict):
                return [dict(row) for row in (data.get("ranked_gene_scores") or []) if isinstance(row, dict)]
            raise ValueError("This pipeline step requires ranked gene scores from a ranked signature or DESeq2 result.")

        def _current_query(step_params: dict[str, Any], *, step_label: str = "") -> str:
            explicit_query = clean_text(step_params.get("query"))
            if explicit_query:
                return explicit_query
            kind = str(current_state.get("kind") or "")
            data = current_state.get("data")
            current_species = clean_text(current_state.get("species") or normalized_source.get("species") or self.settings.default_species) or self.settings.default_species
            genes: list[str] = []
            terms: list[str] = []
            if kind == "query":
                return clean_text(data) or ""
            if kind == "genes":
                genes = normalize_gene_list(data or [], species=current_species)
            elif kind in {
                "gene_summary_result",
                "interaction_network_result",
                "pathway_context_result",
                "gene_disease_associations_result",
                "investigate_gene_set_result",
                "expression_context_result",
            } and isinstance(data, dict):
                genes = _current_gene_list()
                if kind == "gene_summary_result":
                    gene_payload = data.get("gene") if isinstance(data.get("gene"), dict) else {}
                    genes = normalize_gene_list([clean_text(gene_payload.get("symbol") if isinstance(gene_payload, dict) else "") or clean_text(data.get("query"))], species=current_species)
                elif kind == "interaction_network_result":
                    genes = normalize_gene_list(
                        data.get("identifiers")
                        or data.get("genes")
                        or data.get("input_genes")
                        or [],
                        species=current_species,
                    )
                elif kind == "pathway_context_result":
                    genes = normalize_gene_list(data.get("genes") or [], species=current_species)
                elif kind == "gene_disease_associations_result":
                    genes = normalize_gene_list(
                        [
                            data.get("input_gene"),
                            data.get("approved_symbol") or data.get("gene_symbol"),
                        ],
                        species=current_species,
                    )
                elif kind == "investigate_gene_set_result":
                    genes = normalize_gene_list(data.get("resolved_genes") or data.get("input_genes") or [], species=current_species)
                elif kind == "expression_context_result":
                    gene_payload = data.get("gene") if isinstance(data.get("gene"), dict) else {}
                    genes = normalize_gene_list([clean_text(gene_payload.get("symbol") if isinstance(gene_payload, dict) else "") or clean_text(data.get("query"))], species=current_species)
            elif kind in {"deseq2_result", "rna_seq_result"} and isinstance(data, dict):
                genes = normalize_gene_list((data.get("significant_gene_sets") or {}).get("all_significant") or [], species=current_species)
                if not genes:
                    genes = normalize_gene_list((row.get("gene") for row in (data.get("top_genes") or {}).get("upregulated") or []), species=current_species)
                    genes.extend(normalize_gene_list((row.get("gene") for row in (data.get("top_genes") or {}).get("downregulated") or []), species=current_species))
                terms = [
                    str(row.get("term_name") or "")
                    for row in (
                        [row for row in ((data.get("enrichment") or {}).get("upregulated") or {}).get("terms") or [] if isinstance(row, dict)]
                        + [row for row in ((data.get("enrichment") or {}).get("downregulated") or {}).get("terms") or [] if isinstance(row, dict)]
                    )
                ]
            elif kind in {"enrichment_result", "disease_enrichment_result"} and isinstance(data, dict):
                genes = normalize_gene_list(data.get("genes") or data.get("input_genes") or [], species=current_species)
                terms = [str(row.get("term_name") or "") for row in (data.get("terms") or []) if isinstance(row, dict)]
            elif kind == "differential_expression_comparison_result" and isinstance(data, dict):
                genes = normalize_gene_list(data.get("shared_across_all_genes_preview") or [], species=current_species)
            elif kind == "ranked_gene_scores" and isinstance(data, list):
                genes = normalize_gene_list((row.get("gene") for row in data if isinstance(row, dict)), species=current_species)
            if not genes and not terms:
                raise ValueError(
                    "This pipeline step needs an explicit query or a prior gene-based result from which a query can be built."
                )
            return build_literature_query(
                genes=genes[:3],
                terms=[term for term in terms[:4] if term],
                question=clean_text(step_label) or clean_text(normalized_source.get("label")) or "GLESKit pipeline literature follow-up",
            )

        def _step_artifact_ids(result: dict[str, Any]) -> list[str]:
            workspace = dict(result.get("workspace") or {})
            artifact_ids = list(workspace.get("artifact_ids") or [])
            if artifact_ids:
                return [str(artifact_id) for artifact_id in artifact_ids if str(artifact_id).strip()]
            artifacts = result.get("artifacts")
            if isinstance(artifacts, dict):
                ids = [str(item.get("artifact_id") or "").strip() for item in artifacts.values() if isinstance(item, dict)]
                return [artifact_id for artifact_id in ids if artifact_id]
            if isinstance(artifacts, list):
                ids = [str(item.get("artifact_id") or "").strip() for item in artifacts if isinstance(item, dict)]
                return [artifact_id for artifact_id in ids if artifact_id]
            return []

        def _step_record(tool: str, result: dict[str, Any]) -> dict[str, Any]:
            artifact_ids = _step_artifact_ids(result)
            return {
                "tool": tool,
                "summary": str(result.get("summary") or ""),
                "provider": str(result.get("provider") or ""),
                "artifact_ids": artifact_ids,
                "warning_count": len(result.get("warnings") or []),
            }

        step_records: list[dict[str, Any]] = []
        warnings: list[str] = []
        flat_artifact_ids: list[str] = []
        step_outputs: dict[str, dict[str, Any]] = {}

        with ExitStack() as pipeline_stack:
            for raw_step in requested_steps:
                tool, params, step_label, step_id = _normalize_step(raw_step)
                if not tool:
                    raise ValueError("Each pipeline step must declare a supported tool name.")
                if step_id and step_id in step_outputs:
                    raise ValueError(f"Pipeline step id '{step_id}' is duplicated.")

                if tool == "run_log_transform":
                    if not current_matrix:
                        raise ValueError("run_log_transform requires a matrix source.")
                    transformed_matrix, result = self.expression_matrix_backend.run_log_transform(
                        current_matrix,
                        base=float(params.get("base", 2.0)),
                        offset=float(params.get("offset", 1.0)),
                        preset=clean_text(params.get("preset", "")),
                    )
                    captured = self._capture_expression_matrix_analysis(
                        step_name="log_transform",
                        result=result,
                        label=step_label,
                        provenance={
                            **dict(result.get("input") or {}),
                            "base": float(params.get("base", 2.0)),
                            "offset": float(params.get("offset", 1.0)),
                            "preset": clean_text(params.get("preset", "")) or "custom",
                            "step": "run_log_transform",
                        },
                        artifact_kind="log_transform",
                    )
                    current_matrix = transformed_matrix
                    current_state = {
                        "kind": "matrix",
                        "data": transformed_matrix,
                        "source": normalized_source,
                        "matrix_transformed": True,
                    }
                    result = captured
                elif tool in {
                    "run_expression_pca",
                    "run_expression_hierarchical_clustering",
                    "run_expression_correlation",
                    "run_expression_consensus_clustering",
                }:
                    if not current_matrix:
                        raise ValueError(f"{tool} requires a current matrix source in the pipeline.")
                    if tool == "run_expression_correlation":
                        secondary_matrix = None
                        secondary_source = params.get("secondary_source")
                        secondary_matrix_path = clean_text(params.get("secondary_matrix_path", ""))
                        secondary_table_text = str(params.get("secondary_table_text") or "")
                        if isinstance(secondary_source, dict):
                            secondary_matrix, _resolved_secondary_path, _normalized_secondary_axis = self._load_expression_matrix_input(
                                matrix_path=clean_text(secondary_source.get("matrix_path") or secondary_source.get("counts_path")),
                                table_text=str(secondary_source.get("table_text") or ""),
                                row_id_column=clean_text(secondary_source.get("row_id_column") or ""),
                                delimiter=clean_text(secondary_source.get("delimiter") or ""),
                                sheet_name=clean_text(secondary_source.get("sheet_name") or ""),
                                transpose=bool(secondary_source.get("transpose", False)),
                                analysis_axis=clean_text(secondary_source.get("analysis_axis") or "columns") or "columns",
                                role="secondary_matrix_path",
                            )
                        elif secondary_matrix_path or clean_text(secondary_table_text) or clean_text(params.get("secondary_value_column", "")):
                            secondary_matrix, _resolved_secondary_path, _normalized_secondary_axis = self._load_expression_matrix_input(
                                matrix_path=secondary_matrix_path,
                                table_text=secondary_table_text,
                                row_id_column=clean_text(params.get("secondary_row_id_column", "")),
                                delimiter=clean_text(params.get("secondary_delimiter", "")),
                                sheet_name=clean_text(params.get("secondary_sheet_name", "")),
                                transpose=bool(params.get("secondary_transpose", False)),
                                analysis_axis=clean_text(params.get("secondary_analysis_axis", "columns")) or "columns",
                                role="secondary_matrix_path",
                            )
                        result = self._run_expression_matrix_step(
                            step_name="correlation",
                            matrix=current_matrix,
                            analysis_axis=clean_text(params.get("analysis_axis", "columns")) or "columns",
                            label=step_label,
                            method=clean_text(params.get("method", "pearson")) or "pearson",
                            secondary_matrix=secondary_matrix,
                            secondary_value_column=clean_text(params.get("secondary_value_column", "")),
                            secondary_analysis_axis=clean_text(params.get("secondary_analysis_axis", "columns")) or "columns",
                            top_n=int(params.get("top_n", 25)),
                        )
                    elif tool == "run_expression_pca":
                        result = self._run_expression_matrix_step(
                            step_name="pca",
                            matrix=current_matrix,
                            analysis_axis=clean_text(params.get("analysis_axis", "columns")) or "columns",
                            label=step_label,
                            n_components=int(params.get("n_components", 2)),
                            scale=bool(params.get("scale", True)),
                            top_n_loadings=int(params.get("top_n_loadings", 15)),
                            force=bool(params.get("force", False)),
                        )
                    elif tool == "run_expression_hierarchical_clustering":
                        result = self._run_expression_matrix_step(
                            step_name="hierarchical_clustering",
                            matrix=current_matrix,
                            analysis_axis=clean_text(params.get("analysis_axis", "columns")) or "columns",
                            label=step_label,
                            cluster_count=int(params.get("cluster_count", 3)),
                            linkage=clean_text(params.get("linkage", "average")) or "average",
                            metric=clean_text(params.get("metric", "correlation")) or "correlation",
                        )
                    else:
                        result = self._run_expression_matrix_step(
                            step_name="consensus_clustering",
                            matrix=current_matrix,
                            analysis_axis=self._normalize_analysis_axis(clean_text(params.get("analysis_axis", "columns")) or "columns"),
                            label=step_label,
                            cluster_count=int(params.get("cluster_count", 3)),
                            iterations=int(params.get("iterations", 50)),
                            sample_fraction=float(params.get("sample_fraction", 0.7)),
                            feature_fraction=float(params.get("feature_fraction", 1.0)),
                            random_seed=int(params.get("random_seed", 0)),
                            metric=clean_text(params.get("metric", "correlation")) or "correlation",
                            linkage=clean_text(params.get("linkage", "average")) or "average",
                        )
                elif tool == "run_supervised_expression_model":
                    if not current_matrix:
                        raise ValueError("run_supervised_expression_model requires a current matrix source in the pipeline.")
                    result = self.run_supervised_expression_model(
                        table_text=current_matrix.source_text,
                        row_id_column=current_matrix.row_id_column,
                        delimiter=current_matrix.delimiter,
                        analysis_axis=self._normalize_analysis_axis(clean_text(params.get("analysis_axis", current_matrix.analysis_axis_default)) or "columns"),
                        transpose=False,
                        metadata_path=clean_text(params.get("metadata_path") or normalized_source.get("metadata_path")),
                        metadata_table_text=str(params.get("metadata_table_text") or normalized_source.get("metadata_table_text") or ""),
                        metadata_delimiter=clean_text(params.get("metadata_delimiter") or normalized_source.get("metadata_delimiter")),
                        metadata_sheet_name=clean_text(params.get("metadata_sheet_name") or normalized_source.get("metadata_sheet_name")),
                        sample_column=clean_text(params.get("sample_column") or normalized_source.get("sample_column") or "sample") or "sample",
                        target_column=clean_text(params.get("target_column") or normalized_source.get("target_column")),
                        task=clean_text(params.get("task") or normalized_source.get("task") or "classification") or "classification",
                        model_family=clean_text(params.get("model_family") or normalized_source.get("model_family") or "logistic_regression") or "logistic_regression",
                        split_strategy=clean_text(params.get("split_strategy") or normalized_source.get("split_strategy") or "train_test") or "train_test",
                        group_column=clean_text(params.get("group_column") or normalized_source.get("group_column")),
                        test_size=float(params.get("test_size", normalized_source.get("test_size", 0.3))),
                        random_seed=int(params.get("random_seed", normalized_source.get("random_seed", 0))),
                        top_n_features=int(params.get("top_n_features", normalized_source.get("top_n_features", 25))),
                        label=step_label,
                    )
                    current_state = {
                        "kind": "supervised_expression_model_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "evaluate_cluster_agreement":
                    if not current_matrix:
                        raise ValueError("evaluate_cluster_agreement requires a current matrix source in the pipeline.")
                    result = self.evaluate_cluster_agreement(
                        table_text=current_matrix.source_text,
                        row_id_column=current_matrix.row_id_column,
                        delimiter=current_matrix.delimiter,
                        analysis_axis=self._normalize_analysis_axis(clean_text(params.get("analysis_axis", current_matrix.analysis_axis_default)) or "columns"),
                        transpose=False,
                        cluster_count=int(params.get("cluster_count", 3)),
                        iterations=int(params.get("iterations", 50)),
                        train_fraction=float(params.get("train_fraction", 0.7)),
                        metric=clean_text(params.get("metric") or "correlation") or "correlation",
                        linkage=clean_text(params.get("linkage") or "average") or "average",
                        random_seed=int(params.get("random_seed", 0)),
                        label=step_label,
                    )
                    current_state = {
                        "kind": "cluster_agreement_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "inspect_tabular_data":
                    inspection_path = clean_text(
                        params.get("path")
                        or normalized_source.get("path")
                        or normalized_source.get("matrix_path")
                        or normalized_source.get("counts_path")
                        or normalized_source.get("result_path")
                    )
                    if not inspection_path:
                        raise ValueError("inspect_tabular_data requires path in the source or step params.")
                    result = self.inspect_tabular_data(
                        path=inspection_path,
                        sheet_name=clean_text(params.get("sheet_name") or normalized_source.get("sheet_name")),
                        sheet_names=params.get("sheet_names") if isinstance(params.get("sheet_names"), list) else None,
                        role_hint=clean_text(params.get("role_hint") or ""),
                        secondary_path=clean_text(
                            params.get("secondary_path")
                            or normalized_source.get("sample_metadata_path")
                            or normalized_source.get("metadata_path")
                        ),
                        secondary_sheet_name=clean_text(
                            params.get("secondary_sheet_name")
                            or normalized_source.get("sample_metadata_sheet_name")
                            or normalized_source.get("metadata_sheet_name")
                        ),
                        secondary_role_hint=clean_text(params.get("secondary_role_hint") or ""),
                    )
                    current_state = {
                        "kind": "tabular_inspection_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "prepare_rnaseq_inputs":
                    result = self.prepare_rnaseq_inputs(
                        counts_path=clean_text(
                            params.get("counts_path")
                            or normalized_source.get("counts_path")
                            or normalized_source.get("matrix_path")
                        ),
                        source_path=clean_text(params.get("source_path") or ""),
                        counts_sheet_name=clean_text(
                            params.get("counts_sheet_name") or normalized_source.get("counts_sheet_name")
                        ),
                        counts_header_row=int(params.get("counts_header_row", normalized_source.get("counts_header_row", 1))),
                        counts_skip_rows=int(params.get("counts_skip_rows", normalized_source.get("counts_skip_rows", 0))),
                        metadata_path=clean_text(
                            params.get("metadata_path")
                            or normalized_source.get("sample_metadata_path")
                            or normalized_source.get("metadata_path")
                        ),
                        metadata_sheet_name=clean_text(
                            params.get("metadata_sheet_name")
                            or normalized_source.get("sample_metadata_sheet_name")
                            or normalized_source.get("metadata_sheet_name")
                        ),
                        metadata_header_row=int(params.get("metadata_header_row", normalized_source.get("metadata_header_row", 1))),
                        metadata_skip_rows=int(params.get("metadata_skip_rows", normalized_source.get("metadata_skip_rows", 0))),
                        gene_column=clean_text(params.get("gene_column") or normalized_source.get("gene_column")),
                        sample_key_column=clean_text(
                            params.get("sample_key_column")
                            or normalized_source.get("sample_key_column")
                            or normalized_source.get("sample_column")
                        ),
                        condition_column=clean_text(
                            params.get("condition_column") or normalized_source.get("condition_column")
                        ),
                        sample_filter_column=clean_text(params.get("sample_filter_column") or ""),
                        sample_filter_values=params.get("sample_filter_values")
                        if isinstance(params.get("sample_filter_values"), list)
                        else None,
                        design_columns=params.get("design_columns")
                        if isinstance(params.get("design_columns"), list)
                        else None,
                        count_transform=clean_text(params.get("count_transform") or "none") or "none",
                        label=step_label,
                    )
                    current_state = {
                        "kind": "prepared_rnaseq",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "run_expression_statistics":
                    statistics_source_path = clean_text(params.get("source_path"))
                    statistics_table_text = str(params.get("table_text") or "")
                    statistics_row_id_column = clean_text(params.get("row_id_column"))
                    statistics_delimiter = clean_text(params.get("delimiter"))
                    statistics_sheet_name = clean_text(params.get("sheet_name"))
                    statistics_analysis_axis = clean_text(params.get("analysis_axis") or "columns") or "columns"
                    if not statistics_source_path and not clean_text(statistics_table_text) and current_matrix is not None:
                        statistics_table_text = current_matrix.source_text
                        statistics_row_id_column = statistics_row_id_column or current_matrix.row_id_column
                        statistics_delimiter = statistics_delimiter or current_matrix.delimiter
                        statistics_analysis_axis = clean_text(params.get("analysis_axis", current_matrix.analysis_axis_default)) or "columns"
                    result = self.run_expression_statistics(
                        analysis=clean_text(params.get("analysis") or "distribution_summary") or "distribution_summary",
                        source_path=statistics_source_path,
                        table_text=statistics_table_text,
                        row_id_column=statistics_row_id_column,
                        delimiter=statistics_delimiter,
                        sheet_name=statistics_sheet_name,
                        transpose=bool(params.get("transpose", False)),
                        analysis_axis=statistics_analysis_axis,
                        target_axis=clean_text(params.get("target_axis") or "features") or "features",
                        summary_stat=clean_text(params.get("summary_stat") or "mean") or "mean",
                        annotation_path=clean_text(params.get("annotation_path")),
                        annotation_table_text=str(params.get("annotation_table_text") or ""),
                        annotation_delimiter=clean_text(params.get("annotation_delimiter")),
                        annotation_sheet_name=clean_text(params.get("annotation_sheet_name")),
                        annotation_key_column=clean_text(params.get("annotation_key_column")),
                        annotation_value_column=clean_text(params.get("annotation_value_column")),
                        method=clean_text(params.get("method") or "pearson") or "pearson",
                        value_column=clean_text(params.get("value_column")),
                        scope=clean_text(params.get("scope") or "all_values") or "all_values",
                        group_column=clean_text(params.get("group_column")),
                        key_column=clean_text(params.get("key_column")),
                        annotation_group_column=clean_text(params.get("annotation_group_column")),
                        test=clean_text(params.get("test") or "welch") or "welch",
                        success_count=int(params.get("success_count", 0)),
                        trial_count=int(params.get("trial_count", 0)),
                        alpha=float(params.get("alpha", 0.05)),
                        correction_methods=[str(item) for item in params.get("correction_methods", [])] if isinstance(params.get("correction_methods"), list) else None,
                        label=step_label,
                    )
                    current_state = {
                        "kind": "expression_statistics_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                        "matrix_transformed": bool(current_state.get("matrix_transformed", False)),
                    }
                elif tool == "analyze_single_cell_rnaseq":
                    single_cell_analysis = clean_text(params.get("analysis") or normalized_source.get("analysis") or "summary") or "summary"
                    h5ad_path = clean_text(params.get("h5ad_path") or normalized_source.get("h5ad_path"))
                    deg_table_path = clean_text(params.get("deg_table_path") or normalized_source.get("deg_table_path"))
                    result = self.analyze_single_cell_rnaseq(
                        analysis=single_cell_analysis,
                        h5ad_path=h5ad_path,
                        deg_table_path=deg_table_path,
                        gene=clean_text(params.get("gene")),
                        padj_threshold=float(params.get("padj_threshold", 0.05)),
                        gene_column=clean_text(params.get("gene_column")),
                        cell_type_column=clean_text(params.get("cell_type_column")),
                        padj_column=clean_text(params.get("padj_column")),
                        p_value_column=clean_text(params.get("p_value_column")),
                        log2_fold_change_column=clean_text(params.get("log2_fold_change_column")),
                        sheet_names=[str(item) for item in params.get("sheet_names", [])] if isinstance(params.get("sheet_names"), list) else None,
                        sample_column=clean_text(params.get("sample_column")),
                        condition_column=clean_text(params.get("condition_column")),
                        cell_type_values=[str(item) for item in params.get("cell_type_values", [])] if isinstance(params.get("cell_type_values"), list) else None,
                        layer=clean_text(params.get("layer")),
                        use_raw=bool(params.get("use_raw", False)),
                        obs_preview_limit=int(params.get("obs_preview_limit", 10)),
                        var_preview_limit=int(params.get("var_preview_limit", 10)),
                        label=step_label,
                    )
                    if single_cell_analysis == "pseudobulk":
                        dataset = self.single_cell_backend.build_pseudobulk_dataset(
                            h5ad_path=h5ad_path,
                            sample_column=clean_text(params.get("sample_column")),
                            condition_column=clean_text(params.get("condition_column")),
                            cell_type_column=clean_text(params.get("cell_type_column")),
                            cell_type_values=[str(item) for item in params.get("cell_type_values", [])] if isinstance(params.get("cell_type_values"), list) else None,
                            layer=clean_text(params.get("layer")),
                            use_raw=bool(params.get("use_raw", False)),
                        )
                        current_state = {
                            "kind": "pseudobulk",
                            "data": dataset,
                            "source": normalized_source,
                            "matrix_transformed": False,
                        }
                        current_matrix = None
                    else:
                        current_state = {
                            "kind": "single_cell_result",
                            "data": dict(result.get("structured_data") or {}),
                            "source": normalized_source,
                        }
                elif tool == "gene_summary":
                    query = clean_text(params.get("query") or params.get("gene") or params.get("symbol"))
                    if not query:
                        gene_candidates = _current_gene_list()
                        query = gene_candidates[0] if gene_candidates else ""
                    if not query:
                        raise ValueError("gene_summary requires a query or gene symbol.")
                    result = self.gene_summary(
                        query=query,
                        species=clean_text(params.get("species") or normalized_source.get("species") or self.settings.default_species) or self.settings.default_species,
                    )
                    current_state = {
                        "kind": "gene_summary_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "interaction_network":
                    step_species = clean_text(params.get("species") or normalized_source.get("species") or current_state.get("species") or self.settings.default_species) or self.settings.default_species
                    genes = normalize_gene_list(
                        params.get("genes") or params.get("gene_list") or _current_gene_list(),
                        species=step_species,
                    )
                    if not genes:
                        raise ValueError("interaction_network requires at least one gene.")
                    result = self.interaction_network(
                        genes=genes,
                        species=step_species,
                        limit=int(params.get("limit", normalized_source.get("limit", 25))),
                    )
                    current_state = {
                        "kind": "interaction_network_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "pathway_context":
                    step_species = clean_text(params.get("species") or normalized_source.get("species") or current_state.get("species") or self.settings.default_species) or self.settings.default_species
                    genes = normalize_gene_list(
                        params.get("genes") or params.get("gene_list") or _current_gene_list(),
                        species=step_species,
                    )
                    if not genes:
                        raise ValueError("pathway_context requires at least one gene.")
                    result = self.pathway_context(
                        genes=genes,
                        species=step_species,
                        limit=int(params.get("limit", normalized_source.get("limit", 5))),
                    )
                    current_state = {
                        "kind": "pathway_context_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "gene_disease_associations":
                    gene = clean_text(params.get("gene") or params.get("query") or "")
                    if not gene:
                        gene_candidates = _current_gene_list()
                        gene = gene_candidates[0] if gene_candidates else ""
                    if not gene:
                        raise ValueError("gene_disease_associations requires a gene.")
                    result = self.gene_disease_associations(
                        gene=gene,
                        species=clean_text(params.get("species") or normalized_source.get("species") or self.settings.default_species) or self.settings.default_species,
                        limit=int(params.get("limit", normalized_source.get("limit", 10))),
                    )
                    current_state = {
                        "kind": "gene_disease_associations_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "investigate_gene_set":
                    step_species = clean_text(params.get("species") or normalized_source.get("species") or current_state.get("species") or self.settings.default_species) or self.settings.default_species
                    genes = normalize_gene_list(params.get("genes") or _current_gene_list(), species=step_species)
                    if not genes:
                        raise ValueError("investigate_gene_set requires at least one gene.")
                    result = self.investigate_gene_set(
                        genes=genes,
                        question=clean_text(params.get("question") or normalized_source.get("question") or ""),
                        label=step_label,
                        species=step_species,
                    )
                    current_state = {
                        "kind": "investigate_gene_set_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "analyze_rnaseq_result":
                    result_path = clean_text(params.get("result_path") or normalized_source.get("result_path"))
                    result_table_text = str(params.get("table_text") or normalized_source.get("table_text") or "")
                    if not result_path and not clean_text(result_table_text):
                        raise ValueError("analyze_rnaseq_result requires result_path or table_text.")
                    result = self.analyze_rnaseq_result(
                        result_path=result_path,
                        table_text=result_table_text,
                        sheet_name=clean_text(params.get("sheet_name") or normalized_source.get("sheet_name")),
                        species=clean_text(params.get("species") or normalized_source.get("species") or self.settings.default_species) or self.settings.default_species,
                        label=step_label,
                        comparison=clean_text(params.get("comparison") or normalized_source.get("comparison")),
                        gene_column=clean_text(params.get("gene_column") or normalized_source.get("gene_column")),
                        log2_fold_change_column=clean_text(params.get("log2_fold_change_column") or normalized_source.get("log2_fold_change_column")),
                        padj_column=clean_text(params.get("padj_column") or normalized_source.get("padj_column")),
                        p_value_column=clean_text(params.get("p_value_column") or normalized_source.get("p_value_column")),
                        base_mean_column=clean_text(params.get("base_mean_column") or normalized_source.get("base_mean_column")),
                        stat_column=clean_text(params.get("stat_column") or normalized_source.get("stat_column")),
                        padj_threshold=float(params.get("padj_threshold", normalized_source.get("padj_threshold", 0.05))),
                        p_value_threshold=float(params.get("p_value_threshold", normalized_source.get("p_value_threshold", 0.05))),
                        base_mean_threshold=float(params.get("base_mean_threshold", normalized_source.get("base_mean_threshold", 0.0))),
                        log2_fold_change_threshold=float(params.get("log2_fold_change_threshold", normalized_source.get("log2_fold_change_threshold", 0.0))),
                        top_n=int(params.get("top_n", normalized_source.get("top_n", 25))),
                    )
                    current_state = {
                        "kind": "rna_seq_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "analyze_deseq2_counts":
                    if current_state.get("matrix_transformed"):
                        raise ProviderError("DESeq2 requires raw counts; log-transformed matrices cannot be used as input.")
                    counts_sheet_name = clean_text(params.get("counts_sheet_name") or normalized_source.get("counts_sheet_name"))
                    sample_metadata_sheet_name = clean_text(params.get("sample_metadata_sheet_name") or normalized_source.get("sample_metadata_sheet_name"))
                    sample_metadata_path = clean_text(params.get("sample_metadata_path") or normalized_source.get("sample_metadata_path"))
                    sample_metadata_text = str(params.get("sample_metadata_text") or normalized_source.get("sample_metadata_text") or "")
                    counts_path = clean_text(params.get("counts_path") or normalized_source.get("counts_path") or normalized_source.get("matrix_path"))
                    prepared_manifest_path = clean_text(params.get("prepared_manifest_path") or "")
                    if current_state.get("kind") == "pseudobulk":
                        dataset = current_state.get("data")
                        counts_frame = dataset.counts.reset_index().rename(columns={"index": "gene"})
                        counts_path = pipeline_stack.enter_context(TemporaryDelimitedTable(counts_frame))
                        sample_metadata_path = pipeline_stack.enter_context(TemporaryDelimitedTable(dataset.metadata))
                        sample_metadata_text = ""
                        counts_sheet_name = ""
                        sample_metadata_sheet_name = ""
                    if current_state.get("kind") == "prepared_rnaseq" and isinstance(current_state.get("data"), dict):
                        prepared_manifest_path = clean_text(
                            prepared_manifest_path or (current_state.get("data") or {}).get("prepared_manifest_path")
                        )
                    if not counts_path and not prepared_manifest_path:
                        raise ValueError(
                            "analyze_deseq2_counts requires counts_path in the source or step params, or a prepared_manifest_path."
                        )
                    result = self.analyze_deseq2_counts(
                        counts_path=counts_path,
                        prepared_manifest_path=prepared_manifest_path,
                        counts_sheet_name=counts_sheet_name,
                        sample_metadata_path=sample_metadata_path,
                        sample_metadata_sheet_name=sample_metadata_sheet_name,
                        sample_metadata_text=sample_metadata_text,
                        species=clean_text(params.get("species") or normalized_source.get("species") or self.settings.default_species) or self.settings.default_species,
                        label=step_label,
                        comparison=clean_text(params.get("comparison") or normalized_source.get("comparison")),
                        gene_column=clean_text(params.get("gene_column") or normalized_source.get("gene_column")),
                        sample_column=clean_text(params.get("sample_column") or normalized_source.get("sample_column") or "sample") or "sample",
                        condition_column=clean_text(params.get("condition_column") or normalized_source.get("condition_column") or "condition") or "condition",
                        condition_a=clean_text(params.get("condition_a") or normalized_source.get("condition_a")),
                        condition_b=clean_text(params.get("condition_b") or normalized_source.get("condition_b")),
                        design_formula=clean_text(params.get("design_formula") or normalized_source.get("design_formula")),
                        min_count=int(params.get("min_count", normalized_source.get("min_count", 10))),
                        top_n=int(params.get("top_n", normalized_source.get("top_n", 25))),
                        padj_threshold=float(params.get("padj_threshold", normalized_source.get("padj_threshold", 0.05))),
                        p_value_threshold=float(params.get("p_value_threshold", normalized_source.get("p_value_threshold", 0.05))),
                        base_mean_threshold=float(params.get("base_mean_threshold", normalized_source.get("base_mean_threshold", 0.0))),
                        significance_mode=clean_text(params.get("significance_mode") or normalized_source.get("significance_mode") or "padj") or "padj",
                        log2_fold_change_threshold=float(
                            params.get("log2_fold_change_threshold", normalized_source.get("log2_fold_change_threshold", 0.0))
                        ),
                        lfc_shrinkage=clean_text(params.get("lfc_shrinkage") or normalized_source.get("lfc_shrinkage") or "none") or "none",
                    )
                    current_state = {
                        "kind": "deseq2_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "compare_differential_expression_results":
                    comparison_inputs: list[dict[str, Any]] = []
                    from_steps = params.get("from_steps") or []
                    if from_steps:
                        if not isinstance(from_steps, list):
                            raise ValueError("compare_differential_expression_results from_steps must be a list of step ids.")
                        for ref in from_steps:
                            ref_id = clean_text(ref)
                            if not ref_id or ref_id not in step_outputs:
                                raise ValueError(f"Pipeline step reference '{ref_id}' was not found.")
                            referenced = step_outputs[ref_id]
                            if referenced["tool"] not in {"analyze_deseq2_counts", "analyze_rnaseq_result"}:
                                raise ValueError(
                                    "compare_differential_expression_results can only reference analyze_deseq2_counts "
                                    "or analyze_rnaseq_result steps."
                                )
                            comparison_inputs.append(
                                {
                                    "label": referenced["label"] or referenced["step_id"] or referenced["tool"],
                                    "structured_data": referenced["structured_data"],
                                }
                            )
                    if isinstance(params.get("results"), list):
                        comparison_inputs.extend(item for item in params.get("results", []) if isinstance(item, dict))
                    if len(comparison_inputs) < 2 and current_state.get("kind") in {"deseq2_result", "rna_seq_result"}:
                        comparison_inputs.append(
                            {
                                "label": step_label or "current_result",
                                "structured_data": dict(current_state.get("data") or {}),
                            }
                        )
                    result = self.compare_differential_expression_results(
                        results=comparison_inputs,
                        gene_source=clean_text(params.get("gene_source") or "all_significant") or "all_significant",
                        label=step_label,
                    )
                    current_state = {
                        "kind": "differential_expression_comparison_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "enrich_gene_set":
                    step_species = clean_text(params.get("species") or normalized_source.get("species") or current_state.get("species") or self.settings.default_species) or self.settings.default_species
                    genes = _current_gene_list(gene_source=clean_text(params.get("gene_source") or "all_significant") or "all_significant")
                    result = self.enrich_gene_set(
                        genes=genes,
                        species=step_species,
                        label=step_label,
                        database=clean_text(params.get("database") or normalized_source.get("database") or "GO_BP") or "GO_BP",
                        library=clean_text(params.get("library") or normalized_source.get("library")),
                        backend=clean_text(params.get("backend") or normalized_source.get("backend") or "auto") or "auto",
                        background_genes=normalize_gene_list(
                            params.get("background_genes") or normalized_source.get("background_genes") or [],
                            species=step_species,
                        ),
                        simplify_go_similarity=float(params.get("simplify_go_similarity", normalized_source.get("simplify_go_similarity", 0.0))),
                    )
                    current_state = {
                        "kind": "enrichment_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "ranked_gene_set_gsea":
                    scores = _current_ranked_scores()
                    step_species = clean_text(params.get("species") or normalized_source.get("species") or current_state.get("species") or self.settings.default_species) or self.settings.default_species
                    result = self.ranked_gene_set_gsea(
                        gene_scores=scores,
                        species=step_species,
                        label=step_label,
                        limit=int(params.get("limit", normalized_source.get("limit", 15))),
                        database=clean_text(params.get("database") or normalized_source.get("database") or "GO_BP") or "GO_BP",
                    )
                    current_state = {
                        "kind": "ranked_gsea_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "gene_set_disease_context":
                    genes = _current_gene_list(gene_source=clean_text(params.get("gene_source") or "all_significant") or "all_significant")
                    result = self.gene_set_disease_context(
                        genes=genes,
                        species=clean_text(params.get("species") or normalized_source.get("species") or self.settings.default_species) or self.settings.default_species,
                        label=step_label,
                        database=clean_text(params.get("database") or normalized_source.get("database") or "DO") or "DO",
                        limit=int(params.get("limit", normalized_source.get("limit", 15))),
                    )
                    current_state = {
                        "kind": "disease_enrichment_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                elif tool == "search_papers":
                    query = _current_query(params, step_label=step_label)
                    result = self.search_papers(
                        query=query,
                        limit=int(params.get("limit", normalized_source.get("limit", 5))),
                        open_access_only=bool(params.get("open_access_only", normalized_source.get("open_access_only", False))),
                    )
                    current_state = {
                        "kind": "paper_search_result",
                        "data": dict(result.get("structured_data") or {}),
                        "source": normalized_source,
                    }
                else:
                    raise ValueError(
                        "Unsupported pipeline step. Supported tools are inspect_tabular_data, prepare_rnaseq_inputs, run_log_transform, run_expression_pca, "
                        "run_expression_hierarchical_clustering, run_expression_correlation, "
                        "run_expression_consensus_clustering, run_supervised_expression_model, "
                        "evaluate_cluster_agreement, run_expression_statistics, analyze_single_cell_rnaseq, "
                        "gene_summary, interaction_network, pathway_context, gene_disease_associations, "
                        "investigate_gene_set, analyze_rnaseq_result, analyze_deseq2_counts, "
                        "compare_differential_expression_results, enrich_gene_set, ranked_gene_set_gsea, "
                        "gene_set_disease_context, and search_papers."
                    )

                warnings.extend(str(item) for item in (result.get("warnings") or []))
                artifact_ids = _step_artifact_ids(result)
                flat_artifact_ids.extend(artifact_ids)
                record = {
                    "tool": tool,
                    "id": step_id or None,
                    "summary": str(result.get("summary") or ""),
                    "provider": str(result.get("provider") or ""),
                    "artifact_ids": artifact_ids,
                }
                step_records.append(record)
                if step_id:
                    step_outputs[step_id] = {
                        "tool": tool,
                        "step_id": step_id,
                        "label": step_label,
                        "structured_data": dict(result.get("structured_data") or {}),
                    }

        unique_warnings = unique_preserve_order(warnings)
        workspace_info = {
            **self.workspace_gateway.status(),
            "saved": bool(flat_artifact_ids),
            "artifact_ids": unique_preserve_order(flat_artifact_ids),
            "artifact_id": flat_artifact_ids[0] if flat_artifact_ids else None,
            "step_count": len(step_records),
        }
        final_step = step_records[-1]
        structured_data = {
            "source": {
                "kind": source_kind,
                **source_matrix_info,
                **{
                    key: normalized_source.get(key)
                    for key in [
                        "sample_metadata_path",
                        "counts_sheet_name",
                        "sample_metadata_sheet_name",
                        "sample_metadata_text",
                        "gene_column",
                        "sample_column",
                        "condition_column",
                        "condition_a",
                        "condition_b",
                        "design_formula",
                        "species",
                        "comparison",
                        "label",
                        "query",
                        "result_path",
                        "h5ad_path",
                        "deg_table_path",
                    ]
                    if key in normalized_source
                },
            },
            "steps": step_records,
            "final": final_step,
        }
        summary = f"Ran pipeline with {len(step_records)} step(s): {', '.join(item['tool'] for item in step_records)}."
        return {
            "summary": summary,
            "provider": "gleskit_pipeline",
            "structured_data": structured_data,
            "warnings": unique_warnings,
            "workspace": workspace_info,
            "artifacts": [
                {
                    "tool": item["tool"],
                    "summary": item["summary"],
                    "provider": item["provider"],
                    "artifact_ids": item["artifact_ids"],
                }
                for item in step_records
            ],
        }

    def _capture_expression_matrix_analysis(
        self,
        *,
        step_name: str,
        result: dict[str, Any],
        label: str = "",
        provenance: dict[str, Any] | None = None,
        artifact_kind: str | None = None,
        derived_inputs: dict[str, Any] | None = None,
        question_ready: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = self._expression_matrix_title(
            step_name,
            label=label,
            source_path=str((result.get("input") or {}).get("path") or ""),
        )
        artifact_structured_data = {**dict(result), "analysis_step": step_name}
        if derived_inputs:
            artifact_structured_data["derived_inputs"] = dict(derived_inputs)
        if question_ready:
            artifact_structured_data["question_ready"] = dict(question_ready)
        if step_name == "log_transform" and isinstance(derived_inputs, dict):
            reusable_matrix_path = clean_text(derived_inputs.get("matrix_path"))
            if reusable_matrix_path:
                artifact_structured_data["path"] = reusable_matrix_path
                artifact_structured_data["data_path"] = reusable_matrix_path
                technical_block = dict(artifact_structured_data.get("technical") or {})
                technical_block.setdefault("path", reusable_matrix_path)
                artifact_structured_data["technical"] = technical_block

        if _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS"):
            structured_data = self._compact_expression_matrix_structured_data_for_model(
                step_name=step_name,
                artifact_structured_data=artifact_structured_data,
            )
        else:
            structured_data = artifact_structured_data

        artifact_spec = {
            "kind": artifact_kind or f"expression_{step_name}",
            "title": title,
            "summary": str(result.get("summary") or title),
            "provider": "expression_matrix",
            "payload": artifact_structured_data,
            "provenance": dict(provenance or {}),
        }
        workspace_info = self._capture_artifacts_to_active_session([artifact_spec])
        warnings = self._workspace_warnings(workspace_info)
        response = {
            "summary": str(result.get("summary") or title),
            "provider": "expression_matrix",
            "structured_data": structured_data,
            "warnings": warnings,
            "workspace": workspace_info,
            "artifacts": {
                "result": {
                    "title": title,
                    "artifact_id": workspace_info.get("artifact_ids", [None])[0] if workspace_info.get("artifact_ids") else None,
                    "kind": artifact_kind or f"expression_{step_name}",
                }
            },
        }
        if derived_inputs:
            response["derived_inputs"] = dict(derived_inputs)
            reusable_matrix_path = clean_text(derived_inputs.get("matrix_path"))
            if reusable_matrix_path:
                response["matrix_path"] = reusable_matrix_path
        if question_ready:
            response["question_ready"] = dict(question_ready)
            pc1_percent = question_ready.get("pc1_explained_variance_percent")
            if pc1_percent is not None:
                response["pc1_explained_variance_percent"] = pc1_percent
        return response

    def _compact_expression_matrix_structured_data_for_model(
        self,
        *,
        step_name: str,
        artifact_structured_data: dict[str, Any],
    ) -> dict[str, Any]:
        compact_input = self._compact_expression_matrix_input_payload(
            artifact_structured_data.get("input")
        )
        compact = {
            "summary": artifact_structured_data.get("summary"),
            "provider": artifact_structured_data.get("provider"),
            "analysis": artifact_structured_data.get("analysis"),
            "analysis_step": artifact_structured_data.get("analysis_step") or step_name,
            "input": compact_input,
            "parameters": artifact_structured_data.get("parameters"),
            "derived_inputs": artifact_structured_data.get("derived_inputs"),
            "question_ready": artifact_structured_data.get("question_ready"),
        }
        if step_name == "log_transform":
            compact["output"] = artifact_structured_data.get("output")
        elif step_name == "pca":
            components = dict(artifact_structured_data.get("components") or {})
            explained_ratio = list(components.get("explained_variance_ratio") or [])
            components["explained_variance_percent"] = [
                float(value) * 100.0 for value in explained_ratio if value is not None
            ]
            compact["observations"] = self._compact_matrix_axis_payload(
                artifact_structured_data.get("observations")
            )
            compact["features"] = self._compact_matrix_axis_payload(
                artifact_structured_data.get("features")
            )
            compact["components"] = components
            compact["scores_preview"] = list(artifact_structured_data.get("scores") or [])[:10]
            compact["top_loadings"] = list(artifact_structured_data.get("loadings") or [])[:10]
        elif step_name == "correlation":
            compact["mode"] = artifact_structured_data.get("mode")
            compact["correlations"] = list(artifact_structured_data.get("correlations") or [])[:25]
        elif step_name == "hierarchical_clustering":
            compact["cluster_count"] = artifact_structured_data.get("cluster_count")
            compact["cluster_sizes"] = ((artifact_structured_data.get("question_ready") or {}).get("cluster_sizes"))
            compact["clusters_preview"] = list(artifact_structured_data.get("clusters") or [])[:5]
        elif step_name == "consensus_clustering":
            compact["cluster_count"] = artifact_structured_data.get("cluster_count")
            compact["cluster_sizes"] = ((artifact_structured_data.get("question_ready") or {}).get("cluster_sizes"))
            compact["consistency_summary"] = artifact_structured_data.get("consistency_summary")
        elif step_name == "cluster_agreement":
            compact["evaluation_scope"] = artifact_structured_data.get("evaluation_scope")
            compact["agreement"] = artifact_structured_data.get("agreement")
            train_block = dict(artifact_structured_data.get("train") or {})
            test_block = dict(artifact_structured_data.get("test") or {})
            compact["train"] = {"consistency_summary": train_block.get("consistency_summary")}
            compact["test"] = {"consistency_summary": test_block.get("consistency_summary")}
        return compact

    def _compact_matrix_axis_payload(self, payload: Any) -> dict[str, Any] | Any:
        if not isinstance(payload, dict):
            return payload
        labels = list(payload.get("labels") or [])
        compact = {key: payload.get(key) for key in payload.keys() if key != "labels"}
        compact["label_preview"] = labels[:10]
        compact["label_count"] = len(labels)
        return compact

    def _compact_expression_matrix_input_payload(self, payload: Any) -> dict[str, Any] | Any:
        if not isinstance(payload, dict):
            return payload
        row_labels = list(payload.get("row_labels") or [])
        column_labels = list(payload.get("column_labels") or [])
        compact = {
            "source_kind": payload.get("source_kind"),
            "path": payload.get("path"),
            "delimiter": payload.get("delimiter"),
            "row_id_column": payload.get("row_id_column"),
            "analysis_axis": payload.get("analysis_axis"),
            "sheet_name": payload.get("sheet_name"),
            "available_sheets": payload.get("available_sheets"),
            "header_row": payload.get("header_row"),
            "skip_rows": payload.get("skip_rows"),
            "row_count": payload.get("row_count"),
            "column_count": payload.get("column_count"),
            "row_labels_preview": row_labels[:10],
            "column_labels_preview": column_labels[:10],
        }
        return compact

    def _compact_workspace_artifact_for_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact_kind = clean_text(payload.get("kind"))
        artifact_payload = dict(payload.get("payload") or {})
        if artifact_kind not in {
            "log_transform",
            "expression_pca",
            "expression_hierarchical_clustering",
            "expression_correlation",
            "expression_consensus_clustering",
            "cluster_agreement",
            "single_cell_cluster_marker_analysis",
        }:
            return payload
        if artifact_kind == "single_cell_cluster_marker_analysis":
            compact_payload = self._compact_cluster_marker_payload_for_model(artifact_payload)
            return {
                "artifact_id": payload.get("artifact_id"),
                "session_id": payload.get("session_id"),
                "kind": payload.get("kind"),
                "title": payload.get("title"),
                "summary": payload.get("summary"),
                "created_at": payload.get("created_at"),
                "provider": payload.get("provider"),
                "reusable_path": self._extract_reusable_file_path(payload) or None,
                "question_ready": artifact_payload.get("question_ready"),
                "derived_inputs": artifact_payload.get("derived_inputs"),
                "payload": compact_payload,
                "workspace": payload.get("workspace"),
            }
        step_name = clean_text(artifact_payload.get("analysis_step") or artifact_payload.get("analysis") or artifact_kind)
        if step_name.startswith("expression_"):
            step_name = step_name.removeprefix("expression_")

        compact = {
            "artifact_id": payload.get("artifact_id"),
            "session_id": payload.get("session_id"),
            "kind": payload.get("kind"),
            "title": payload.get("title"),
            "summary": payload.get("summary"),
            "created_at": payload.get("created_at"),
            "provider": payload.get("provider"),
            "reusable_path": self._extract_reusable_file_path(payload) or None,
            "question_ready": artifact_payload.get("question_ready"),
            "derived_inputs": artifact_payload.get("derived_inputs"),
            "payload": self._compact_expression_matrix_structured_data_for_model(
                step_name=step_name,
                artifact_structured_data=artifact_payload,
            ),
            "workspace": payload.get("workspace"),
        }
        return compact

    def _persist_cluster_labels_output(
        self,
        *,
        cluster_labels: dict[str, Any],
        clusters: list[dict[str, Any]] | None,
        step_name: str,
        label: str = "",
        source_path: str = "",
    ) -> dict[str, Any]:
        runtime_status = self.workspace_gateway.status()
        session_id = clean_text(runtime_status.get("active_session_id")) or "stateless"
        root = self.settings.workspace_default_root / "derived_clusters" / session_id
        root.mkdir(parents=True, exist_ok=True)
        source_name = clean_text(Path(source_path).stem if source_path else "") or "matrix"
        label_token = clean_text(label).replace(" ", "_").replace("/", "_") or source_name
        stem = f"{step_name}_{label_token}_{new_id('clusters')}"
        labels_path = root / f"{stem}.tsv"
        summary_path = root / f"{stem}.summary.json"

        label_rows = [
            {
                "observation_label": clean_text(observation_label) or observation_label,
                "cluster": cluster_id,
            }
            for observation_label, cluster_id in (cluster_labels or {}).items()
            if clean_text(observation_label) or observation_label
        ]
        pd.DataFrame(label_rows).to_csv(labels_path, sep="\t", index=False)
        summary_payload = {
            "cluster_count": len({row["cluster"] for row in label_rows}),
            "cluster_sizes": [
                {
                    "cluster": row.get("cluster"),
                    "size": row.get("size"),
                    "members": list(row.get("members") or []),
                }
                for row in (clusters or [])
                if isinstance(row, dict)
            ],
        }
        summary_path.write_text(json.dumps(summary_payload, indent=2, sort_keys=True), encoding="utf-8")
        return {
            "cluster_labels_path": str(labels_path.resolve()),
            "cluster_summary_path": str(summary_path.resolve()),
            "cluster_label_count": len(label_rows),
            "cluster_count": summary_payload["cluster_count"],
        }

    def _persist_expression_matrix_output(
        self,
        *,
        matrix: Any,
        step_name: str,
        label: str = "",
        source_path: str = "",
    ) -> dict[str, Any]:
        runtime_status = self.workspace_gateway.status()
        session_id = clean_text(runtime_status.get("active_session_id")) or "stateless"
        root = self.settings.workspace_default_root / "derived_matrices" / session_id
        root.mkdir(parents=True, exist_ok=True)
        source_name = clean_text(Path(source_path).stem if source_path else "") or "matrix"
        label_token = clean_text(label).replace(" ", "_").replace("/", "_") or source_name
        suffix = ".tsv" if str(getattr(matrix, "delimiter", "\t") or "\t") == "\t" else ".csv"
        path = root / f"{step_name}_{label_token}_{new_id('matrix')}{suffix}"
        serialized = clean_text(getattr(matrix, "source_text", ""))
        if not serialized:
            frame = pd.DataFrame(
                data=getattr(matrix, "values"),
                index=list(getattr(matrix, "row_labels", [])),
                columns=list(getattr(matrix, "column_labels", [])),
            )
            frame.index.name = clean_text(getattr(matrix, "row_id_column", "")) or "row_id"
            frame.to_csv(path, sep="\t" if suffix == ".tsv" else ",")
        else:
            path.write_text(serialized, encoding="utf-8")
        return {
            "matrix_path": str(path.resolve()),
            "row_id_column": clean_text(getattr(matrix, "row_id_column", "")) or "row_id",
            "analysis_axis": clean_text(getattr(matrix, "analysis_axis_default", "")) or None,
            "format": "tsv" if suffix == ".tsv" else "csv",
        }

    def _load_cluster_labels_payload(
        self,
        *,
        cluster_labels: dict[str, Any] | list[dict[str, Any]] | list[Any] | None = None,
        cluster_labels_path: str = "",
    ) -> dict[str, Any] | list[dict[str, Any]] | list[Any] | None:
        cleaned_path = clean_text(cluster_labels_path)
        if not cleaned_path:
            return cluster_labels
        path = Path(self._resolve_input_file_selector(cleaned_path, role="cluster_labels_path")).expanduser().resolve()
        if not path.exists():
            raise ProviderError(f"Cluster labels path was not found: {path}")
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                nested_labels = payload.get("cluster_labels")
                if isinstance(nested_labels, dict):
                    return nested_labels
                if all(not isinstance(value, (dict, list)) for value in payload.values()):
                    return payload
            if isinstance(payload, list):
                return payload
            raise ProviderError("Cluster labels JSON must be a mapping or a list of label/cluster rows.")

        table = load_table(path=str(path), dtype=str, keep_default_na=False)
        frame = table.frame.copy()
        if frame.empty:
            raise ProviderError("Cluster labels table is empty.")
        normalized_columns = {clean_text(column).lower(): column for column in frame.columns}
        cluster_column = (
            normalized_columns.get("cluster")
            or normalized_columns.get("cluster_id")
            or normalized_columns.get("cluster_label")
        )
        label_column = (
            normalized_columns.get("observation_label")
            or normalized_columns.get("label")
            or normalized_columns.get("sample")
            or normalized_columns.get("sample_id")
            or normalized_columns.get("observation")
            or normalized_columns.get("cell")
            or normalized_columns.get("id")
        )
        if not cluster_column and frame.shape[1] >= 2:
            cluster_column = frame.columns[1]
        if not label_column and frame.shape[1] >= 2:
            label_column = frame.columns[0]
        if not label_column or not cluster_column:
            raise ProviderError(
                "Cluster labels table must contain a label column and a cluster column."
            )
        resolved: dict[str, Any] = {}
        for row in frame.to_dict(orient="records"):
            observation_label = clean_text(row.get(label_column))
            cluster_id = row.get(cluster_column)
            if not observation_label:
                continue
            resolved[observation_label] = cluster_id
        if not resolved:
            raise ProviderError("Cluster labels path did not contain any usable label-to-cluster assignments.")
        return resolved

    def _persist_cluster_marker_gene_sets(
        self,
        *,
        marker_gene_sets: dict[str, list[str]],
        label: str = "",
        source_path: str = "",
        stem_prefix: str = "cluster_markers",
        output_key_prefix: str = "marker_gene_sets",
    ) -> dict[str, Any]:
        runtime_status = self.workspace_gateway.status()
        session_id = clean_text(runtime_status.get("active_session_id")) or "stateless"
        root = self.settings.workspace_default_root / "derived_clusters" / session_id
        root.mkdir(parents=True, exist_ok=True)
        source_name = clean_text(Path(source_path).stem if source_path else "") or "markers"
        label_token = clean_text(label).replace(" ", "_").replace("/", "_") or source_name
        safe_prefix = clean_text(stem_prefix).replace(" ", "_").replace("/", "_") or "cluster_markers"
        safe_key_prefix = clean_text(output_key_prefix).replace(" ", "_").replace("/", "_") or "marker_gene_sets"
        stem = f"{safe_prefix}_{label_token}_{new_id('cluster_markers')}"
        tsv_path = root / f"{stem}.tsv"
        json_path = root / f"{stem}.json"
        rows: list[dict[str, Any]] = []
        for cluster_id, genes in (marker_gene_sets or {}).items():
            for rank, gene in enumerate(genes or [], start=1):
                rows.append({"cluster": cluster_id, "rank": rank, "gene": gene})
        pd.DataFrame(rows).to_csv(tsv_path, sep="\t", index=False)
        json_path.write_text(json.dumps(marker_gene_sets, indent=2, sort_keys=True), encoding="utf-8")
        payload = {
            f"{safe_key_prefix}_path": str(tsv_path.resolve()),
            f"{safe_key_prefix}_json_path": str(json_path.resolve()),
            f"{safe_key_prefix}_cluster_count": len(marker_gene_sets or {}),
            f"{safe_key_prefix}_gene_total": len(rows),
        }
        if safe_key_prefix == "marker_gene_sets":
            payload["cluster_count"] = len(marker_gene_sets or {})
            payload["marker_gene_total"] = len(rows)
        return payload

    def _count_significant_enrichment_terms(self, payload: dict[str, Any], *, threshold: float = 0.05) -> int:
        terms = [row for row in (payload.get("terms") or []) if isinstance(row, dict)]
        count = 0
        for row in terms:
            adjusted = row.get("adjusted_p_value")
            if adjusted is None:
                adjusted = row.get("padj")
            try:
                if adjusted is not None and float(adjusted) <= float(threshold):
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def _top_enrichment_term_summary(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        top_terms = [row for row in (payload.get("top_terms") or []) if isinstance(row, dict)]
        if not top_terms:
            return None
        top_term = dict(top_terms[0])
        return {
            "name": top_term.get("name") or top_term.get("term_name") or top_term.get("label"),
            "source": top_term.get("source"),
            "adjusted_p_value": top_term.get("adjusted_p_value"),
            "overlap": top_term.get("overlap") or top_term.get("intersection_size"),
        }

    def _build_cluster_marker_question_ready(
        self,
        *,
        payload: dict[str, Any],
        cluster_enrichment: list[dict[str, Any]],
        enrichment_database: str,
    ) -> dict[str, Any]:
        question_ready: dict[str, Any] = {
            "cluster_count": int(payload.get("cluster_count") or 0),
            "significant_marker_count_bh": int(payload.get("significant_marker_count_bh") or 0),
            "clusters_by_marker_count_bh": [
                {
                    "cluster": row.get("cluster"),
                    "marker_count_bh": row.get("marker_count_bh"),
                    "cluster_size": row.get("cluster_size"),
                    "top_marker_gene_count": row.get("top_marker_gene_count"),
                }
                for row in list(payload.get("cluster_markers") or [])
                if isinstance(row, dict)
            ],
        }
        if not cluster_enrichment:
            return question_ready

        enrichment_rows: list[dict[str, Any]] = []
        for entry in cluster_enrichment:
            if not isinstance(entry, dict):
                continue
            cluster_id = entry.get("cluster")
            structured = dict(entry.get("structured_data") or {})
            top_term = self._top_enrichment_term_summary(structured)
            enrichment_rows.append(
                {
                    "cluster": cluster_id,
                    "significant_pathway_count": self._count_significant_enrichment_terms(structured),
                    "top_term": top_term,
                }
            )
        question_ready["cluster_enrichment_summary"] = enrichment_rows
        if enrichment_rows:
            ranked = sorted(
                enrichment_rows,
                key=lambda row: (
                    -int(row.get("significant_pathway_count") or 0),
                    float(((row.get("top_term") or {}).get("adjusted_p_value") or 1.0)),
                    str(row.get("cluster") or ""),
                ),
            )
            best = ranked[0]
            top_term = dict(best.get("top_term") or {})
            question_ready["cluster_with_most_significant_pathways"] = best.get("cluster")
            question_ready["max_cluster_significant_pathway_count"] = int(best.get("significant_pathway_count") or 0)
            question_ready["top_pathway_for_max_cluster"] = top_term.get("name")
            question_ready["top_pathway_adjusted_p_value"] = top_term.get("adjusted_p_value")
            if clean_text(enrichment_database).upper() == "REAC":
                question_ready["top_reactome_pathway_for_max_cluster"] = top_term.get("name")
        return question_ready

    def _compact_cluster_marker_payload_for_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        cluster_markers = [row for row in (payload.get("cluster_markers") or []) if isinstance(row, dict)]
        compact_cluster_markers = [
            {
                "cluster": row.get("cluster"),
                "cluster_size": row.get("cluster_size"),
                "marker_count_bh": row.get("marker_count_bh"),
                "significant_marker_gene_count": row.get("significant_marker_gene_count"),
                "top_marker_gene_count": row.get("top_marker_gene_count"),
                "top_marker_genes": list(row.get("top_marker_genes") or [])[:15],
            }
            for row in cluster_markers[:12]
        ]
        return {
            "summary": payload.get("summary"),
            "provider": payload.get("provider"),
            "analysis": payload.get("analysis"),
            "input": payload.get("input"),
            "species": payload.get("species"),
            "label": payload.get("label"),
            "cluster_count": payload.get("cluster_count"),
            "feature_count": payload.get("feature_count"),
            "significant_marker_count_bh": payload.get("significant_marker_count_bh"),
            "cluster_markers": compact_cluster_markers,
            "question_ready": payload.get("question_ready"),
            "derived_inputs": payload.get("derived_inputs"),
        }

    @staticmethod
    def _expression_matrix_title(step_name: str, *, label: str = "", source_path: str = "") -> str:
        step_label = {
            "pca": "PCA",
            "log_transform": "Log transform",
            "hierarchical_clustering": "Hierarchical clustering",
            "correlation": "Correlation analysis",
            "consensus_clustering": "Consensus clustering",
        }.get(step_name, step_name.replace("_", " ").title())
        token = clean_text(label)
        if token:
            return f"{step_label}: {token}"
        path_token = clean_text(Path(source_path).name if source_path else "")
        if path_token:
            return f"{step_label}: {path_token}"
        return step_label

    @staticmethod
    def _expression_statistics_title(analysis: str) -> str:
        return {
            "matrix_annotation_correlation": "Expression-statistics correlation",
            "distribution_summary": "Expression distribution summary",
            "group_test": "Expression group test",
            "featurewise_group_test": "Featurewise group test",
            "proportion_interval": "Expression proportion interval",
            "exact_binomial_test": "Exact binomial test",
            "multiple_testing_summary": "Multiple-testing summary",
        }.get(analysis, f"Expression statistics: {analysis.replace('_', ' ')}")

    @staticmethod
    def _single_cell_title(analysis: str) -> str:
        return {
            "summary": "Single-cell dataset summary",
            "cell_type_deg_counts": "Single-cell DEG counts",
            "gene_cell_type_lookup": "Single-cell gene lookup",
            "cluster_marker_analysis": "Single-cell cluster markers",
            "pseudobulk": "Single-cell pseudobulk",
        }.get(analysis, f"Single-cell analysis: {analysis.replace('_', ' ')}")

    def _single_cell_pseudobulk_result(
        self,
        *,
        h5ad_path: str,
        sample_column: str,
        condition_column: str,
        cell_type_column: str,
        cell_type_values: list[str],
        layer: str,
        use_raw: bool,
        dataset: Any,
    ) -> dict[str, Any]:
        counts_frame = dataset.counts.reset_index().rename(columns={"index": "gene"})
        metadata_frame = dataset.metadata.copy()
        counts_text = counts_frame.to_csv(sep="\t", index=False)
        metadata_text = metadata_frame.to_csv(sep="\t", index=False)
        return {
            "summary": str(dataset.summary.get("summary") or "Built a single-cell pseudobulk dataset."),
            "provider": "single_cell",
            "analysis": "pseudobulk",
            "input": {
                "h5ad_path": clean_text(h5ad_path) or None,
                "sample_column": clean_text(sample_column) or None,
                "condition_column": clean_text(condition_column) or None,
                "cell_type_column": clean_text(cell_type_column) or None,
                "cell_type_values": [clean_text(value) for value in cell_type_values if clean_text(value)],
                "layer": clean_text(layer) or None,
                "use_raw": bool(use_raw),
            },
            "summary_stats": dict(dataset.summary),
            "counts": {
                "row_count": int(counts_frame.shape[0]),
                "column_count": int(counts_frame.shape[1]),
                "columns": [str(column) for column in counts_frame.columns],
                "preview": table_rows_as_dicts(counts_frame.head(10)),
                "table_text": counts_text,
            },
            "metadata": {
                "row_count": int(metadata_frame.shape[0]),
                "column_count": int(metadata_frame.shape[1]),
                "columns": [str(column) for column in metadata_frame.columns],
                "rows": table_rows_as_dicts(metadata_frame),
                "table_text": metadata_text,
            },
        }

    def _compare_differential_expression_results_payload(
        self,
        *,
        results: list[dict[str, Any]],
        gene_source: str,
    ) -> dict[str, Any]:
        normalized_gene_source = clean_text(gene_source).lower() or "all_significant"
        prepared = [
            self._normalize_differential_expression_result_spec(
                spec,
                index=index + 1,
                gene_source=normalized_gene_source,
            )
            for index, spec in enumerate(results or [])
            if isinstance(spec, dict)
        ]
        if len(prepared) < 2:
            raise ProviderError("Differential-expression comparison requires at least 2 result specifications.")
        if normalized_gene_source not in {"upregulated", "downregulated", "all_significant"}:
            raise ProviderError("gene_source must be upregulated, downregulated, or all_significant.")

        active_sets = [set(item[normalized_gene_source]) for item in prepared]
        label_order = [item["label"] for item in prepared]
        union_genes = sorted(set().union(*active_sets))
        shared_all = sorted(set(active_sets[0]).intersection(*active_sets[1:]))

        pattern_map: dict[tuple[bool, ...], list[str]] = {}
        for gene in union_genes:
            pattern = tuple(gene in gene_set for gene_set in active_sets)
            pattern_map.setdefault(pattern, []).append(gene)
        pattern_rows = [
            {
                "pattern": {label_order[index]: bool(flag) for index, flag in enumerate(pattern)},
                "labels_present": [label_order[index] for index, flag in enumerate(pattern) if flag],
                "labels_absent": [label_order[index] for index, flag in enumerate(pattern) if not flag],
                "gene_count": len(genes),
                "genes_preview": genes[:25],
            }
            for pattern, genes in sorted(
                pattern_map.items(),
                key=lambda item: (-len(item[1]), tuple(int(flag) for flag in item[0]), tuple(item[1][:1] or [""])),
            )
        ]

        pairwise_rows: list[dict[str, Any]] = []
        for left_index in range(len(prepared)):
            for right_index in range(left_index + 1, len(prepared)):
                left = prepared[left_index]
                right = prepared[right_index]
                left_genes = set(left[normalized_gene_source])
                right_genes = set(right[normalized_gene_source])
                shared = sorted(left_genes & right_genes)
                only_left = sorted(left_genes - right_genes)
                only_right = sorted(right_genes - left_genes)
                same_direction = 0
                opposite_direction = 0
                for gene in shared:
                    left_direction = left["direction_map"].get(gene)
                    right_direction = right["direction_map"].get(gene)
                    if left_direction and right_direction:
                        if left_direction == right_direction:
                            same_direction += 1
                        else:
                            opposite_direction += 1
                union_count = len(left_genes | right_genes)
                pairwise_rows.append(
                    {
                        "left": left["label"],
                        "right": right["label"],
                        "left_count": len(left_genes),
                        "right_count": len(right_genes),
                        "shared_gene_count": len(shared),
                        "shared_count": len(shared),
                        "shared_genes_preview": shared[:25],
                        "left_only_count": len(only_left),
                        "unique_up": len(only_left),
                        "left_only_genes_preview": only_left[:25],
                        "right_only_count": len(only_right),
                        "unique_down": len(only_right),
                        "right_only_genes_preview": only_right[:25],
                        "union_count": union_count,
                        "concordant_count": same_direction,
                        "discordant_count": opposite_direction,
                        "jaccard": float(len(shared) / union_count) if union_count else 0.0,
                        "jaccard_index": float(len(shared) / union_count) if union_count else 0.0,
                        "overlap_fraction_of_left": float(len(shared) / len(left_genes)) if left_genes else 0.0,
                        "overlap_fraction_of_right": float(len(shared) / len(right_genes)) if right_genes else 0.0,
                        "shared_same_direction_count": same_direction,
                        "shared_opposite_direction_count": opposite_direction,
                    }
                )

        exclusive_gene_map: dict[str, list[str]] = {}
        for pattern, genes in pattern_map.items():
            if sum(pattern) == 1:
                label_index = pattern.index(True)
                exclusive_gene_map[label_order[label_index]] = list(genes)

        result_summaries = []
        for item in prepared:
            active_genes = sorted(item[normalized_gene_source])
            exclusive_genes = exclusive_gene_map.get(item["label"], [])
            result_summaries.append(
                {
                    "label": item["label"],
                    "comparison": item["comparison"],
                    "source_kind": item["source_kind"],
                    "species": item.get("species"),
                    "gene_count": len(active_genes),
                    "exclusive_gene_count": len(exclusive_genes),
                    "exclusive_genes_preview": exclusive_genes[:25],
                    "upregulated_count": len(item["upregulated"]),
                    "downregulated_count": len(item["downregulated"]),
                    "all_significant_count": len(item["all_significant"]),
                    "genes_preview": active_genes[:25],
                }
            )

        exclusive_counts = {
            summary["label"]: int(summary["exclusive_gene_count"])
            for summary in result_summaries
        }
        pairwise_set_operations = [
            {
                "left": row["left"],
                "right": row["right"],
                "shared_count": row["shared_count"],
                "left_only_count": row["left_only_count"],
                "right_only_count": row["right_only_count"],
                "union_count": row["union_count"],
            }
            for row in pairwise_rows
        ]
        union_count = len(union_genes)

        summary = (
            f"Compared {len(prepared)} differential-expression result(s) using the {normalized_gene_source} gene set, "
            f"covering {union_count} unique gene(s) with {len(shared_all)} shared across all result(s)."
        )
        return {
            "summary": summary,
            "provider": "gleskit_aggregate",
            "analysis": "differential_expression_comparison",
            "input": {
                "gene_source": normalized_gene_source,
                "result_count": len(prepared),
            },
            "results": result_summaries,
            "union_gene_count": union_count,
            "shared_across_all_gene_count": len(shared_all),
            "shared_across_all_genes_preview": shared_all[:50],
            "question_ready_counts": {
                "exclusive_by_label": exclusive_counts,
                "shared_across_all": len(shared_all),
                "union": union_count,
                "pairwise": pairwise_set_operations,
            },
            "set_operations": {
                "exclusive_by_label": exclusive_counts,
                "shared_across_all_count": len(shared_all),
                "union_count": union_count,
                "pairwise": pairwise_set_operations,
            },
            "pairwise": pairwise_rows,
            "membership_patterns": pattern_rows,
        }

    def _normalize_differential_expression_result_spec(
        self,
        spec: dict[str, Any],
        *,
        index: int,
        gene_source: str,
    ) -> dict[str, Any]:
        spec = dict(spec)
        for path_key in (
            "manifest_path",
            "result_path",
            "all_significant_genes_path",
            "upregulated_genes_path",
            "downregulated_genes_path",
            "ranked_gene_scores_path",
            "genes_path",
        ):
            value = clean_text(spec.get(path_key))
            if value:
                spec[path_key] = self._resolve_input_file_selector(value, role=path_key)
        structured_data = spec.get("structured_data") if isinstance(spec.get("structured_data"), dict) else spec
        payload = dict(structured_data or {})
        species = clean_text(spec.get("species") or payload.get("species") or self.settings.default_species) or self.settings.default_species
        if "significant_gene_sets" not in payload:
            derived_inputs = {}
            if isinstance(spec.get("derived_inputs"), dict):
                derived_inputs = dict(spec.get("derived_inputs") or {})
            elif isinstance(payload.get("derived_inputs"), dict):
                derived_inputs = dict(payload.get("derived_inputs") or {})
            manifest = self._read_deseq2_handoff_manifest(
                clean_text(spec.get("manifest_path") or derived_inputs.get("manifest_path"))
            )
            manifest = self._normalize_deseq2_handoff_manifest(manifest)
            manifest_species = clean_text(manifest.get("species"))
            if manifest_species and not clean_text(spec.get("species") or payload.get("species")):
                species = manifest_species
            manifest_files = dict(manifest.get("files") or {}) if isinstance(manifest.get("files"), dict) else {}
            desired_path_key = {
                "upregulated": "upregulated_genes_path",
                "downregulated": "downregulated_genes_path",
                "all_significant": "all_significant_genes_path",
            }.get(gene_source, "all_significant_genes_path")
            compact_kind = "all_significant" if gene_source not in {"upregulated", "downregulated"} else gene_source
            compact_path = clean_text(
                spec.get(desired_path_key)
                or derived_inputs.get(desired_path_key)
                or manifest_files.get(desired_path_key)
                or (spec.get("genes_path") if compact_kind == "all_significant" else "")
            )
            ranked_scores_path = clean_text(
                spec.get("ranked_gene_scores_path")
                or derived_inputs.get("ranked_gene_scores_path")
                or manifest_files.get("ranked_gene_scores_path")
            )
            if compact_path:
                compact_path = self._resolve_input_file_selector(compact_path, role="genes_path")
                compact_genes = self._load_gene_list_from_table_path(compact_path, species=species)
                payload = {
                    "summary": f"Loaded {len(compact_genes)} gene(s) from {Path(compact_path).name}.",
                    "provider": "gleskit_aggregate",
                    "input": {
                        "source_kind": "gene_list",
                        "result_path": compact_path,
                    },
                    "significant_gene_sets": {
                        "upregulated": compact_genes if compact_kind == "upregulated" else [],
                        "downregulated": compact_genes if compact_kind == "downregulated" else [],
                        "all_significant": compact_genes,
                    },
                    "direction_map": {gene: compact_kind if compact_kind in {"upregulated", "downregulated"} else "" for gene in compact_genes},
                }
            else:
                resolved_result_path, _converted_rds, handoff_manifest = self._normalize_rnaseq_result_path(clean_text(spec.get("result_path")))
                if not resolved_result_path and ranked_scores_path:
                    raise ProviderError(
                        "Differential-expression comparison requires DE result tables, a DESeq2 handoff manifest, or one of "
                        "`all_significant_genes_path`, `upregulated_genes_path`, or `downregulated_genes_path`. "
                        "`ranked_gene_scores_path` is for ranked_gene_set_gsea, not compare_differential_expression_results."
                    )
                handoff_column_map = dict((handoff_manifest.get("column_map") or {}) if isinstance(handoff_manifest, dict) else {})
                payload = self.rnaseq_backend.analyze_result(
                    result_path=resolved_result_path,
                    table_text=str(spec.get("table_text") or ""),
                    label=clean_text(spec.get("label")),
                    comparison=clean_text(spec.get("comparison")),
                    gene_column=clean_text(spec.get("gene_column")) or clean_text(handoff_column_map.get("gene")),
                    log2_fold_change_column=clean_text(spec.get("log2_fold_change_column")) or clean_text(handoff_column_map.get("log2FoldChange")),
                    padj_column=clean_text(spec.get("padj_column")) or clean_text(handoff_column_map.get("padj")),
                    p_value_column=clean_text(spec.get("p_value_column")) or clean_text(handoff_column_map.get("pvalue")),
                    base_mean_column=clean_text(spec.get("base_mean_column")) or clean_text(handoff_column_map.get("baseMean")),
                    dispersion_column=clean_text(spec.get("dispersion_column")) or clean_text(handoff_column_map.get("dispersion")),
                    stat_column=clean_text(spec.get("stat_column")) or clean_text(handoff_column_map.get("stat")),
                    delimiter=clean_text(spec.get("delimiter")),
                    sheet_name=clean_text(spec.get("sheet_name")),
                    header_row=int(spec.get("header_row", 1)),
                    skip_rows=int(spec.get("skip_rows", 0)),
                    padj_threshold=float(spec.get("padj_threshold", 0.05)),
                    p_value_threshold=float(spec.get("p_value_threshold", 0.05)),
                    log2_fold_change_threshold=float(spec.get("log2_fold_change_threshold", 0.0)),
                    top_n=int(spec.get("top_n", 25)),
                )
        gene_sets = dict(payload.get("significant_gene_sets") or {})
        upregulated = normalize_gene_list(gene_sets.get("upregulated") or [], species=species)
        downregulated = normalize_gene_list(gene_sets.get("downregulated") or [], species=species)
        all_significant = normalize_gene_list(gene_sets.get("all_significant") or [], species=species)
        if not all_significant:
            all_significant = unique_preserve_order(upregulated + downregulated)
        label = (
            clean_text(spec.get("label"))
            or clean_text(payload.get("label"))
            or clean_text((payload.get("input") or {}).get("label"))
            or clean_text(spec.get("comparison"))
            or clean_text((payload.get("input") or {}).get("comparison"))
            or f"result_{index}"
        )
        comparison = clean_text(spec.get("comparison")) or clean_text((payload.get("input") or {}).get("comparison"))
        direction_map = {gene: "upregulated" for gene in upregulated}
        direction_map.update({gene: "downregulated" for gene in downregulated})
        return {
            "label": label,
            "comparison": comparison or None,
            "source_kind": clean_text((payload.get("input") or {}).get("source_kind")) or "analysis_result",
            "species": species,
            "upregulated": upregulated,
            "downregulated": downregulated,
            "all_significant": all_significant,
            "direction_map": direction_map,
        }

    def enrich_gene_set(
        self,
        *,
        genes: list[str] | None = None,
        genes_path: str = "",
        genes_text: str = "",
        species: str | None = None,
        label: str = "",
        database: str = "GO_BP",
        library: str = "",
        backend: str = "auto",
        background_genes: list[str] | None = None,
        background_genes_path: str = "",
        simplify_go_similarity: float = 0.0,
        focus_terms: list[str] | None = None,
        response_detail: str = "full",
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        cleaned_genes = normalize_gene_list(genes or [], species=effective_species)
        resolved_genes_path = self._resolve_input_file_selector(genes_path, role="genes_path")
        if resolved_genes_path:
            cleaned_genes = unique_preserve_order(
                cleaned_genes
                + self._load_gene_list_from_table_path(resolved_genes_path, species=effective_species)
            )
        if clean_text(genes_text):
            cleaned_genes = unique_preserve_order(
                cleaned_genes + self._load_gene_list_from_text(clean_text(genes_text), species=effective_species)
            )
        background_input = normalize_gene_list(background_genes or [], species=effective_species)
        resolved_background_path = self._resolve_input_file_selector(background_genes_path, role="background_genes_path")
        if resolved_background_path:
            background_input = unique_preserve_order(background_input + self._load_gene_list_from_table_path(
                resolved_background_path,
                species=effective_species,
            ))
        warnings: list[str] = []
        analysis_genes, resolution_payload = self._prepare_pathway_gene_inputs(
            cleaned_genes,
            species=effective_species,
            warnings=warnings,
        )
        if (
            resolution_payload.get("resolution_used")
            and resolution_payload.get("resolved_gene_count", 0) == 0
            and analysis_genes
            and all(self._gene_token_needs_resolution(gene) for gene in analysis_genes)
        ):
            raise ProviderError(
                "Enrichment could not map any input identifiers to analyzable genes.",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Provide gene symbols or verify the species and identifier namespace.",
            )
        analysis_background, background_resolution = self._prepare_pathway_gene_inputs(
            background_input,
            species=effective_species,
            warnings=warnings,
        ) if background_input else ([], {})
        pathway_species = self._pathway_species_for_genes(analysis_genes, species=effective_species)
        payload, provider_name, summary = self._pathway_enrichment_context(
            analysis_genes,
            species=pathway_species,
            label=clean_text(label) or "Gene set enrichment",
            warnings=warnings,
            database=database,
            library=library,
            backend=backend,
            background_genes=analysis_background,
            simplify_go_similarity=simplify_go_similarity,
            focus_terms=focus_terms,
        )
        payload["input_genes"] = cleaned_genes
        payload["requested_species"] = effective_species
        payload["normalized_gene_set"] = resolution_payload
        payload["background_genes"] = analysis_background
        payload["background_resolution"] = background_resolution
        payload["background_genes_path"] = resolved_background_path or None
        payload["genes"] = analysis_genes
        workspace_info = self._capture_to_session_or_active(
            session_id=save_to_session_id,
            kind="enrichment",
            title=clean_text(label) or "Gene set enrichment",
            summary=summary,
            provider=provider_name,
            payload=payload,
            provenance={
                "species": effective_species,
                "input_genes": cleaned_genes,
                "genes": analysis_genes,
                "database": clean_text(database).upper() or "GO_BP",
                "background_gene_count": len(analysis_background),
                "background_genes_path": resolved_background_path or None,
                "simplify_go_similarity": float(simplify_go_similarity or 0.0),
                "focus_terms": [clean_text(term) for term in focus_terms or [] if clean_text(term)][:10],
            },
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        detail = clean_text(response_detail).lower() or "compact"
        if detail not in {"compact", "full"}:
            raise ProviderError("response_detail must be 'compact' or 'full'.")
        structured_data = self._compact_enrichment_payload_for_model(payload) if detail == "compact" or _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS") else payload
        return {
            "summary": summary,
            "provider": provider_name,
            "structured_data": structured_data,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def gene_set_disease_context(
        self,
        *,
        genes: list[str],
        species: str | None = None,
        label: str = "",
        database: str = "DO",
        limit: int = 15,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        self._require_human_species(effective_species, tool_name="gene_set_disease_context")
        cleaned_genes = normalize_gene_list(genes, species=effective_species)
        warnings: list[str] = []
        payload, provider_name, summary = self._gene_set_disease_context_payload(
            cleaned_genes,
            species=effective_species,
            label=label,
            database=database,
            limit=limit,
            warnings=warnings,
        )
        workspace_info = self._capture_to_active_session(
            kind="disease_enrichment",
            title=clean_text(label) or "Gene set disease context",
            summary=summary,
            provider=provider_name,
            payload=payload,
            provenance={
                "species": effective_species,
                "genes": cleaned_genes,
                "database": clean_text(payload.get("database")).upper() or "DO",
            },
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": summary,
            "provider": provider_name,
            "structured_data": payload,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def ranked_gene_set_gsea(
        self,
        *,
        gene_scores: dict[str, float] | list[dict[str, Any]] | None = None,
        gene_scores_path: str = "",
        gene_scores_text: str = "",
        species: str | None = None,
        label: str = "",
        limit: int = 15,
        database: str = "GO_BP",
        response_detail: str = "full",
        execution_timeout_seconds: int | None = None,
        save_to_session_id: str = "",
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)

        warnings: list[str] = []
        normalized_scores: list[dict[str, Any]] = []
        raw_scores: list[dict[str, Any]] = []
        seen: dict[str, float] = {}
        resolved_gene_scores_path = self._resolve_input_file_selector(gene_scores_path, role="gene_scores_path")
        if resolved_gene_scores_path:
            for row in self._load_gene_score_rows_from_table_path(resolved_gene_scores_path, species=effective_species):
                token = clean_text(row.get("gene"))
                if not token:
                    continue
                try:
                    value = float(row.get("score"))
                except (TypeError, ValueError):
                    continue
                raw_scores.append({"gene": token, "score": value})
                current = seen.get(token)
                if current is None or abs(value) > abs(current):
                    seen[token] = value
        if clean_text(gene_scores_text):
            parsed_text = load_table(path="", text=gene_scores_text, dtype=str, keep_default_na=False)
            for row in table_rows_as_dicts(parsed_text.frame):
                token = normalize_gene_token(row.get("gene"), species=effective_species)
                if not token:
                    continue
                try:
                    value = float(row.get("score"))
                except (TypeError, ValueError):
                    continue
                raw_scores.append({"gene": token, "score": value})
                current = seen.get(token)
                if current is None or abs(value) > abs(current):
                    seen[token] = value
        if isinstance(gene_scores, dict):
            for gene, raw_score in gene_scores.items():
                token = normalize_gene_token(gene, species=effective_species)
                if not token:
                    continue
                try:
                    value = float(raw_score)
                except (TypeError, ValueError):
                    continue
                raw_scores.append({"gene": token, "score": value})
                current = seen.get(token)
                if current is None or abs(value) > abs(current):
                    seen[token] = value
        elif gene_scores is not None:
            for row in gene_scores:
                if not isinstance(row, dict):
                    continue
                token = normalize_gene_token(row.get("gene"), species=effective_species)
                if not token:
                    continue
                try:
                    value = float(row.get("score"))
                except (TypeError, ValueError):
                    continue
                raw_scores.append({"gene": token, "score": value})
                current = seen.get(token)
                if current is None or abs(value) > abs(current):
                    seen[token] = value

        for gene, score in seen.items():
            normalized_scores.append({"gene": gene, "score": score})
        normalized_scores.sort(key=lambda row: abs(float(row["score"])), reverse=True)
        normalized_scores, resolution_payload = self._prepare_ranked_gene_scores(
            normalized_scores,
            species=effective_species,
            warnings=warnings,
        )

        normalized_database = self._normalize_pathway_database(database) or "GO_BP"
        if normalized_database not in {"GO_BP", "REAC", "KEGG"}:
            raise ProviderError("ranked_gene_set_gsea supports database='GO_BP', 'REAC', or 'KEGG'.")
        if normalized_database == "KEGG":
            gsea_sources = ["KEGG"]
        elif normalized_database == "REAC":
            gsea_sources = ["REAC"]
        else:
            gsea_sources = ["GO:BP"]
        provider_result = self._call_yulab_backend(
            "ranked_pathway_gsea",
            ranked_gene_scores=normalized_scores,
            species=effective_species,
            sources=gsea_sources,
            limit=limit,
            execution_timeout_seconds=execution_timeout_seconds,
        )
        payload = dict(provider_result.payload)
        payload["species"] = effective_species
        payload["label"] = clean_text(label) or None
        payload["input_gene_scores"] = raw_scores
        payload["ranked_gene_scores"] = normalized_scores
        payload["gene_score_count"] = len(normalized_scores)
        payload["normalized_gene_set"] = resolution_payload
        payload["database"] = normalized_database
        payload = self._standardize_enrichment_payload(
            payload,
            species=effective_species,
            query_size=len(normalized_scores),
            background_size=len(normalized_scores),
            backend="clusterProfiler",
        )
        summary = provider_result.summary + self._enrichment_summary_suffix(payload)

        workspace_info = self._capture_to_session_or_active(
            session_id=save_to_session_id,
            kind="ranked_gsea",
            title=clean_text(label) or "Ranked gene set GSEA",
            summary=summary,
            provider=provider_result.provider,
            payload=payload,
            provenance={
                "species": effective_species,
                "gene_score_count": len(normalized_scores),
                "database": normalized_database,
            },
        )
        detail = clean_text(response_detail).lower() or "compact"
        if detail not in {"compact", "full"}:
            raise ProviderError("response_detail must be 'compact' or 'full'.")
        return {
            "summary": summary,
            "provider": provider_result.provider,
            "structured_data": self._compact_enrichment_payload_for_model(payload) if detail == "compact" or _env_flag("GLESKIT_COMPACT_TOOL_PAYLOADS") else payload,
            "warnings": warnings + self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def compare_gene_programs(
        self,
        *,
        programs: dict[str, list[str]],
        species: str | None = None,
        label: str = "",
        database: str = "GO_BP",
        limit: int = 10,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)

        warnings: list[str] = []
        normalized_programs: dict[str, list[str]] = {}
        input_programs: dict[str, list[str]] = {}
        for program_label, genes in (programs or {}).items():
            cleaned_label = clean_text(program_label)
            cleaned_genes = normalize_gene_list(genes, species=effective_species)
            if cleaned_label and cleaned_genes:
                normalized_genes, _resolution_payload = self._prepare_pathway_gene_inputs(
                    cleaned_genes,
                    species=effective_species,
                    warnings=warnings,
                )
                normalized_programs[cleaned_label] = normalized_genes
                input_programs[cleaned_label] = cleaned_genes
        if len(normalized_programs) < 2:
            raise ProviderError("compare_gene_programs requires at least two named gene programs with genes.")
        normalized_database = self._normalize_pathway_database(database) or "GO_BP"

        if (self._is_human_species(effective_species) or self._is_mouse_species(effective_species)) and normalized_database in {"GO_BP", "REAC"}:
            try:
                provider_result = self._call_yulab_backend(
                    "compare_gene_programs",
                    programs=normalized_programs,
                    species=effective_species,
                    database=normalized_database,
                    limit=limit,
                )
                payload = dict(provider_result.payload)
                provider_name = provider_result.provider
                summary = provider_result.summary
            except ProviderError as exc:
                payload, provider_name, summary = self._compare_gene_programs_with_gprofiler(
                    programs=normalized_programs,
                    species=effective_species,
                    database=normalized_database,
                    limit=limit,
                )
                payload["local_backend_warning"] = str(exc)
        else:
            payload, provider_name, summary = self._compare_gene_programs_with_gprofiler(
                programs=normalized_programs,
                species=effective_species,
                database=normalized_database,
                limit=limit,
            )
        payload["species"] = effective_species
        payload["label"] = clean_text(label) or None
        payload["database"] = normalized_database
        payload["program_inputs"] = normalized_programs
        payload["input_programs"] = input_programs
        if provider_name != "gprofiler":
            payload["backend"] = "clusterProfiler"

        workspace_info = self._capture_to_active_session(
            kind="program_comparison",
            title=clean_text(label) or "Gene program comparison",
            summary=summary,
            provider=provider_name,
            payload=payload,
            provenance={
                "species": effective_species,
                "database": payload["database"],
                "program_labels": list(normalized_programs.keys()),
            },
        )
        return {
            "summary": summary,
            "provider": provider_name,
            "structured_data": payload,
            "warnings": warnings + self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def _compare_gene_programs_with_gprofiler(
        self,
        *,
        programs: dict[str, list[str]],
        species: str,
        database: str,
        limit: int,
    ) -> tuple[dict[str, Any], str, str]:
        if database == "GO_BP":
            gprofiler_sources = ["GO:BP"]
        elif database == "REAC":
            gprofiler_sources = ["REAC"]
        elif database == "KEGG":
            gprofiler_sources = ["KEGG"]
        elif database == "WP":
            gprofiler_sources = ["WP"]
        else:
            raise ProviderError("compare_gene_programs supports database='GO_BP', 'REAC', 'KEGG', or 'WP'.")

        program_payloads: dict[str, dict[str, Any]] = {}
        comparison_terms: list[dict[str, Any]] = []
        shared_terms_by_key: dict[str, dict[str, Any]] = {}
        for program_label, genes in programs.items():
            provider_result = self.gprofiler.enrich_genes(
                genes,
                species=species,
                limit=limit,
                sources=gprofiler_sources,
            )
            raw_terms = [dict(row) for row in (provider_result.payload.get("terms") or []) if isinstance(row, dict)]
            terms: list[dict[str, Any]] = []
            for raw_term in raw_terms:
                term = dict(raw_term)
                term["program_label"] = program_label
                term["source"] = clean_text(term.get("source")).upper() or gprofiler_sources[0]
                term["adjusted_p_value"] = term.get("adjusted_p_value", term.get("p_value"))
                terms.append(term)
                comparison_terms.append(term)

                term_key = clean_text(term.get("term_id")) or clean_text(term.get("term_name"))
                if not term_key:
                    continue
                current_score = term.get("adjusted_p_value")
                current_score = float(current_score) if isinstance(current_score, (int, float)) else None
                shared_entry = shared_terms_by_key.get(term_key)
                if shared_entry is None:
                    shared_terms_by_key[term_key] = {
                        "term_id": term.get("term_id"),
                        "term_name": term.get("term_name"),
                        "source": term.get("source"),
                        "program_labels": [program_label],
                        "program_count": 1,
                        "best_adjusted_p_value": current_score,
                    }
                else:
                    if program_label not in shared_entry["program_labels"]:
                        shared_entry["program_labels"].append(program_label)
                        shared_entry["program_count"] = len(shared_entry["program_labels"])
                    best_score = shared_entry.get("best_adjusted_p_value")
                    if current_score is not None and (best_score is None or current_score < best_score):
                        shared_entry["best_adjusted_p_value"] = current_score

            terms.sort(
                key=lambda row: (
                    row.get("adjusted_p_value") is None,
                    float(row["adjusted_p_value"]) if isinstance(row.get("adjusted_p_value"), (int, float)) else float("inf"),
                    clean_text(row.get("term_name")),
                )
            )
            program_payloads[program_label] = {
                "gene_count": len(genes),
                "term_count": len(terms),
                "terms": terms,
            }

        comparison_terms.sort(
            key=lambda row: (
                row.get("adjusted_p_value") is None,
                float(row["adjusted_p_value"]) if isinstance(row.get("adjusted_p_value"), (int, float)) else float("inf"),
                clean_text(row.get("program_label")),
                clean_text(row.get("term_name")),
            )
        )
        shared_terms = [
            term
            for term in shared_terms_by_key.values()
            if int(term.get("program_count") or 0) >= 2
        ]
        shared_terms.sort(
            key=lambda row: (
                -int(row.get("program_count") or 0),
                row.get("best_adjusted_p_value") is None,
                float(row["best_adjusted_p_value"]) if isinstance(row.get("best_adjusted_p_value"), (int, float)) else float("inf"),
                clean_text(row.get("term_name")),
            )
        )

        payload = {
            "analysis_mode": "ora_compare",
            "database": database,
            "backend": "gprofiler",
            "species": species,
            "program_count": len(programs),
            "programs": program_payloads,
            "comparison_terms": comparison_terms,
            "shared_terms": shared_terms,
        }
        summary = (
            f"Compared {len(programs)} gene program(s) with g:Profiler and found "
            f"{len(shared_terms)} shared enrichment term(s)."
        )
        return payload, "gprofiler", summary

    def search_papers(
        self,
        *,
        query: str,
        limit: int = 5,
        open_access_only: bool = False,
    ) -> dict[str, Any]:
        provider_result = self.europepmc.search_papers(query, limit=limit, open_access_only=open_access_only)
        workspace_info = self._capture_to_active_session(
            kind="paper_search",
            title=f"Paper search: {clean_text(query)}",
            summary=provider_result.summary,
            provider=provider_result.provider,
            payload=provider_result.payload,
            provenance={"query": clean_text(query), "limit": limit, "open_access_only": open_access_only},
        )
        return {
            "summary": provider_result.summary,
            "provider": provider_result.provider,
            "structured_data": provider_result.payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def gene_disease_associations(
        self,
        *,
        gene: str,
        species: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        resolution = self._resolve_gene_provider_result(gene, species=effective_species)
        matches = resolution.payload.get("matches", [])
        if not matches:
            raise ProviderError(f"Unable to resolve gene '{gene}' for Open Targets lookup.")
        best = matches[0]
        ensembl_gene_id = str(best.get("ensembl_gene_id") or "").strip()
        if not ensembl_gene_id:
            raise ProviderError(f"Gene '{gene}' did not resolve to an Ensembl gene identifier.")
        try:
            provider_result = self.opentargets.associated_diseases(ensembl_gene_id, limit=limit)
        except ProviderError as exc:
            symbol = normalize_gene_token(best.get("symbol"), species=effective_species) or normalize_gene_token(
                gene,
                species=effective_species,
            )
            matched_on = str(best.get("matched_on") or "provider_query")
            matched_value = str(best.get("matched_value") or best.get("query") or gene).strip()
            raise ProviderError(
                "Open Targets lookup failed after gene normalization. "
                f"Input='{clean_text(gene)}', resolved_symbol='{symbol}', "
                f"matched_on='{matched_on}', matched_value='{matched_value}', "
                f"ensembl_gene_id='{ensembl_gene_id}', limit={limit}. "
                f"Upstream error: {exc}"
            ) from exc
        summary = provider_result.summary
        provider_name = provider_result.provider
        payload = dict(provider_result.payload)
        payload["input_gene"] = normalize_gene_token(gene, species=effective_species)
        warnings: list[str] = []
        if self._is_human_species(effective_species):
            try:
                gwas_result = self.gwas_catalog.variant_trait_associations(
                    str(best.get("symbol") or normalize_gene_token(gene, species=effective_species)),
                    limit=max(5, min(int(limit), 15)),
                )
                payload["variant_trait_context"] = dict(gwas_result.payload)
                if gwas_result.payload.get("associations"):
                    summary += f" Added GWAS trait context with {len(gwas_result.payload['associations'])} association(s)."
            except ProviderError as exc:
                warnings.append(str(exc))
        workspace_info = self._capture_to_active_session(
            kind="gene_disease_associations",
            title=f"Disease associations: {payload.get('approved_symbol') or normalize_gene_token(gene, species=effective_species)}",
            summary=summary,
            provider=provider_name,
            payload=payload,
            provenance={"input_gene": gene, "species": effective_species, "ensembl_gene_id": ensembl_gene_id},
        )
        return {
            "summary": summary,
            "provider": provider_name,
            "structured_data": payload,
            "warnings": warnings + self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def variant_trait_associations(
        self,
        *,
        gene: str,
        species: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        self._require_human_species(effective_species, tool_name="variant_trait_associations")
        resolution = self._resolve_gene_provider_result(gene, species=effective_species)
        matches = resolution.payload.get("matches", [])
        if not matches:
            raise ProviderError(f"Unable to resolve gene '{clean_text(gene)}' for GWAS Catalog lookup.")
        best = dict(matches[0])
        symbol = normalize_gene_token(best.get("symbol"), species=effective_species) or normalize_gene_token(
            gene,
            species=effective_species,
        )

        provider_result = self.gwas_catalog.variant_trait_associations(symbol, limit=limit)
        payload = dict(provider_result.payload)
        payload["input_gene"] = normalize_gene_token(gene, species=effective_species)
        payload["normalized_gene"] = {
            "symbol": symbol,
            "matched_on": best.get("matched_on"),
            "matched_value": best.get("matched_value"),
            "ensembl_gene_id": best.get("ensembl_gene_id"),
        }
        workspace_info = self._capture_to_active_session(
            kind="variant_trait_associations",
            title=f"Variant-trait associations: {symbol}",
            summary=provider_result.summary,
            provider=provider_result.provider,
            payload=payload,
            provenance={"input_gene": gene, "species": effective_species, "symbol": symbol},
        )
        return {
            "summary": provider_result.summary,
            "provider": provider_result.provider,
            "structured_data": payload,
            "warnings": self._workspace_warnings(workspace_info),
            "workspace": workspace_info,
        }

    def investigate_gene_set(
        self,
        *,
        genes: list[str],
        question: str = "",
        label: str = "",
        species: str | None = None,
    ) -> dict[str, Any]:
        effective_species = self._effective_species(species)
        input_genes = normalize_gene_list(genes, species=effective_species)
        warnings: list[str] = []
        identity_resolution_payload = self._resolve_gene_identity_rows(
            input_genes,
            species=effective_species,
            warnings=warnings,
        )
        resolved_entries = [dict(row) for row in identity_resolution_payload.get("resolved_genes", []) if isinstance(row, dict)]
        unresolved_genes = list(identity_resolution_payload.get("unresolved_genes", []) or [])
        resolved_genes = unique_preserve_order(row["canonical_symbol"] for row in resolved_entries)
        resolution_payload = {
            "input_genes": input_genes,
            "resolved_genes": resolved_genes,
            "resolved_entries": resolved_entries,
            "unresolved_genes": unresolved_genes,
            "species": effective_species,
        }

        enrichment_payload = {"species": effective_species, "genes": resolved_genes, "terms": []}
        enrichment_summary = "No resolved genes available for enrichment."
        if resolved_genes:
            try:
                enrichment_payload, _provider_name, enrichment_summary = self._pathway_enrichment_context(
                    resolved_genes,
                    species=effective_species,
                    label=f"{label or 'Gene set investigation'} enrichment",
                    warnings=warnings,
                )
            except ProviderError as exc:
                warnings.append(str(exc))
                enrichment_summary = "Enrichment was unavailable."

        lead_gene_map = {
            normalize_gene_token(entry.get("canonical_symbol"), species=effective_species): entry
            for entry in resolved_entries[:3]
            if normalize_gene_token(entry.get("canonical_symbol"), species=effective_species)
        }
        disease_rows = self._lead_gene_disease_rows(
            list(lead_gene_map.keys()),
            lead_gene_map=lead_gene_map,
            warnings=warnings,
            limit=5,
        )
        disease_payload = {"genes": resolved_genes[:3], "diseases": disease_rows, "warnings": warnings}
        disease_summary = (
            f"Collected {len(disease_rows)} disease association row(s) for {min(len(resolved_entries), 3)} lead gene(s)."
        )
        disease_enrichment_payload = self._safe_gene_set_disease_context(
            resolved_genes,
            species=effective_species,
            label=f"{label or 'Gene set investigation'} disease enrichment",
            warnings=warnings,
            resolution_payload=identity_resolution_payload,
        )

        enrichment_terms = [dict(row) for row in enrichment_payload.get("terms", []) if isinstance(row, dict)]
        literature_query = build_literature_query(
            genes=resolved_genes[:3],
            terms=[
                str(row.get("term_name") or "")
                for row in (enrichment_terms[:2] + (disease_enrichment_payload.get("terms") or [])[:2])
            ],
            question=question,
        )
        paper_payload = {"query": literature_query, "paper_count": 0, "papers": []}
        paper_summary = "No literature query could be formed."
        if resolved_genes or question:
            query = literature_query or " AND ".join(resolved_genes[:2]) or clean_text(question)
            try:
                paper_result = self.europepmc.search_papers(query, limit=5)
                paper_payload = paper_result.payload
                paper_summary = paper_result.summary
            except ProviderError as exc:
                warnings.append(str(exc))
                paper_summary = "Literature search was unavailable."

        papers = [dict(row) for row in paper_payload.get("papers", []) if isinstance(row, dict)]
        bundle_payload = build_evidence_bundle_payload(
            question=question or (f"What biology is supported by {', '.join(resolved_genes[:3])}?" if resolved_genes else "What biology is supported by this gene set?"),
            genes=resolved_genes,
            enrichment_terms=enrichment_terms,
            disease_associations=disease_rows,
            papers=papers,
        )

        workflow_payload = {
            "input_genes": input_genes,
            "resolved_genes": resolved_genes,
            "question": clean_text(question),
            "resolution": resolution_payload,
            "enrichment": enrichment_payload,
            "disease_enrichment": disease_enrichment_payload,
            "disease_associations": disease_payload,
            "papers": paper_payload,
            "evidence_bundle": bundle_payload,
            "warnings": warnings,
        }

        workspace_info = self._capture_investigation(
            label=clean_text(label) or "Gene set investigation",
            species=effective_species,
            question=question,
            resolution_payload=resolution_payload,
            enrichment_payload=enrichment_payload,
            enrichment_summary=enrichment_summary,
            disease_payload=disease_payload,
            disease_summary=disease_summary,
            paper_payload=paper_payload,
            paper_summary=paper_summary,
            bundle_payload=bundle_payload,
            workflow_payload=workflow_payload,
        )
        warnings.extend(self._workspace_warnings(workspace_info))
        return {
            "summary": f"Investigated {len(input_genes)} gene(s) and assembled an evidence bundle.",
            "provider": "gleskit_local",
            "structured_data": workflow_payload,
            "warnings": warnings,
            "workspace": workspace_info,
        }

    def _quickgo_gene_product_id(self, protein_payload: dict[str, Any] | None) -> str | None:
        if not protein_payload:
            return None
        accession = clean_text(protein_payload.get("accession"))
        return f"UniProtKB:{accession}" if accession else None

    def _resolve_gene_provider_result(self, query: str, *, species: str) -> Any:
        local_result = self.reference_catalog.resolve_gene(query, species=species)
        if local_result is not None:
            return local_result

        mygene_result = self.mygene.resolve_gene(query, species=species)
        if mygene_result.payload.get("matches"):
            return mygene_result

        ensembl_result = self.ensembl.resolve_gene(query, species=species)
        if ensembl_result is not None:
            return ensembl_result

        return mygene_result

    def _best_uniprot_accession(self, gene_match: dict[str, Any], *, species: str) -> str | None:
        uniprot_ids = [clean_text(item) for item in gene_match.get("uniprot_ids", []) if clean_text(item)]
        if uniprot_ids:
            return uniprot_ids[0]
        symbol = clean_text(gene_match.get("symbol"))
        if not symbol:
            return None
        try:
            search = self.uniprot.search_proteins(symbol, species=species, size=1)
        except ProviderError:
            return None
        matches = search.payload.get("matches", [])
        if not matches:
            return None
        return clean_text(matches[0].get("accession")) or None

    @staticmethod
    def _normalize_pathway_database(database: str) -> str:
        token = clean_text(database).upper().replace("-", "_").replace(" ", "_").replace(":", "_")
        while "__" in token:
            token = token.replace("__", "_")
        aliases = {
            "GO_BP": "GO_BP",
            "GO_BIOLOGICAL_PROCESS": "GO_BP",
            "GO_BIOLOGICAL_PROCESS_2021": "GO_BP",
            "REAC": "REAC",
            "REACTOME": "REAC",
            "REACTOME_2022": "REAC",
            "KEGG": "KEGG",
            "KEGG_2019_MOUSE": "KEGG",
            "WP": "WP",
            "WIKIPATHWAYS": "WP",
            "WIKIPATHWAYS_2019_MOUSE": "WP",
        }
        return aliases.get(token, token)

    def _call_yulab_backend(self, method_name: str, /, *args, **kwargs):
        method = getattr(self.yulab_backend, method_name)
        attempts = 2 if method_name in {"pathway_enrichment", "ranked_pathway_gsea"} else 1
        last_error: ProviderError | None = None
        for attempt in range(attempts):
            try:
                call_kwargs = dict(kwargs)
                while True:
                    try:
                        return method(*args, **call_kwargs)
                    except TypeError as exc:
                        unsupported_key = next(
                            (
                                key
                                for key in ("focus_terms", "species", "execution_timeout_seconds")
                                if f"unexpected keyword argument '{key}'" in str(exc)
                            ),
                            None,
                        )
                        if unsupported_key is None:
                            raise
                        call_kwargs.pop(unsupported_key, None)
            except ProviderError as exc:
                last_error = exc
                message = str(exc).lower()
                transient = any(token in message for token in ("timeout", "timed out", "provider header", "temporarily unavailable"))
                if attempt + 1 < attempts and transient:
                    continue
                code = "provider_timeout" if transient else "provider_error"
                raise ProviderError(f"{exc} [error_code={code}, attempts={attempt + 1}]") from exc
        if last_error is not None:
            raise last_error
        raise ProviderError(f"GLESKit provider call failed: {method_name} [error_code=provider_error]")

    @staticmethod
    def _gene_token_needs_resolution(gene: str) -> bool:
        token = clean_text(gene).upper()
        if not token:
            return False
        if token.isdigit():
            return True
        return token.startswith("ENS")

    def _prepare_pathway_gene_inputs(
        self,
        genes: list[str],
        *,
        species: str,
        warnings: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        input_genes = normalize_gene_list(genes, species=species)
        resolution_payload: dict[str, Any] = {
            "input_genes": input_genes,
            "resolved_gene_count": 0,
            "resolved_with_entrez_count": 0,
            "resolved_genes": [],
            "unresolved_genes": [],
            "analysis_genes": input_genes,
            "analysis_gene_count": len(input_genes),
            "resolution_used": False,
            "mapping_status": "not_required",
        }
        if len(input_genes) < 2:
            return input_genes, resolution_payload
        if not any(self._gene_token_needs_resolution(gene) for gene in input_genes):
            return input_genes, resolution_payload

        resolved = self._resolve_gene_identity_rows(
            input_genes,
            species=species,
            warnings=warnings,
            limit=len(input_genes),
        )
        resolved_by_input = {
            normalize_gene_token(row.get("input_gene"), species=species): normalize_gene_token(
                row.get("canonical_symbol"), species=species
            )
            for row in resolved["resolved_genes"]
            if normalize_gene_token(row.get("input_gene"), species=species)
        }
        analysis_genes = unique_preserve_order(
            resolved_by_input.get(normalize_gene_token(gene, species=species))
            or normalize_gene_token(gene, species=species)
            for gene in input_genes
        )
        resolved["analysis_genes"] = analysis_genes
        resolved["analysis_gene_count"] = len(analysis_genes)
        resolved["resolution_used"] = True
        unresolved_analysis_ids = [gene for gene in analysis_genes if self._gene_token_needs_resolution(gene)]
        if unresolved_analysis_ids:
            resolved["mapping_status"] = "partial_or_unresolved"
            warnings.append(
                f"{len(unresolved_analysis_ids)} identifier(s) remain unresolved after local and fallback lookup; "
                "do not claim complete provider mapping."
            )
        else:
            resolved["mapping_status"] = "resolved"
        return analysis_genes, resolved

    def _prepare_ranked_gene_scores(
        self,
        ranked_gene_scores: list[dict[str, Any]],
        *,
        species: str,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cleaned_rows: list[dict[str, Any]] = []
        needs_resolution = False
        for row in ranked_gene_scores:
            if not isinstance(row, dict):
                continue
            gene = normalize_gene_token(row.get("gene"), species=species)
            if not gene:
                continue
            try:
                score = float(row.get("score"))
            except (TypeError, ValueError):
                continue
            cleaned_rows.append({"gene": gene, "score": score})
            if self._gene_token_needs_resolution(gene):
                needs_resolution = True

        resolution_payload: dict[str, Any] = {
            "input_gene_scores": cleaned_rows,
            "analysis_gene_scores": [],
            "analysis_gene_score_count": 0,
            "resolved_gene_count": 0,
            "resolved_with_entrez_count": 0,
            "resolved_genes": [],
            "unresolved_genes": [],
            "resolution_used": False,
        }
        if not cleaned_rows:
            return [], resolution_payload
        if not needs_resolution:
            seen: dict[str, float] = {}
            for row in cleaned_rows:
                gene = row["gene"]
                score = float(row["score"])
                current = seen.get(gene)
                if current is None or abs(score) > abs(current):
                    seen[gene] = score
            normalized_rows = [{"gene": gene, "score": score} for gene, score in seen.items()]
            normalized_rows.sort(key=lambda row: abs(float(row["score"])), reverse=True)
            resolution_payload["analysis_gene_scores"] = normalized_rows
            resolution_payload["analysis_gene_score_count"] = len(normalized_rows)
            return normalized_rows, resolution_payload

        resolved = self._resolve_gene_identity_rows(
            unique_preserve_order(row["gene"] for row in cleaned_rows),
            species=species,
            warnings=warnings,
            limit=len(cleaned_rows),
        )
        resolved_by_input = {
            normalize_gene_token(row.get("input_gene"), species=species): normalize_gene_token(
                row.get("canonical_symbol"), species=species
            )
            for row in resolved["resolved_genes"]
            if normalize_gene_token(row.get("input_gene"), species=species)
        }
        seen: dict[str, float] = {}
        for row in cleaned_rows:
            gene = row["gene"]
            score = float(row["score"])
            normalized_gene = resolved_by_input.get(gene) or gene
            current = seen.get(normalized_gene)
            if current is None or abs(score) > abs(current):
                seen[normalized_gene] = score
        normalized_rows = [{"gene": gene, "score": score} for gene, score in seen.items()]
        normalized_rows.sort(key=lambda row: abs(float(row["score"])), reverse=True)
        resolution_payload.update(resolved)
        resolution_payload["analysis_gene_scores"] = normalized_rows
        resolution_payload["analysis_gene_score_count"] = len(normalized_rows)
        resolution_payload["resolution_used"] = True
        return normalized_rows, resolution_payload

    def _is_human_species(self, species: str) -> bool:
        token = clean_text(species).lower().replace(" ", "_")
        return token in {"human", "homo_sapiens", "9606"}

    def _is_mouse_species(self, species: str) -> bool:
        token = clean_text(species).lower().replace(" ", "_")
        return token in {"mouse", "mus_musculus", "10090"}

    def _is_pseudomonas_species(self, species: str) -> bool:
        token = clean_text(species).lower().replace(" ", "_")
        return token in {
            "pseudomonas_aeruginosa",
            "pseudomonas_aeruginosa_ucbpp_pa14",
            "pseudomonas_aeruginosa_pa14",
            "208963",
            "pau",
        }

    def _pathway_species_for_genes(self, genes: list[str], *, species: str) -> str:
        """Refine a generic Pseudomonas label when stable locus tags identify PA14."""
        token = clean_text(species).lower().replace(" ", "_")
        if token in {"pseudomonas_aeruginosa", "208963", "pau"} and any(
            clean_text(gene).upper().startswith("PA14_") for gene in genes
        ):
            return "pseudomonas_aeruginosa_ucbpp_pa14"
        return species

    def _require_human_species(self, species: str, *, tool_name: str) -> None:
        if self._is_human_species(species):
            return
        raise ProviderError(f"{tool_name} currently supports human studies only. Received species '{species}'.")

    def _reactome_context_for_terms(
        self,
        terms: list[dict[str, Any]] | Any,
        *,
        warnings: list[str],
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in (terms or []) if isinstance(row, dict)]
        reactome_terms = [row for row in rows if clean_text(row.get("source")).upper() == "REAC"]
        details: list[dict[str, Any]] = []
        for row in reactome_terms[: max(1, min(int(limit), 10))]:
            pathway_id = clean_text(row.get("term_id"))
            if not pathway_id:
                continue
            try:
                detail = self.reactome.pathway(
                    pathway_id,
                    participant_limit=8,
                    timeout_seconds=5,
                    participant_timeout_seconds=3,
                )
                payload = dict(detail.payload)
                payload["enrichment_term"] = row
                details.append(payload)
            except ProviderError as exc:
                warnings.append(str(exc))
        return details

    @staticmethod
    def _enrichment_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            token = clean_text(value)
            return [token] if token else []
        if isinstance(value, (list, tuple, set)):
            return unique_preserve_order(item for item in value if clean_text(item))
        token = clean_text(value)
        return [token] if token else []

    @staticmethod
    def _enrichment_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            return None
        return parsed

    @staticmethod
    def _enrichment_ratio_parts(value: Any) -> tuple[int | None, int | None]:
        token = clean_text(value)
        if not token or "/" not in token:
            return None, None
        left, right = token.split("/", 1)
        numerator = GLESKitService._enrichment_int(left)
        denominator = GLESKitService._enrichment_int(right)
        return numerator, denominator

    @staticmethod
    def _enrichment_safe_ratio(numerator: int | None, denominator: int | None) -> float | None:
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return float(numerator / denominator)

    @staticmethod
    def _enrichment_odds_ratio(query_size: int | None, term_size: int | None, intersection_size: int | None, background_size: int | None) -> float | None:
        if query_size is None or term_size is None or intersection_size is None or background_size is None:
            return None
        if query_size <= 0 or term_size <= 0 or background_size <= 0:
            return None
        a = float(max(0, intersection_size))
        b = float(max(0, query_size - intersection_size))
        c = float(max(0, term_size - intersection_size))
        d = float(max(0, background_size - a - b - c))
        if b <= 0 or c <= 0:
            if a > 0 and d > 0:
                return float("inf")
            return None
        return float((a * d) / (b * c))

    def _standardize_enrichment_term(
        self,
        term: dict[str, Any],
        *,
        species: str,
        query_size: int,
        background_size: int | None = None,
        backend: str = "",
    ) -> dict[str, Any]:
        row = dict(term)
        gene_ids = self._enrichment_string_list(row.get("gene_ids") or row.get("core_gene_ids"))
        matched_genes = self._enrichment_string_list(row.get("matched_genes") or row.get("core_matched_genes"))
        if not gene_ids:
            gene_ids = matched_genes[:]
        if not matched_genes:
            matched_genes = gene_ids[:]

        term_size = self._enrichment_int(row.get("term_size") or row.get("set_size"))
        if not term_size or term_size <= 0:
            term_size = self._enrichment_ratio_parts(row.get("background_ratio"))[0]
        if not term_size or term_size <= 0:
            term_size = max(len(gene_ids), len(matched_genes))

        row_query_size = self._enrichment_int(row.get("query_size")) or query_size
        if not row_query_size or row_query_size <= 0:
            row_query_size = self._enrichment_ratio_parts(row.get("gene_ratio"))[1]
        if not row_query_size or row_query_size <= 0:
            row_query_size = max(len(gene_ids), len(matched_genes))

        row_background_size = self._enrichment_int(row.get("background_size")) or background_size
        if not row_background_size or row_background_size <= 0:
            row_background_size = self._enrichment_ratio_parts(row.get("background_ratio"))[1]
        if not row_background_size or row_background_size <= 0:
            row_background_size = max(row_query_size, term_size)

        intersection_size = self._enrichment_int(row.get("intersection_size"))
        if not intersection_size or intersection_size <= 0:
            intersection_size = self._enrichment_int(row.get("count"))
        if not intersection_size or intersection_size <= 0:
            intersection_size = len(matched_genes)

        row["gene_ids"] = gene_ids
        row["matched_genes"] = matched_genes
        row["query_size"] = row_query_size
        row["background_size"] = row_background_size
        row["term_size"] = term_size
        row["intersection_size"] = intersection_size
        row["precision"] = self._enrichment_safe_ratio(intersection_size, row_query_size) if row.get("precision") is None else row.get("precision")
        row["recall"] = self._enrichment_safe_ratio(intersection_size, term_size) if row.get("recall") is None else row.get("recall")
        row["odds_ratio"] = self._enrichment_odds_ratio(row_query_size, term_size, intersection_size, row_background_size) if row.get("odds_ratio") is None else row.get("odds_ratio")
        if not clean_text(row.get("gene_ratio")):
            row["gene_ratio"] = f"{intersection_size}/{max(1, row_query_size)}"
        if not clean_text(row.get("background_ratio")):
            row["background_ratio"] = f"{term_size}/{max(1, row_background_size)}"
        row["backend"] = clean_text(backend) or clean_text(row.get("backend")) or "clusterProfiler"
        row["species"] = species
        return row

    def _standardize_enrichment_payload(
        self,
        payload: dict[str, Any],
        *,
        species: str,
        query_size: int,
        background_size: int | None = None,
        backend: str = "",
    ) -> dict[str, Any]:
        standardized = dict(payload)
        standardized["species"] = species
        standardized["backend"] = clean_text(backend) or clean_text(standardized.get("backend")) or "clusterProfiler"
        standardized_query_size = self._enrichment_int(standardized.get("query_size")) or query_size
        standardized_background_size = self._enrichment_int(standardized.get("background_size")) or background_size
        terms = [
            self._standardize_enrichment_term(
                row,
                species=species,
                query_size=standardized_query_size,
                background_size=standardized_background_size,
                backend=standardized["backend"],
            )
            for row in (standardized.get("terms") or [])
            if isinstance(row, dict)
        ]
        standardized["query_size"] = standardized_query_size
        if standardized_background_size is not None:
            standardized["background_size"] = standardized_background_size
        standardized["terms"] = terms
        standardized["term_count"] = len(terms)
        requested_focus_terms = [
            clean_text(value)
            for value in (standardized.get("focus_terms_requested") or [])
            if clean_text(value)
        ][:10]
        focused_terms = [
            self._standardize_enrichment_term(
                row,
                species=species,
                query_size=standardized_query_size,
                background_size=standardized_background_size,
                backend=standardized["backend"],
            )
            for row in (standardized.get("focused_terms") or [])
            if isinstance(row, dict)
        ]
        standardized["focus_terms_requested"] = requested_focus_terms
        standardized["focus_terms_found"] = [
            clean_text(value)
            for value in (standardized.get("focus_terms_found") or [])
            if clean_text(value)
        ][:10]
        standardized["focused_terms"] = focused_terms[:20]
        standardized = self._attach_enrichment_highlights(standardized)
        if terms and standardized_background_size is None:
            standardized["background_size"] = max(self._enrichment_int(term.get("background_size")) or 0 for term in terms)
        if "positive_terms" in standardized:
            standardized["positive_terms"] = [
                self._standardize_enrichment_term(
                    row,
                    species=species,
                    query_size=standardized_query_size,
                    background_size=standardized_background_size,
                    backend=standardized["backend"],
                )
                for row in (standardized.get("positive_terms") or [])
                if isinstance(row, dict)
            ]
        if "negative_terms" in standardized:
            standardized["negative_terms"] = [
                self._standardize_enrichment_term(
                    row,
                    species=species,
                    query_size=standardized_query_size,
                    background_size=standardized_background_size,
                    backend=standardized["backend"],
                )
                for row in (standardized.get("negative_terms") or [])
                if isinstance(row, dict)
            ]
        return standardized

    def _safe_enrichment_context(
        self,
        genes: list[str],
        *,
        species: str,
        label: str,
        warnings: list[str],
        background_genes: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_genes, resolution_payload = self._prepare_pathway_gene_inputs(
            genes,
            species=species,
            warnings=warnings,
        )
        if len(normalized_genes) < 2:
            return {
                "label": label,
                "species": species,
                "genes": normalized_genes,
                "input_genes": normalize_gene_list(genes, species=species),
                "normalized_gene_set": resolution_payload,
                "backend": "clusterProfiler",
                "query_size": len(normalized_genes),
                "background_size": len(normalized_genes),
                "term_count": 0,
                "terms": [],
                "reactome_context": [],
            }
        pathway_species = self._pathway_species_for_genes(normalized_genes, species=species)
        pathway_database = "KEGG" if self._is_pseudomonas_species(pathway_species) else ""
        try:
            payload, _provider_name, _summary = self._pathway_enrichment_context(
                normalized_genes,
                species=pathway_species,
                label=label,
                warnings=warnings,
                database=pathway_database,
                background_genes=background_genes,
            )
            payload["input_genes"] = normalize_gene_list(genes, species=species)
            payload["normalized_gene_set"] = resolution_payload
            payload["genes"] = normalized_genes
            payload["requested_species"] = species
            return payload
        except ProviderError as exc:
            warnings.append(str(exc))
            return {
                "label": label,
                "species": species,
                "genes": normalized_genes,
                "input_genes": normalize_gene_list(genes, species=species),
                "normalized_gene_set": resolution_payload,
                "backend": "clusterProfiler",
                "query_size": len(normalized_genes),
                "background_size": len(normalized_genes),
                "term_count": 0,
                "terms": [],
                "reactome_context": [],
            }

    def _gene_set_disease_context_payload(
        self,
        genes: list[str],
        *,
        species: str,
        label: str,
        database: str,
        limit: int,
        warnings: list[str],
        resolution_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        cleaned_genes = normalize_gene_list(genes, species=species)
        if resolution_payload is None:
            resolution_payload = self._resolve_gene_identity_rows(
                cleaned_genes,
                species=species,
                warnings=warnings,
            )
        resolved_entries = resolution_payload["resolved_genes"]
        resolved_with_entrez = [row for row in resolved_entries if clean_text(row.get("entrez_gene_id"))]
        if not resolved_with_entrez:
            raise ProviderError(
                "gene_set_disease_context could not resolve any input genes to Entrez identifiers required by the local disease-enrichment backend."
            )

        backend_result = self.yulab_backend.gene_set_disease_context(
            entrez_gene_ids=[str(row["entrez_gene_id"]) for row in resolved_with_entrez],
            database=database,
            limit=limit,
        )
        payload = dict(backend_result.payload)
        payload["input_genes"] = cleaned_genes
        payload["label"] = clean_text(label) or None
        payload["species"] = species
        payload["database"] = clean_text(database).upper() or "DO"
        payload["normalized_gene_set"] = resolution_payload

        symbol_map: dict[str, str] = {}
        for entry in resolved_with_entrez:
            entrez_gene_id = clean_text(entry.get("entrez_gene_id"))
            symbol = clean_text(entry.get("canonical_symbol")).upper()
            if entrez_gene_id and symbol and entrez_gene_id not in symbol_map:
                symbol_map[entrez_gene_id] = symbol

        terms = [dict(row) for row in (payload.get("terms") or []) if isinstance(row, dict)]
        for row in terms:
            row["source"] = clean_text(row.get("source")).upper() or payload["database"]
            gene_ids = [
                clean_text(item)
                for item in (row.get("gene_ids") or [])
                if clean_text(item)
            ]
            matched_genes = unique_preserve_order(symbol_map.get(item) or item for item in gene_ids)
            row["gene_ids"] = gene_ids
            row["matched_genes"] = matched_genes
            row["intersection_size"] = int(row.get("intersection_size") or len(matched_genes) or 0)
        payload["terms"] = terms
        payload["term_count"] = len(terms)

        summary = backend_result.summary
        if resolution_payload["unresolved_genes"]:
            summary += f" {len(resolution_payload['unresolved_genes'])} gene(s) could not be normalized locally."
        return payload, backend_result.provider, summary

    def _safe_gene_set_disease_context(
        self,
        genes: list[str],
        *,
        species: str,
        label: str,
        warnings: list[str],
        resolution_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(normalize_gene_list(genes, species=species)) < 2 or not self._is_human_species(species):
            return {
                "label": label,
                "species": species,
                "analysis_mode": "ora",
                "database": "DO",
                "term_count": 0,
                "terms": [],
            }
        try:
            payload, _provider_name, _summary = self._gene_set_disease_context_payload(
                genes,
                species=species,
                label=label,
                database="DO",
                limit=15,
                warnings=warnings,
                resolution_payload=resolution_payload,
            )
            return payload
        except ProviderError as exc:
            warnings.append(str(exc))
            return {
                "label": label,
                "species": species,
                "analysis_mode": "ora",
                "database": "DO",
                "term_count": 0,
                "terms": [],
            }

    def _pathway_enrichment_context(
        self,
        genes: list[str],
        *,
        species: str,
        label: str,
        warnings: list[str],
        database: str = "",
        library: str = "",
        backend: str = "auto",
        background_genes: list[str] | None = None,
        simplify_go_similarity: float = 0.0,
        focus_terms: list[str] | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        cleaned_genes = normalize_gene_list(genes, species=species)
        cleaned_background = normalize_gene_list(background_genes or [], species=species)
        requested_database = self._normalize_pathway_database(database)
        requested_backend = clean_text(backend).lower() or "auto"
        requested_library = normalize_enrichr_library(library, allow_named=True)
        if not requested_library and requested_backend in {"enrichr", "gseapy", "gseapy_enrichr"}:
            requested_library = normalize_enrichr_library(database, allow_named=True)
        requested_focus_terms = list(
            dict.fromkeys(clean_text(term) for term in focus_terms or [] if clean_text(term))
        )[:10]
        if requested_backend not in {"auto", "clusterprofiler", "enrichr", "gseapy", "gseapy_enrichr"}:
            raise ProviderError(
                "Unsupported enrichment backend. Use backend='auto', 'clusterprofiler', or 'enrichr'."
            )
        if len(cleaned_genes) < 2:
            empty_payload = {
                "label": label,
                "species": species,
                "genes": cleaned_genes,
                "database": requested_database or None,
                "term_count": 0,
                "terms": [],
                "focus_terms_requested": requested_focus_terms,
                "focus_terms_found": [],
                "focused_terms": [],
                "reactome_context": [],
            }
            return empty_payload, "local", "Not enough genes were available for enrichment."

        use_enrichr = requested_backend in {"enrichr", "gseapy", "gseapy_enrichr"} or (
            requested_backend == "auto" and bool(requested_library)
        )
        if use_enrichr:
            if not requested_library:
                raise ProviderError(
                    "Enrichr enrichment requires library='GO_Biological_Process_2021' or library='Reactome_2022'."
                )
            if requested_backend == "clusterprofiler":
                raise ProviderError("library selects an Enrichr library; use backend='enrichr' or omit backend.")
            enrichr_result = self.enrichr.enrich_genes(
                cleaned_genes,
                library=requested_library,
                species=species,
                limit=15,
                background_genes=cleaned_background,
                focus_terms=requested_focus_terms,
            )
            payload = dict(enrichr_result.payload)
            payload["label"] = label
            payload["species"] = species
            payload["genes"] = cleaned_genes
            payload["input_genes"] = cleaned_genes
            payload["background_genes"] = cleaned_background
            payload["database"] = requested_library
            payload["library"] = requested_library
            payload["requested_backend"] = requested_backend
            payload["enrichment_backend"] = "gseapy_enrichr"
            payload = self._standardize_enrichment_payload(
                payload,
                species=species,
                query_size=len(cleaned_genes),
                background_size=len(cleaned_background) or None,
                backend="gseapy_enrichr",
            )
            summary = enrichr_result.summary + self._enrichment_summary_suffix(payload)
            return payload, enrichr_result.provider, summary

        local_error: str | None = None
        local_sources = ["GO:BP", "REAC"]
        gprofiler_sources = None
        if requested_database == "GO_BP":
            local_sources = ["GO:BP"]
            gprofiler_sources = ["GO:BP"]
        elif requested_database == "REAC":
            local_sources = ["REAC"]
            gprofiler_sources = ["REAC"]
        elif requested_database == "KEGG":
            local_sources = ["KEGG"]
            gprofiler_sources = ["KEGG"]
        elif requested_database == "WP":
            gprofiler_sources = ["WP"]
        elif requested_database:
            raise ProviderError("Unsupported enrichment database. Use GO_BP, REAC, KEGG, or WP.")

        use_local = (
            (self._is_human_species(species) or self._is_mouse_species(species) or self._is_pseudomonas_species(species))
            and (not requested_database or requested_database in {"GO_BP", "REAC", "KEGG"})
        )
        if requested_backend == "clusterprofiler" and not use_local:
            raise ProviderError(
                "The requested clusterProfiler backend is unavailable for this species or enrichment database.",
                error_code="invalid_input",
                retryable=False,
                suggested_next_step="Use backend='auto' for an eligible fallback or select a local GO, Reactome, or KEGG route.",
            )
        if use_local:
            try:
                local_result = self._call_yulab_backend(
                    "pathway_enrichment",
                    genes=cleaned_genes,
                    species=species,
                    sources=local_sources,
                    limit=15,
                    background_genes=cleaned_background,
                    simplify_go_similarity=simplify_go_similarity,
                    focus_terms=requested_focus_terms,
                )
                payload = dict(local_result.payload)
                reactome_context = self._reactome_context_for_terms(payload.get("terms", []), warnings=warnings)
                if reactome_context:
                    payload["reactome_context"] = reactome_context
                payload["label"] = label
                payload["species"] = species
                payload["genes"] = cleaned_genes
                payload["background_genes"] = cleaned_background
                payload["database"] = requested_database or ",".join(local_sources)
                payload["requested_backend"] = requested_backend
                payload["fallback_used"] = False
                payload["simplify_go_similarity"] = float(simplify_go_similarity or 0.0)
                payload = self._standardize_enrichment_payload(
                    payload,
                    species=species,
                    query_size=len(cleaned_genes),
                    background_size=len(cleaned_background) if cleaned_background else None,
                    backend="clusterProfiler",
                )
                summary = local_result.summary
                if reactome_context:
                    summary += f" Added Reactome detail for {len(reactome_context)} enriched pathway(s)."
                summary += self._enrichment_summary_suffix(payload)
                return payload, local_result.provider, summary
            except ProviderError as exc:
                local_error = str(exc)

        if requested_backend == "clusterprofiler":
            raise ProviderError(
                f"The requested clusterProfiler enrichment failed: {local_error or 'local backend unavailable.'}",
                error_code="provider_timeout" if "timeout" in (local_error or "").lower() else "provider_error",
                retryable=False,
                suggested_next_step="Use the reported local-backend diagnostic; do not substitute a different backend when the requested method is required.",
                diagnostic=local_error or "Local clusterProfiler backend was unavailable.",
            )

        try:
            gprofiler_kwargs: dict[str, Any] = {
                "species": species,
                "sources": gprofiler_sources,
            }
            if requested_focus_terms:
                gprofiler_kwargs["focus_terms"] = requested_focus_terms
            result = self.gprofiler.enrich_genes(cleaned_genes, **gprofiler_kwargs)
            payload = dict(result.payload)
            reactome_context = self._reactome_context_for_terms(payload.get("terms", []), warnings=warnings)
            if reactome_context:
                payload["reactome_context"] = reactome_context
            payload["label"] = label
            payload["species"] = species
            payload["background_genes"] = cleaned_background
            payload["database"] = requested_database or None
            payload["requested_backend"] = requested_backend
            payload["fallback_used"] = bool(local_error)
            payload["simplify_go_similarity"] = float(simplify_go_similarity or 0.0)
            payload = self._standardize_enrichment_payload(
                payload,
                species=species,
                query_size=len(cleaned_genes),
                background_size=len(cleaned_background) if cleaned_background else None,
                backend="gprofiler",
            )
            summary = result.summary
            if reactome_context:
                summary += f" Added Reactome detail for {len(reactome_context)} enriched pathway(s)."
            if local_error:
                payload["local_backend_warning"] = local_error
            if cleaned_background and requested_database in {"KEGG", "WP"}:
                payload["background_warning"] = "background_genes are ignored for KEGG and WP enrichment."
            summary += self._enrichment_summary_suffix(payload)
            return payload, result.provider, summary
        except ProviderError as exc:
            if local_error:
                raise ProviderError(f"{local_error} Fallback g:Profiler lookup also failed: {exc}") from exc
            raise

    def _safe_ranked_gsea_context(
        self,
        ranked_gene_scores: list[dict[str, Any]],
        *,
        species: str,
        label: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        normalized_scores, resolution_payload = self._prepare_ranked_gene_scores(
            ranked_gene_scores,
            species=species,
            warnings=warnings,
        )
        if len(normalized_scores) < 4 or not (
            self._is_human_species(species) or self._is_mouse_species(species) or self._is_pseudomonas_species(species)
        ):
            return {
                "label": label,
                "species": species,
                "analysis_mode": "gsea",
                "sources": ["KEGG"] if self._is_pseudomonas_species(species) else ["GO:BP", "REAC"],
                "ranked_gene_scores": normalized_scores,
                "input_gene_scores": [dict(row) for row in ranked_gene_scores if isinstance(row, dict)],
                "normalized_gene_set": resolution_payload,
                "gene_score_count": len(normalized_scores),
                "query_size": len(normalized_scores),
                "background_size": len(normalized_scores),
                "backend": "clusterProfiler",
                "term_count": 0,
                "terms": [],
                "positive_terms": [],
                "negative_terms": [],
            }
        try:
            gsea_sources = ["KEGG"] if self._is_pseudomonas_species(species) else ["GO:BP", "REAC"]
            result = self._call_yulab_backend(
                "ranked_pathway_gsea",
                ranked_gene_scores=normalized_scores,
                species=species,
                sources=gsea_sources,
                limit=15,
            )
            payload = dict(result.payload)
            payload["label"] = label
            payload["species"] = species
            payload["ranked_gene_scores"] = normalized_scores
            payload["input_gene_scores"] = [dict(row) for row in ranked_gene_scores if isinstance(row, dict)]
            payload["normalized_gene_set"] = resolution_payload
            payload["gene_score_count"] = len(normalized_scores)
            payload = self._standardize_enrichment_payload(
                payload,
                species=species,
                query_size=len(normalized_scores),
                background_size=len(normalized_scores),
                backend="clusterProfiler",
            )
            return payload
        except ProviderError as exc:
            warnings.append(str(exc))
            return {
                "label": label,
                "species": species,
                "analysis_mode": "gsea",
                "sources": ["GO:BP", "REAC"],
                "ranked_gene_scores": normalized_scores,
                "input_gene_scores": [dict(row) for row in ranked_gene_scores if isinstance(row, dict)],
                "normalized_gene_set": resolution_payload,
                "gene_score_count": len(normalized_scores),
                "query_size": len(normalized_scores),
                "background_size": len(normalized_scores),
                "backend": "clusterProfiler",
                "term_count": 0,
                "terms": [],
                "positive_terms": [],
                "negative_terms": [],
            }

    def _resolve_gene_tokens(
        self,
        genes: list[str],
        *,
        species: str,
        limit: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        input_genes = unique_preserve_order(genes)[: max(1, limit)]
        resolved_entries: list[dict[str, Any]] = []
        unresolved_genes: list[str] = []
        for gene in input_genes:
            try:
                resolution = self._resolve_gene_provider_result(gene, species=species)
            except ProviderError as exc:
                warnings.append(str(exc))
                unresolved_genes.append(gene)
                continue
            matches = resolution.payload.get("matches", [])
            if not matches:
                unresolved_genes.append(gene)
                continue
            best = dict(matches[0])
            resolved_entries.append(
                {
                    "input_gene": gene,
                    "canonical_symbol": normalize_gene_token(best.get("symbol"), species=species)
                    or normalize_gene_token(gene, species=species),
                    "ensembl_gene_id": best.get("ensembl_gene_id"),
                    "name": best.get("name"),
                    "matched_on": best.get("matched_on"),
                    "matched_value": best.get("matched_value"),
                }
            )
        return {
            "input_genes": input_genes,
            "resolved_genes": unique_preserve_order(row["canonical_symbol"] for row in resolved_entries),
            "resolved_entries": resolved_entries,
            "unresolved_genes": unresolved_genes,
        }

    def _resolve_gene_identity_rows(
        self,
        genes: list[str],
        *,
        species: str,
        warnings: list[str],
        limit: int = 300,
    ) -> dict[str, Any]:
        input_genes = unique_preserve_order(genes)[: max(1, int(limit))]
        resolved_rows: list[dict[str, Any]] = []
        unresolved_genes: list[str] = []
        unresolved_without_entrez: list[str] = []
        provider_usage: dict[str, int] = {}
        resolutions_by_input: dict[str, Any] = {}
        batch_resolver = getattr(self.reference_catalog, "resolve_genes", None)
        if callable(batch_resolver):
            try:
                resolutions_by_input.update(dict(batch_resolver(input_genes, species=species) or {}))
            except ProviderError as exc:
                warnings.append(str(exc))

        # Keep the local reference catalog as the first choice, then resolve only
        # still-unmapped identifier-like values in bounded provider batches. This
        # avoids a network request per row in a large RNA-seq handoff.
        needs_full_resolution = len(input_genes) <= 50
        fallback_candidates = [
            gene
            for gene in input_genes
            if gene not in resolutions_by_input
            and (needs_full_resolution or self._gene_token_needs_resolution(gene))
        ]
        mygene_batch = getattr(self.mygene, "resolve_genes", None)
        if fallback_candidates and callable(mygene_batch):
            try:
                resolutions_by_input.update(dict(mygene_batch(fallback_candidates, species=species) or {}))
            except ProviderError as exc:
                warnings.append(str(exc))
        elif fallback_candidates:
            # Third-party provider adapters may not expose the batch protocol.
            # Keep their compatibility fallback bounded so an older adapter can
            # never turn a high-cardinality handoff into thousands of requests.
            fallback_limit = min(len(fallback_candidates), 50)
            for gene in fallback_candidates[:fallback_limit]:
                try:
                    resolution = self._resolve_gene_provider_result(gene, species=species)
                except ProviderError as exc:
                    warnings.append(str(exc))
                    continue
                if resolution.payload.get("matches"):
                    resolutions_by_input[gene] = resolution
            if len(fallback_candidates) > fallback_limit:
                warnings.append(
                    f"Skipped individual online fallback for {len(fallback_candidates) - fallback_limit} unresolved identifiers; "
                    "use a batch-capable provider or refresh the local reference catalog."
                )

        unresolved_ensembl = [
            gene
            for gene in fallback_candidates
            if gene not in resolutions_by_input and clean_text(gene).upper().startswith("ENS")
        ]
        ensembl_batch = getattr(self.ensembl, "resolve_genes", None)
        if unresolved_ensembl and callable(ensembl_batch):
            try:
                resolutions_by_input.update(dict(ensembl_batch(unresolved_ensembl, species=species) or {}))
            except ProviderError as exc:
                warnings.append(str(exc))
        for gene in input_genes:
            try:
                resolution = resolutions_by_input.get(gene)
            except ProviderError as exc:
                warnings.append(str(exc))
                unresolved_genes.append(gene)
                continue
            if resolution is None:
                if gene in fallback_candidates:
                    unresolved_genes.append(gene)
                continue
            matches = resolution.payload.get("matches", [])
            if not matches:
                unresolved_genes.append(gene)
                continue
            best = dict(matches[0])
            provider_name = clean_text(getattr(resolution, "provider", "")) or "unknown"
            provider_usage[provider_name] = provider_usage.get(provider_name, 0) + 1
            row = {
                "input_gene": gene,
                "canonical_symbol": normalize_gene_token(best.get("symbol"), species=species)
                or normalize_gene_token(gene, species=species),
                "name": clean_text(best.get("name")) or None,
                "summary": clean_text(best.get("summary")) or None,
                "biotype": clean_text(best.get("biotype")) or None,
                "entrez_gene_id": clean_text(best.get("entrez_gene_id")) or None,
                "ensembl_gene_id": clean_text(best.get("ensembl_gene_id")) or None,
                "matched_on": clean_text(best.get("matched_on")) or None,
                "matched_value": clean_text(best.get("matched_value")) or None,
                "provider": provider_name,
            }
            if not row["entrez_gene_id"]:
                unresolved_without_entrez.append(gene)
            resolved_rows.append(row)
        return {
            "input_genes": input_genes,
            "resolved_gene_count": len(resolved_rows),
            "resolved_with_entrez_count": len([row for row in resolved_rows if row.get("entrez_gene_id")]),
            "resolved_genes": resolved_rows,
            "unresolved_genes": unresolved_genes,
            "unresolved_without_entrez": unresolved_without_entrez,
            "provider_usage": provider_usage,
        }

    def _lead_gene_disease_rows(
        self,
        lead_genes: list[str],
        *,
        lead_gene_map: dict[str, dict[str, Any]],
        warnings: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        ordered_genes = unique_preserve_order(clean_text(gene) for gene in lead_genes if clean_text(gene))
        request_items: list[tuple[str, str]] = []
        for gene_symbol in ordered_genes:
            entry = lead_gene_map.get(gene_symbol) or {}
            ensembl_gene_id = clean_text(entry.get("ensembl_gene_id"))
            if ensembl_gene_id:
                request_items.append((gene_symbol, ensembl_gene_id))
        if not request_items:
            return []

        rows_by_gene: dict[str, list[dict[str, Any]]] = {gene_symbol: [] for gene_symbol, _ in request_items}
        max_workers = max(1, min(len(request_items), int(getattr(self.settings, "local_max_workers", 1) or 1)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.opentargets.associated_diseases, ensembl_gene_id, limit=limit): gene_symbol
                for gene_symbol, ensembl_gene_id in request_items
            }
            for future in as_completed(future_map):
                gene_symbol = future_map[future]
                try:
                    result = future.result()
                except ProviderError as exc:
                    warnings.append(str(exc))
                    continue
                for row in result.payload.get("diseases", []):
                    merged = dict(row)
                    merged["gene_symbol"] = gene_symbol
                    rows_by_gene.setdefault(gene_symbol, []).append(merged)

        rows: list[dict[str, Any]] = []
        for gene_symbol in ordered_genes:
            rows.extend(rows_by_gene.get(gene_symbol, []))
        return rows

    def _capture_to_active_session(
        self,
        *,
        kind: str,
        title: str,
        summary: str,
        payload: dict[str, Any],
        provider: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status = self.workspace_gateway.status()
        info = {
            **status,
            "saved": False,
            "artifact_id": None,
        }
        pair = self.workspace_gateway.get_workspace()
        if pair is None:
            return info
        runtime, workspace = pair
        if not runtime.capture_enabled or not runtime.active_session_id:
            return info
        try:
            artifact = workspace.capture_artifact(
                runtime.active_session_id,
                kind=kind,
                title=title,
                summary=summary,
                payload=payload,
                provider=provider,
                provenance=provenance,
            )
        except Exception as exc:  # pragma: no cover - defensive
            info["error"] = str(exc)
            return info
        info["saved"] = True
        info["artifact_id"] = artifact.artifact_id
        return info

    def _capture_to_session_or_active(
        self,
        *,
        session_id: str = "",
        kind: str,
        title: str,
        summary: str,
        payload: dict[str, Any],
        provider: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture explicitly to one session, otherwise preserve stateless-first behavior."""
        requested_session = clean_text(session_id)
        if not requested_session:
            return self._capture_to_active_session(
                kind=kind,
                title=title,
                summary=summary,
                payload=payload,
                provider=provider,
                provenance=provenance,
            )
        status = self.workspace_gateway.status()
        info = {**status, "saved": False, "artifact_id": None, "session_id": requested_session}
        pair = self.workspace_gateway.get_workspace()
        if pair is None:
            info["error"] = "Workspace is unavailable."
            return info
        _, workspace = pair
        try:
            artifact = workspace.capture_artifact(
                requested_session,
                kind=kind,
                title=title,
                summary=summary,
                payload=payload,
                provider=provider,
                provenance={**dict(provenance or {}), "explicit_session_persistence": True},
            )
        except Exception as exc:  # pragma: no cover - defensive
            info["error"] = str(exc)
            return info
        info["saved"] = True
        info["artifact_id"] = artifact.artifact_id
        return info

    def _capture_artifacts_to_active_session(
        self,
        specs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        status = self.workspace_gateway.status()
        info = {
            **status,
            "saved": False,
            "artifact_id": None,
            "artifact_ids": [],
        }
        pair = self.workspace_gateway.get_workspace()
        if pair is None:
            return info
        runtime, workspace = pair
        if not runtime.capture_enabled or not runtime.active_session_id:
            return info

        artifact_ids: list[str] = []
        try:
            for spec in specs:
                artifact = workspace.capture_artifact(
                    runtime.active_session_id,
                    kind=str(spec.get("kind") or "artifact"),
                    title=str(spec.get("title") or "").strip(),
                    summary=str(spec.get("summary") or "").strip(),
                    payload=dict(spec.get("payload") or {}),
                    provider=str(spec.get("provider") or "").strip() or None,
                    provenance=dict(spec.get("provenance") or {}),
                )
                artifact_ids.append(artifact.artifact_id)
        except Exception as exc:  # pragma: no cover - defensive
            info["error"] = str(exc)
            info["artifact_ids"] = artifact_ids
            return info

        info["saved"] = bool(artifact_ids)
        info["artifact_ids"] = artifact_ids
        info["artifact_id"] = artifact_ids[0] if artifact_ids else None
        return info

    def _workspace_artifact_to_model(self, payload: dict[str, Any]) -> Artifact:
        return Artifact(
            artifact_id=clean_text(payload.get("artifact_id")) or new_id("artifact"),
            session_id=clean_text(payload.get("session_id")) or "workspace",
            kind=clean_text(payload.get("kind")) or "artifact",
            title=clean_text(payload.get("title")) or "Workspace artifact",
            summary=clean_text(payload.get("summary")) or "",
            created_at=clean_text(payload.get("created_at")) or utc_now_iso(),
            provider=clean_text(payload.get("provider")) or None,
            provenance=dict(payload.get("provenance") or {}),
            payload=dict(payload.get("payload") or {}),
        )

    def _normalize_analysis_axis(self, analysis_axis: str, *, default: str = "columns") -> str:
        return self.expression_matrix_backend.normalize_analysis_axis(analysis_axis, default=default)  # type: ignore[return-value]

    @staticmethod
    def _looks_like_workspace_selector(token: str) -> bool:
        cleaned = clean_text(token)
        return cleaned.startswith("upload_") or cleaned.startswith("artifact_")

    @staticmethod
    def _nested_string(payload: dict[str, Any], *path: str) -> str:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                return ""
            current = current.get(key)
        return clean_text(current)

    def _extract_reusable_file_path(self, payload: dict[str, Any]) -> str:
        candidates = [
            self._nested_string(payload, "technical", "path"),
            self._nested_string(payload, "technical", "counts_path"),
            self._nested_string(payload, "technical", "metadata_path"),
            self._nested_string(payload, "data_path"),
            self._nested_string(payload, "path"),
            self._nested_string(payload, "matrix_path"),
            self._nested_string(payload, "stored_matrix_path"),
            self._nested_string(payload, "counts_path"),
            self._nested_string(payload, "metadata_path"),
            self._nested_string(payload, "result_path"),
            self._nested_string(payload, "h5ad_path"),
            self._nested_string(payload, "deg_table_path"),
            self._nested_string(payload, "payload", "technical", "path"),
            self._nested_string(payload, "payload", "data_path"),
            self._nested_string(payload, "payload", "path"),
            self._nested_string(payload, "payload", "matrix_path"),
            self._nested_string(payload, "payload", "stored_matrix_path"),
            self._nested_string(payload, "payload", "counts_path"),
            self._nested_string(payload, "payload", "metadata_path"),
            self._nested_string(payload, "payload", "result_path"),
            self._nested_string(payload, "payload", "h5ad_path"),
            self._nested_string(payload, "payload", "deg_table_path"),
            self._nested_string(payload, "payload", "derived_inputs", "matrix_path"),
            self._nested_string(payload, "payload", "input", "path"),
            self._nested_string(payload, "payload", "input", "matrix_path"),
            self._nested_string(payload, "payload", "input", "counts_path"),
            self._nested_string(payload, "payload", "input", "sample_metadata_path"),
            self._nested_string(payload, "payload", "input", "result_path"),
        ]
        for candidate in candidates:
            if candidate:
                return candidate
        return ""

    def _resolve_input_file_selector(
        self,
        selector: str,
        *,
        session_id: str | None = None,
        role: str,
    ) -> str:
        cleaned = clean_text(selector)
        if not cleaned:
            return ""

        expanded = Path(cleaned).expanduser()
        if expanded.exists():
            resolved = expanded.resolve()
            self._validate_eval_input_path(resolved, role=role)
            return str(resolved)

        if _eval_path_policy_enabled() and expanded.is_absolute():
            self._validate_eval_input_path(expanded.resolve(), role=role)

        if not self._looks_like_workspace_selector(cleaned):
            if _eval_path_policy_enabled():
                self._validate_eval_input_path(expanded.resolve(), role=role)
            return cleaned

        pair = self.workspace_gateway.get_workspace()
        if pair is None:
            raise ValueError(
                f"{role} received '{cleaned}', which looks like a GLESKit upload/artifact id, "
                "but workspace is unavailable. Provide a filesystem path instead."
            )
        runtime, workspace = pair
        if cleaned.startswith("upload_"):
            payload = workspace.get_upload(
                session_id,
                cleaned,
                active_session_id=runtime.active_session_id,
            )
        else:
            payload = workspace.get_artifact(
                session_id,
                cleaned,
                active_session_id=runtime.active_session_id,
            )
        resolved = self._extract_reusable_file_path(payload)
        if resolved:
            resolved_path = Path(resolved).expanduser().resolve()
            self._validate_eval_input_path(resolved_path, role=role)
            return str(resolved_path)
        raise ValueError(
            f"{role} selector '{cleaned}' resolved to a GLESKit object that does not expose a reusable file path. "
            "Use the original upload/path, or keep chained matrix work inside one run_pipeline call."
        )

    @staticmethod
    def _validate_eval_input_path(path: Path, *, role: str) -> None:
        if not _eval_path_policy_enabled():
            return
        allowed_paths = _eval_path_entries("GLESKIT_EVAL_ALLOWED_INPUT_PATHS")
        allowed_roots = _eval_path_entries("GLESKIT_EVAL_ALLOWED_INPUT_ROOTS")
        resolved = path.expanduser().resolve()
        if resolved in allowed_paths or any(
            resolved == root or root in resolved.parents for root in allowed_roots
        ):
            return
        diagnostic = (
            f"{role} path is outside the controlled BixBench input boundary: {resolved}. "
            "Use only the current capsule visible files or generated analysis handoffs."
        )
        raise ProviderError(
            diagnostic,
            error_code="invalid_input",
            retryable=False,
            suggested_next_step="Use the exact visible file path returned by inspection or the generated handoff path.",
            diagnostic=diagnostic,
        )

    def _load_expression_matrix_input(
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
        analysis_axis: str = "columns",
        role: str = "matrix_path",
    ) -> tuple[Any, str, str]:
        normalized_analysis_axis = self._normalize_analysis_axis(analysis_axis)
        resolved_matrix_path = self._resolve_input_file_selector(matrix_path, role=role)
        matrix = self.expression_matrix_backend.load_matrix(
            matrix_path=resolved_matrix_path,
            table_text=table_text,
            row_id_column=row_id_column,
            delimiter=delimiter,
            sheet_name=sheet_name,
            header_row=header_row,
            skip_rows=skip_rows,
            transpose=transpose,
            analysis_axis=normalized_analysis_axis,  # type: ignore[arg-type]
        )
        return matrix, resolved_matrix_path, normalized_analysis_axis

    def _resolve_figure_source_artifact(
        self,
        *,
        figure_source_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
    ) -> tuple[Artifact, dict[str, Any]]:
        cleaned_source_id = clean_text(figure_source_id)
        cleaned_artifact_id = clean_text(artifact_id)
        cleaned_session_id = clean_text(session_id)
        if bool(cleaned_source_id) == bool(cleaned_artifact_id):
            raise ValueError(
                "Provide exactly one figure source selector: either figure_source_id, or artifact_id (with optional session_id)."
            )

        if cleaned_source_id:
            entry = self.figure_sources.get(cleaned_source_id)
            if entry is None:
                raise ValueError(
                    "Unknown or expired figure_source_id. Re-run the upstream analysis tool to create a fresh figure source."
                )
            return entry.artifact, {
                "mode": "stateless",
                "figure_source_id": cleaned_source_id,
                "source_tool": entry.source_tool,
            }

        runtime, workspace = self.workspace_gateway.require_workspace()
        payload = workspace.get_artifact(
            cleaned_session_id or None,
            cleaned_artifact_id,
            active_session_id=runtime.active_session_id,
        )
        artifact = self._workspace_artifact_to_model(payload)
        source_artifact_id = clean_text((artifact.provenance or {}).get("source_artifact_id"))
        if artifact.kind == "figure" and source_artifact_id:
            payload = workspace.get_artifact(
                artifact.session_id,
                source_artifact_id,
                active_session_id=runtime.active_session_id,
            )
            artifact = self._workspace_artifact_to_model(payload)

        return artifact, {
            "mode": "workspace",
            "artifact_id": artifact.artifact_id,
            "session_id": artifact.session_id,
            "artifact_kind": artifact.kind,
        }

    @staticmethod
    def _preferred_auto_figure_types(tool_name: str) -> list[str]:
        mapping = {
            "enrich_gene_set": ["enrichment_barplot"],
            "gene_set_disease_context": ["enrichment_barplot"],
            "ranked_gene_set_gsea": ["enrichment_barplot"],
            "compare_gene_programs": ["compare_programs_barplot"],
            "investigate_gene_set": ["enrichment_barplot"],
            "expression_context": ["expression_context_barplot"],
            "analyze_rnaseq_result": [
                "rnaseq_volcano",
                "rnaseq_up_barplot",
                "rnaseq_down_barplot",
            ],
            "integrate_gene_level_results": ["cross_modal_concordance_scatter"],
        }
        return mapping.get(tool_name, [])

    def _synthetic_artifact_for_tool_result(self, tool_name: str, result: dict[str, Any]) -> Artifact | None:
        structured = result.get("structured_data")
        if not isinstance(structured, dict):
            return None

        kind_map = {
            "enrich_gene_set": "enrichment",
            "gene_set_disease_context": "disease_enrichment",
            "ranked_gene_set_gsea": "ranked_gsea",
            "compare_gene_programs": "program_comparison",
            "investigate_gene_set": "gene_set_investigation",
            "expression_context": "expression_context",
            "analyze_rnaseq_result": "rnaseq_analysis",
            "analyze_differential_result": "differential_result_analysis",
            "integrate_gene_level_results": "gene_level_integration",
        }
        kind = kind_map.get(tool_name)
        if not kind:
            return None

        workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
        session_id = clean_text((workspace or {}).get("active_session_id")) or "stateless"
        artifact_id = clean_text((workspace or {}).get("artifact_id")) or new_id("artifact")
        title = clean_text(result.get("summary")) or clean_text(structured.get("label")) or tool_name.replace("_", " ").title()
        summary = clean_text(result.get("summary")) or f"{tool_name} result"
        provider = clean_text(result.get("provider")) or "gleskit_aggregate"
        return Artifact(
            artifact_id=artifact_id,
            session_id=session_id,
            kind=kind,
            title=title,
            summary=summary,
            created_at=utc_now_iso(),
            provider=provider,
            payload=structured,
            provenance={"tool_name": tool_name, "auto_generated": True},
        )

    def _capture_investigation(
        self,
        *,
        label: str,
        species: str,
        question: str,
        resolution_payload: dict[str, Any],
        enrichment_payload: dict[str, Any],
        enrichment_summary: str,
        disease_payload: dict[str, Any],
        disease_summary: str,
        paper_payload: dict[str, Any],
        paper_summary: str,
        bundle_payload: dict[str, Any],
        workflow_payload: dict[str, Any],
    ) -> dict[str, Any]:
        status = self.workspace_gateway.status()
        info = {
            **status,
            "saved": False,
            "artifact_id": None,
            "child_artifact_ids": [],
        }
        pair = self.workspace_gateway.get_workspace()
        if pair is None:
            return info
        runtime, workspace = pair
        if not runtime.capture_enabled or not runtime.active_session_id:
            return info

        child_artifact_ids: list[str] = []

        def capture(kind: str, title: str, summary: str, payload: dict[str, Any], provider: str | None = None):
            artifact = workspace.capture_artifact(
                runtime.active_session_id or "",
                kind=kind,
                title=title,
                summary=summary,
                payload=payload,
                provider=provider,
                provenance={"species": species, "question": clean_text(question)},
            )
            child_artifact_ids.append(artifact.artifact_id)
            return artifact

        try:
            capture("gene_set_resolution", label or "Gene set resolution", f"Resolved {len(resolution_payload.get('resolved_genes', []))} of {len(resolution_payload.get('input_genes', []))} input gene(s).", resolution_payload, "local")
            capture("enrichment", f"{label} enrichment", enrichment_summary, enrichment_payload, "gprofiler")
            capture("gene_disease_associations", f"{label} disease associations", disease_summary, disease_payload, "open_targets")
            capture("paper_search", f"{label} literature", paper_summary, paper_payload, "europe_pmc")
            capture("evidence_bundle", f"{label} evidence bundle", bundle_payload["summary"], bundle_payload, "local")
            workflow_payload = dict(workflow_payload)
            workflow_payload["child_artifact_ids"] = list(child_artifact_ids)
            artifact = capture(
                "gene_set_investigation",
                label,
                f"Investigated {len(workflow_payload.get('input_genes', []))} gene(s) and assembled an evidence bundle.",
                workflow_payload,
                "local",
            )
        except Exception as exc:  # pragma: no cover - defensive
            info["error"] = str(exc)
            info["child_artifact_ids"] = child_artifact_ids
            return info

        info["saved"] = True
        info["artifact_id"] = artifact.artifact_id
        info["child_artifact_ids"] = child_artifact_ids
        return info

    def _effective_species(self, species: str | None) -> str:
        cleaned = clean_text(species or self.settings.default_species)
        return cleaned or self.settings.default_species

    @staticmethod
    def _workspace_warnings(workspace_info: dict[str, Any]) -> list[str]:
        error = str(workspace_info.get("error") or "").strip()
        return [error] if error else []


def build_default_service(project_root: Path | None = None) -> GLESKitService:
    settings = load_settings(project_root)
    return GLESKitService.from_settings(settings)


__all__ = [
    "GLESKitService",
    "ProviderError",
    "WorkspaceUnavailableError",
    "build_default_service",
]
