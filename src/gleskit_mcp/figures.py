from __future__ import annotations

import csv
import math
import os
from io import StringIO
from pathlib import Path
import tempfile
from typing import Any

_MPLCONFIGDIR = Path(tempfile.gettempdir()) / "gleskit_mcp_mplconfig"
_MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPLCONFIGDIR))

_XDG_CACHE_HOME = Path(tempfile.gettempdir()) / "gleskit_mcp_cache"
_XDG_CACHE_HOME.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("XDG_CACHE_HOME", str(_XDG_CACHE_HOME))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from gleskit_mcp.analysis.r_runner import RJsonScriptRunner
from gleskit_mcp.analysis.rnaseq import (
    _COLUMN_ALIASES,
    _float_or_none,
    _infer_delimiter,
    _is_significant,
    _resolve_column,
    _significance_mode,
)
from gleskit_mcp.config import Settings
from gleskit_mcp.models import Artifact, new_id
from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.utils import clean_text, normalize_gene_list


class FigureCatalog:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.r_runner = RJsonScriptRunner(settings) if settings is not None else None

    def _r_figure_timeout_seconds(self) -> int:
        if self.settings is None:
            return 45
        return max(20, int(getattr(self.settings, "figure_r_timeout_seconds", 45) or 45))

    @staticmethod
    def _figure_file_paths(stem: Path) -> tuple[Path, Path]:
        return stem.with_suffix(".png").resolve(), stem.with_suffix(".svg").resolve()

    @classmethod
    def _figure_file_payload(cls, stem: Path) -> dict[str, Any]:
        png_path, svg_path = cls._figure_file_paths(stem)
        if not png_path.exists():
            raise ProviderError("The figure backend did not create the expected PNG file.")
        figure_files = [str(png_path)]
        svg_value = ""
        if svg_path.exists():
            svg_value = str(svg_path)
            figure_files.append(svg_value)
        return {
            "png_path": str(png_path),
            "svg_path": svg_value,
            "figure_files": figure_files,
            "display_path": str(png_path),
        }

    @staticmethod
    def _normalize_figure_token(value: str) -> str:
        return "".join(ch for ch in clean_text(value).lower() if ch.isalnum())

    def _resolve_requested_figure_type(self, artifact: Artifact, figure_type: str) -> str:
        requested = clean_text(figure_type)
        supported = [str(row.get("figure_type") or "") for row in self.figure_options(artifact) if clean_text(row.get("figure_type"))]
        if requested in supported:
            return requested

        normalized_supported = {
            self._normalize_figure_token(token): token
            for token in supported
        }
        normalized_requested = self._normalize_figure_token(requested)
        if normalized_requested in normalized_supported:
            return normalized_supported[normalized_requested]

        alias_target = {
            "enrichmentmap": "enrichment_emapplot",
            "emapplot": "enrichment_emapplot",
            "geneconceptnetwork": "enrichment_cnetplot",
            "geneconceptgraph": "enrichment_cnetplot",
            "cnetplot": "enrichment_cnetplot",
            "diseaseenrichmentmap": "disease_emapplot",
            "diseaseemapplot": "disease_emapplot",
            "diseasegeneconceptnetwork": "disease_cnetplot",
            "diseasegeneconceptgraph": "disease_cnetplot",
            "diseasecnetplot": "disease_cnetplot",
            "ppinetwork": "enrichment_ppi_network",
            "genegenenetwork": "enrichment_ppi_network",
            "proteinproteininteractionnetwork": "enrichment_ppi_network",
            "diseaseppinetwork": "disease_ppi_network",
            "diseasegenegenenetwork": "disease_ppi_network",
            "rankedgeaplot": "ranked_gsea_plot",
            "rankedgseaplot": "ranked_gsea_plot",
            "gseaplot": "ranked_gsea_plot",
            "runningscoreplot": "ranked_gsea_plot",
            "compareclusteremapplot": "compare_programs_emapplot",
            "compareclusterenrichmentmap": "compare_programs_emapplot",
            "compareclustercnetplot": "compare_programs_cnetplot",
            "programcomparisoncnetplot": "compare_programs_cnetplot",
            "programcomparisongeneconceptnetwork": "compare_programs_cnetplot",
            "programcomparisonemapplot": "compare_programs_emapplot",
            "programcomparisonenrichmentmap": "compare_programs_emapplot",
            "rnasequpenrichmap": "rnaseq_up_emapplot",
            "rnasequpenrichmentmap": "rnaseq_up_emapplot",
            "upregulatedenrichmentmap": "rnaseq_up_emapplot",
            "rnasequpcnetplot": "rnaseq_up_cnetplot",
            "upregulatedgeneconceptnetwork": "rnaseq_up_cnetplot",
            "upregulatedgeneconceptgraph": "rnaseq_up_cnetplot",
            "rnasequpppinetwork": "rnaseq_up_ppi_network",
            "upregulatedgenegenenetwork": "rnaseq_up_ppi_network",
            "rnaseqdownenrichmap": "rnaseq_down_emapplot",
            "rnaseqdownenrichmentmap": "rnaseq_down_emapplot",
            "downregulatedenrichmentmap": "rnaseq_down_emapplot",
            "rnaseqdowncnetplot": "rnaseq_down_cnetplot",
            "downregulatedgeneconceptnetwork": "rnaseq_down_cnetplot",
            "downregulatedgeneconceptgraph": "rnaseq_down_cnetplot",
            "rnaseqdownppinetwork": "rnaseq_down_ppi_network",
            "downregulatedgenegenenetwork": "rnaseq_down_ppi_network",
        }.get(normalized_requested)
        if alias_target and alias_target in supported:
            return alias_target
        return requested

    def figure_options(self, artifact: Artifact) -> list[dict[str, Any]]:
        payload = artifact.payload or {}
        options: list[dict[str, Any]] = []

        if artifact.kind in {"enrichment", "disease_enrichment"} and self._terms_from_enrichment_payload(payload):
            options.extend(
                [
                    self._option(artifact, "enrichment_barplot", "Enrichment Bar Plot", "Top enriched terms ranked by significance."),
                ]
            )
            if artifact.kind == "enrichment" and self._enrichment_plot_input(artifact):
                options.append(
                    self._option(
                        artifact,
                        "enrichment_emapplot",
                        "Enrichment Map",
                        "ClusterProfiler/enrichplot similarity network of enriched pathways or GO terms.",
                    )
                )
                options.append(
                    self._option(
                        artifact,
                        "enrichment_cnetplot",
                        "Gene-Concept Network",
                        "ClusterProfiler/enrichplot network linking enriched terms to contributing genes.",
                    )
                )
                options.append(
                    self._option(
                        artifact,
                        "enrichment_ppi_network",
                        "Gene-Gene PPI Network",
                        "clusterProfiler::getPPI network for genes contributing to the strongest enriched terms.",
                    )
                )
            if artifact.kind == "disease_enrichment" and self._enrichment_plot_input(artifact):
                options.append(
                    self._option(
                        artifact,
                        "disease_emapplot",
                        "Disease Enrichment Map",
                        "ClusterProfiler/enrichplot similarity network of enriched disease terms.",
                    )
                )
                options.append(
                    self._option(
                        artifact,
                        "disease_cnetplot",
                        "Disease Gene-Concept Network",
                        "ClusterProfiler/enrichplot network linking enriched disease terms to contributing genes.",
                    )
                )
                options.append(
                    self._option(
                        artifact,
                        "disease_ppi_network",
                        "Disease Gene-Gene PPI Network",
                        "clusterProfiler::getPPI network for genes driving the strongest enriched disease terms.",
                    )
                )

        if artifact.kind == "ranked_gsea" and self._terms_from_enrichment_payload(payload):
            options.extend(
                [
                    self._option(artifact, "enrichment_barplot", "Ranked GSEA Bar Plot", "Top ranked GSEA terms ranked by significance."),
                    self._option(
                        artifact,
                        "ranked_gsea_plot",
                        "Ranked GSEA Plot",
                        "ClusterProfiler/enrichplot running-score plot for the strongest ranked term.",
                    ),
                ]
            )

        if artifact.kind == "program_comparison" and self._terms_from_program_comparison_payload(payload):
            options.extend(
                [
                    self._option(
                        artifact,
                        "compare_programs_barplot",
                        "Program Comparison Bar Plot",
                        "Top enrichment terms across the compared gene programs.",
                    ),
                ]
            )
            if self._program_comparison_input(artifact):
                options.append(
                    self._option(
                        artifact,
                        "compare_programs_emapplot",
                        "Program Comparison Enrichment Map",
                        "ClusterProfiler/enrichplot compareCluster enrichment map across the compared gene programs.",
                    )
                )
                options.append(
                    self._option(
                        artifact,
                        "compare_programs_cnetplot",
                        "Program Comparison Gene-Concept Network",
                        "ClusterProfiler/enrichplot compareCluster gene-concept network across the compared gene programs.",
                    )
                )

        if artifact.kind == "gene_set_investigation" and self._terms_from_investigation_payload(payload):
            options.extend(
                [
                    self._option(artifact, "enrichment_barplot", "Enrichment Bar Plot", "Top enriched terms from the saved investigation."),
                ]
            )
            if self._enrichment_plot_input(artifact):
                options.append(
                    self._option(
                        artifact,
                        "enrichment_emapplot",
                        "Enrichment Map",
                        "ClusterProfiler/enrichplot similarity network for the saved investigation enrichment.",
                    )
                )
                options.append(
                    self._option(
                        artifact,
                        "enrichment_cnetplot",
                        "Gene-Concept Network",
                        "ClusterProfiler/enrichplot gene-concept network for the saved investigation enrichment.",
                    )
                )
                options.append(
                    self._option(
                        artifact,
                        "enrichment_ppi_network",
                        "Gene-Gene PPI Network",
                        "clusterProfiler::getPPI network for genes contributing to the strongest saved enrichment terms.",
                    )
                )

        if artifact.kind == "rnaseq_analysis":
            if self._terms_from_rnaseq_payload(payload, "upregulated"):
                options.append(
                    self._option(
                        artifact,
                        "rnaseq_up_barplot",
                        "RNA-seq Upregulated Bar Plot",
                        "Top enriched terms for the upregulated signature ranked by significance.",
                    )
                )

                if self._rnaseq_enrichment_plot_input(artifact, "upregulated"):
                    options.append(
                        self._option(
                            artifact,
                            "rnaseq_up_emapplot",
                            "RNA-seq Upregulated Enrichment Map",
                            "ClusterProfiler/enrichplot similarity network for the upregulated enrichment terms.",
                        )
                    )
                    options.append(
                        self._option(
                            artifact,
                            "rnaseq_up_cnetplot",
                            "RNA-seq Upregulated Gene-Concept Network",
                            "ClusterProfiler/enrichplot gene-concept network for the upregulated enrichment terms.",
                        )
                    )
                    options.append(
                        self._option(
                            artifact,
                            "rnaseq_up_ppi_network",
                            "RNA-seq Upregulated Gene-Gene PPI Network",
                            "clusterProfiler::getPPI network for genes driving the upregulated enrichment terms.",
                        )
                    )
            if self._terms_from_rnaseq_payload(payload, "downregulated"):
                options.append(
                    self._option(
                        artifact,
                        "rnaseq_down_barplot",
                        "RNA-seq Downregulated Bar Plot",
                        "Top enriched terms for the downregulated signature ranked by significance.",
                    )
                )
                if self._rnaseq_enrichment_plot_input(artifact, "downregulated"):
                    options.append(
                        self._option(
                            artifact,
                            "rnaseq_down_emapplot",
                            "RNA-seq Downregulated Enrichment Map",
                            "ClusterProfiler/enrichplot similarity network for the downregulated enrichment terms.",
                        )
                    )
                    options.append(
                        self._option(
                            artifact,
                            "rnaseq_down_cnetplot",
                            "RNA-seq Downregulated Gene-Concept Network",
                            "ClusterProfiler/enrichplot gene-concept network for the downregulated enrichment terms.",
                        )
                    )
                    options.append(
                        self._option(
                            artifact,
                            "rnaseq_down_ppi_network",
                            "RNA-seq Downregulated Gene-Gene PPI Network",
                            "clusterProfiler::getPPI network for genes driving the downregulated enrichment terms.",
                        )
                    )
            if clean_text(((payload.get("input") or {}).get("result_path"))):
                options.append(
                    self._option(
                        artifact,
                        "rnaseq_volcano",
                        "RNA-seq Volcano Plot",
                        "Differential-expression volcano plot from the saved DE table path.",
                    )
                )
            if self._ranked_gsea_input(artifact):
                options.append(
                    self._option(
                        artifact,
                        "ranked_gsea_plot",
                        "RNA-seq Ranked GSEA Plot",
                        "ClusterProfiler/enrichplot running-score plot reconstructed from the full saved ranked signature.",
                    )
                )

        if artifact.kind == "gene_level_integration" and self._cross_modal_points(payload):
            options.append(
                self._option(
                    artifact,
                    "cross_modal_concordance_scatter",
                    "Cross-Modal Concordance Scatter",
                    "Effect sizes for shared genes across two direction-comparable modalities; colors show factual concordance states.",
                )
            )

        if artifact.kind == "expression_context" and self._expression_rows(payload):
            options.append(
                self._option(
                    artifact,
                    "expression_context_barplot",
                    "Expression Context Plot",
                    "Reference tissue-expression bar plot from GTEx or Expression Atlas.",
                )
            )

        return options

    def create_figure(
        self,
        artifact: Artifact,
        *,
        output_dir: Path,
        figure_type: str,
        top_n: int = 10,
        title: str = "",
    ) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        figure_id = new_id("figure")
        stem = output_dir / figure_id
        resolved_figure_type = self._resolve_requested_figure_type(artifact, figure_type)
        custom_title = clean_text(title)
        effective_title = custom_title or self._default_title(artifact, resolved_figure_type)
        actual_figure_type = resolved_figure_type
        extra_payload: dict[str, Any] = {}

        if resolved_figure_type == "enrichment_barplot":
            terms = self._extract_terms(artifact)
            if not terms:
                raise ProviderError("This artifact does not contain enrichment terms for a bar plot.")
            caption = self._plot_enrichment_barplot(terms, stem=stem, title=effective_title, top_n=top_n)
        elif resolved_figure_type == "enrichment_emapplot":
            figure_payload = self._plot_enrichment_r_figure(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                plot_type="emapplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
            if figure_payload.get("render_mode") == "cnetplot":
                actual_figure_type = "enrichment_cnetplot"
        elif resolved_figure_type == "disease_emapplot":
            figure_payload = self._plot_enrichment_r_figure(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                plot_type="emapplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
            if figure_payload.get("render_mode") == "cnetplot":
                actual_figure_type = "disease_cnetplot"
        elif resolved_figure_type == "enrichment_cnetplot":
            figure_payload = self._plot_enrichment_r_figure(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                plot_type="cnetplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "disease_cnetplot":
            figure_payload = self._plot_enrichment_r_figure(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                plot_type="cnetplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "enrichment_ppi_network":
            figure_payload = self._plot_ppi_network(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "disease_ppi_network":
            figure_payload = self._plot_ppi_network(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "compare_programs_barplot":
            rows = self._terms_from_program_comparison_payload(artifact.payload or {})
            if not rows:
                raise ProviderError("This artifact does not contain comparison terms for a bar plot.")
            caption = self._plot_compare_programs_barplot(rows, stem=stem, title=effective_title, top_n=top_n)
        elif resolved_figure_type == "compare_programs_emapplot":
            caption = self._plot_compare_programs_graph(artifact, stem=stem, title=effective_title, top_n=top_n, plot_type="emapplot")
        elif resolved_figure_type == "compare_programs_cnetplot":
            caption = self._plot_compare_programs_graph(artifact, stem=stem, title=effective_title, top_n=top_n, plot_type="cnetplot")
        elif resolved_figure_type == "rnaseq_up_barplot":
            terms = self._terms_from_rnaseq_payload(artifact.payload or {}, "upregulated")
            if not terms:
                raise ProviderError("This RNA-seq artifact does not contain saved upregulated enrichment terms for a bar plot.")
            caption = self._plot_enrichment_barplot(terms, stem=stem, title=effective_title, top_n=top_n)
        elif resolved_figure_type == "rnaseq_up_emapplot":
            figure_payload = self._plot_rnaseq_enrichment_graph(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                direction="upregulated",
                plot_type="emapplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
            if figure_payload.get("render_mode") == "cnetplot":
                actual_figure_type = "rnaseq_up_cnetplot"
        elif resolved_figure_type == "rnaseq_up_cnetplot":
            figure_payload = self._plot_rnaseq_enrichment_graph(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                direction="upregulated",
                plot_type="cnetplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "rnaseq_up_ppi_network":
            figure_payload = self._plot_rnaseq_ppi_network(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                direction="upregulated",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "rnaseq_down_barplot":
            terms = self._terms_from_rnaseq_payload(artifact.payload or {}, "downregulated")
            if not terms:
                raise ProviderError("This RNA-seq artifact does not contain saved downregulated enrichment terms for a bar plot.")
            caption = self._plot_enrichment_barplot(terms, stem=stem, title=effective_title, top_n=top_n)
        elif resolved_figure_type == "rnaseq_down_emapplot":
            figure_payload = self._plot_rnaseq_enrichment_graph(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                direction="downregulated",
                plot_type="emapplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
            if figure_payload.get("render_mode") == "cnetplot":
                actual_figure_type = "rnaseq_down_cnetplot"
        elif resolved_figure_type == "rnaseq_down_cnetplot":
            figure_payload = self._plot_rnaseq_enrichment_graph(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                direction="downregulated",
                plot_type="cnetplot",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "rnaseq_down_ppi_network":
            figure_payload = self._plot_rnaseq_ppi_network(
                artifact,
                stem=stem,
                title=effective_title,
                top_n=top_n,
                direction="downregulated",
            )
            caption = figure_payload["caption"]
            extra_payload = figure_payload
        elif resolved_figure_type == "rnaseq_volcano":
            caption = self._plot_rnaseq_volcano(artifact, stem=stem, title=effective_title)
        elif resolved_figure_type == "cross_modal_concordance_scatter":
            caption = self._plot_cross_modal_concordance(artifact, stem=stem, title=effective_title)
        elif resolved_figure_type == "ranked_gsea_plot":
            caption = self._plot_ranked_gsea_figure(artifact, stem=stem, title=effective_title, top_n=top_n)
        elif resolved_figure_type == "expression_context_barplot":
            rows = self._expression_rows(artifact.payload or {})
            if not rows:
                raise ProviderError("This artifact does not contain saved expression rows to plot.")
            caption = self._plot_expression_context(rows, artifact=artifact, stem=stem, title=effective_title, top_n=top_n)
        else:
            supported = [str(row.get("figure_type") or "") for row in self.figure_options(artifact) if clean_text(row.get("figure_type"))]
            detail = f" Unsupported figure_type '{figure_type}' for artifact kind '{artifact.kind}'."
            if supported:
                detail += f" Supported figure types: {', '.join(supported)}."
            if artifact.kind == "rnaseq_analysis":
                detail += " For RNA-seq pathway relationship graphs, use 'rnaseq_up_emapplot' or 'rnaseq_down_emapplot'. For GLESKit-native gene-term networks, use 'rnaseq_up_cnetplot' or 'rnaseq_down_cnetplot'. For gene-gene networks, use 'rnaseq_up_ppi_network' or 'rnaseq_down_ppi_network'. For a running-score plot from the saved full ranked signature, use 'ranked_gsea_plot'."
            elif artifact.kind in {"enrichment", "gene_set_investigation"}:
                detail += " For pathway relationship graphs, use 'enrichment_emapplot' or 'enrichment_cnetplot'. For gene-gene networks, use 'enrichment_ppi_network'."
            elif artifact.kind == "disease_enrichment":
                detail += " For disease-term graphs, use 'disease_emapplot' or 'disease_cnetplot'. For disease gene-gene networks, use 'disease_ppi_network'."
            elif artifact.kind == "program_comparison":
                detail += " For pathway relationship graphs across programs, use 'compare_programs_emapplot' or 'compare_programs_cnetplot'."
            raise ProviderError(detail.strip())

        if actual_figure_type != resolved_figure_type and not custom_title:
            effective_title = self._default_title(artifact, actual_figure_type)

        file_payload = self._figure_file_payload(stem)
        png_path = Path(str(file_payload["png_path"]))
        figure_payload = {
            "figure_type": actual_figure_type,
            "title": effective_title,
            "caption": caption,
            **file_payload,
            "image_markdown": f"![{effective_title}]({png_path})",
            "report_markdown": f"![{effective_title}]({png_path})",
            "inline_mime_type": "image/png",
            "source_artifact_id": artifact.artifact_id,
            "source_artifact_kind": artifact.kind,
        }
        if actual_figure_type != resolved_figure_type:
            figure_payload["requested_figure_type"] = resolved_figure_type
        for key, value in extra_payload.items():
            if key != "caption" and value not in (None, ""):
                figure_payload[key] = value
        return figure_payload

    def _extract_terms(self, artifact: Artifact) -> list[dict[str, Any]]:
        if artifact.kind in {"enrichment", "disease_enrichment", "ranked_gsea"}:
            return self._terms_from_enrichment_payload(artifact.payload or {})
        if artifact.kind == "gene_set_investigation":
            return self._terms_from_investigation_payload(artifact.payload or {})
        return []

    @staticmethod
    def _option(artifact: Artifact, figure_type: str, label: str, description: str) -> dict[str, Any]:
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_kind": artifact.kind,
            "artifact_title": artifact.title,
            "figure_type": figure_type,
            "label": label,
            "description": description,
        }

    @staticmethod
    def _default_title(artifact: Artifact, figure_type: str) -> str:
        mapping = {
            "enrichment_barplot": "Enrichment Bar Plot",
            "enrichment_emapplot": "Enrichment Map",
            "enrichment_cnetplot": "Gene-Concept Network",
            "enrichment_ppi_network": "Gene-Gene PPI Network",
            "disease_emapplot": "Disease Enrichment Map",
            "disease_cnetplot": "Disease Gene-Concept Network",
            "disease_ppi_network": "Disease Gene-Gene PPI Network",
            "compare_programs_barplot": "Gene Program Comparison",
            "compare_programs_emapplot": "Program Comparison Enrichment Map",
            "compare_programs_cnetplot": "Program Comparison Gene-Concept Network",
            "rnaseq_up_barplot": "RNA-seq Upregulated Enrichment",
            "rnaseq_up_emapplot": "RNA-seq Upregulated Enrichment Map",
            "rnaseq_up_cnetplot": "RNA-seq Upregulated Gene-Concept Network",
            "rnaseq_up_ppi_network": "RNA-seq Upregulated Gene-Gene PPI Network",
            "rnaseq_down_barplot": "RNA-seq Downregulated Enrichment",
            "rnaseq_down_emapplot": "RNA-seq Downregulated Enrichment Map",
            "rnaseq_down_cnetplot": "RNA-seq Downregulated Gene-Concept Network",
            "rnaseq_down_ppi_network": "RNA-seq Downregulated Gene-Gene PPI Network",
            "rnaseq_volcano": "RNA-seq Volcano Plot",
            "ranked_gsea_plot": "Ranked GSEA Plot",
            "expression_context_barplot": "Expression Context Plot",
            "cross_modal_concordance_scatter": "Cross-Modal Concordance Scatter",
        }
        return f"{artifact.title} - {mapping.get(figure_type, figure_type)}"

    @staticmethod
    def _terms_from_enrichment_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(row) for row in (payload.get("terms") or []) if isinstance(row, dict)]

    @staticmethod
    def _terms_from_investigation_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
        enrichment = payload.get("enrichment") or {}
        return [dict(row) for row in (enrichment.get("terms") or []) if isinstance(row, dict)]

    @staticmethod
    def _terms_from_program_comparison_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(row) for row in (payload.get("comparison_terms") or []) if isinstance(row, dict)]

    @staticmethod
    def _terms_from_rnaseq_payload(payload: dict[str, Any], direction: str) -> list[dict[str, Any]]:
        enrichment = (payload.get("enrichment") or {}).get(direction) or {}
        return [dict(row) for row in (enrichment.get("terms") or []) if isinstance(row, dict)]

    @staticmethod
    def _cross_modal_points(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not payload.get("direction_comparable"):
            return []
        inputs = payload.get("inputs") or []
        if len(inputs) < 2:
            return []
        labels = [clean_text(item.get("label")) for item in inputs[:2] if isinstance(item, dict)]
        if len(labels) != 2:
            return []
        points = []
        for row in payload.get("gene_evidence_matrix") or []:
            modalities = row.get("modalities") or {}
            left = (modalities.get(labels[0]) or {}).get("observations") or []
            right = (modalities.get(labels[1]) or {}).get("observations") or []
            if len(left) == 1 and len(right) == 1:
                points.append({"x": left[0].get("log2_fold_change"), "y": right[0].get("log2_fold_change"), "state": row.get("integration_state")})
        return [point for point in points if point.get("x") is not None and point.get("y") is not None]

    @staticmethod
    def _expression_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        gtex_rows = [dict(row) for row in ((payload.get("gtex") or {}).get("tissues") or []) if isinstance(row, dict)]
        if gtex_rows:
            for row in gtex_rows:
                row["_source"] = "GTEx"
                row["_label"] = clean_text(row.get("tissue_site_detail_id"))
                row["_value"] = float(row.get("median") or 0.0)
            return gtex_rows
        atlas_rows = [
            dict(row)
            for row in ((((payload.get("expression_atlas") or {}).get("query_gene_profile") or {}).get("tissues")) or [])
            if isinstance(row, dict)
        ]
        for row in atlas_rows:
            row["_source"] = "Expression Atlas"
            row["_label"] = clean_text(row.get("tissue"))
            row["_value"] = float(row.get("value") or 0.0)
        return atlas_rows

    def _enrichment_plot_input(self, artifact: Artifact) -> dict[str, Any] | None:
        payload = artifact.payload or {}
        if artifact.kind == "enrichment":
            genes = normalize_gene_list(payload.get("genes") or [])
            saved_terms = self._enrichplot_saved_terms(payload.get("terms") or [])
            sources = self._normalize_source_values(
                self._sources_from_terms(saved_terms) if saved_terms else (payload.get("sources") or self._sources_from_terms(payload.get("terms")))
            )
            if len(genes) >= 2 and (sources or saved_terms):
                return {"genes": genes, "sources": sources, "saved_terms": saved_terms}
            return None

        if artifact.kind == "disease_enrichment":
            genes = normalize_gene_list(payload.get("input_genes") or [])
            saved_terms = self._enrichplot_saved_terms(payload.get("terms") or [])
            if len(genes) < 2:
                genes = normalize_gene_list(
                    gene
                    for row in saved_terms
                    for gene in (row.get("matched_genes") or [])
                )
            sources = self._normalize_source_values(
                self._sources_from_terms(saved_terms) if saved_terms else (payload.get("sources") or payload.get("database"))
            )
            if len(genes) >= 2 and (sources or saved_terms):
                return {"genes": genes, "sources": sources, "saved_terms": saved_terms}
            return None

        if artifact.kind == "gene_set_investigation":
            enrichment = payload.get("enrichment") or {}
            genes = normalize_gene_list(enrichment.get("genes") or payload.get("resolved_genes") or payload.get("input_genes") or [])
            saved_terms = self._enrichplot_saved_terms(enrichment.get("terms") or [])
            sources = self._normalize_source_values(
                self._sources_from_terms(saved_terms) if saved_terms else (enrichment.get("sources") or payload.get("sources") or self._sources_from_terms(enrichment.get("terms")))
            )
            if len(genes) >= 2 and (sources or saved_terms):
                return {"genes": genes, "sources": sources, "saved_terms": saved_terms}
        return None

    def _program_comparison_input(self, artifact: Artifact) -> dict[str, Any] | None:
        payload = artifact.payload or {}
        raw_programs = payload.get("program_inputs") or payload.get("programs") or {}
        if not isinstance(raw_programs, dict):
            return None
        programs: dict[str, list[str]] = {}
        for label, genes in raw_programs.items():
            cleaned_label = clean_text(label)
            cleaned_genes = normalize_gene_list(genes or [])
            if cleaned_label and cleaned_genes:
                programs[cleaned_label] = cleaned_genes
        if len(programs) < 2:
            return None
        database = clean_text(payload.get("database")).upper() or "GO_BP"
        saved_terms = self._comparison_saved_terms(payload.get("comparison_terms") or [])
        return {"programs": programs, "database": database, "saved_terms": saved_terms}

    def _ranked_gsea_input(self, artifact: Artifact) -> dict[str, Any] | None:
        payload = artifact.payload or {}
        scores = [dict(row) for row in (payload.get("ranked_gene_scores") or []) if isinstance(row, dict)]
        ranked_payload = payload.get("ranked_gsea") or {}
        if not scores and isinstance(ranked_payload, dict):
            scores = [dict(row) for row in (ranked_payload.get("ranked_gene_scores") or []) if isinstance(row, dict)]
        if len(scores) < 4:
            return None
        sources = self._normalize_pathway_sources(
            payload.get("sources")
            or (ranked_payload.get("sources") if isinstance(ranked_payload, dict) else [])
            or self._sources_from_terms(payload.get("terms"))
            or (
                self._sources_from_terms(ranked_payload.get("terms"))
                if isinstance(ranked_payload, dict)
                else []
            )
            or ["GO:BP", "REAC"]
        )
        return {"gene_scores": scores, "sources": sources}

    def _rnaseq_enrichment_plot_input(self, artifact: Artifact, direction: str) -> dict[str, Any] | None:
        payload = artifact.payload or {}
        significant_gene_sets = payload.get("significant_gene_sets") or {}
        enrichment = (payload.get("enrichment") or {}).get(direction) or {}
        genes = normalize_gene_list(significant_gene_sets.get(direction) or [])
        saved_terms = self._enrichplot_saved_terms(enrichment.get("terms") or [])
        sources = self._normalize_source_values(
            self._sources_from_terms(saved_terms) if saved_terms else (enrichment.get("sources") or self._sources_from_terms(enrichment.get("terms")))
        )
        if len(genes) < 2:
            return None
        return {"genes": genes, "sources": sources, "saved_terms": saved_terms}

    @staticmethod
    def _enrichplot_saved_terms(terms: Any) -> list[dict[str, Any]]:
        saved_terms: list[dict[str, Any]] = []
        for row in terms or []:
            if not isinstance(row, dict):
                continue
            matched_genes = normalize_gene_list(row.get("matched_genes") or [])
            if not matched_genes:
                continue
            term_id = clean_text(row.get("term_id"))
            term_name = clean_text(row.get("term_name"))
            source = clean_text(row.get("source")).upper()
            if not term_id or not term_name or not source:
                continue
            saved_terms.append(
                {
                    "term_id": term_id,
                    "term_name": term_name,
                    "source": source,
                    "p_value": row.get("p_value"),
                    "adjusted_p_value": row.get("adjusted_p_value"),
                    "q_value": row.get("q_value"),
                    "gene_ratio": row.get("gene_ratio"),
                    "background_ratio": row.get("background_ratio"),
                    "intersection_size": row.get("intersection_size"),
                    "matched_genes": matched_genes,
                }
            )
        return saved_terms

    @staticmethod
    def _comparison_saved_terms(terms: Any) -> list[dict[str, Any]]:
        saved_terms: list[dict[str, Any]] = []
        for row in terms or []:
            if not isinstance(row, dict):
                continue
            program_label = clean_text(row.get("program_label"))
            matched_genes = normalize_gene_list(row.get("matched_genes") or [])
            term_id = clean_text(row.get("term_id"))
            term_name = clean_text(row.get("term_name"))
            source = clean_text(row.get("source")).upper()
            if not program_label or not matched_genes or not term_id or not term_name or not source:
                continue
            saved_terms.append(
                {
                    "program_label": program_label,
                    "term_id": term_id,
                    "term_name": term_name,
                    "source": source,
                    "p_value": row.get("p_value"),
                    "adjusted_p_value": row.get("adjusted_p_value"),
                    "q_value": row.get("q_value"),
                    "gene_ratio": row.get("gene_ratio"),
                    "background_ratio": row.get("background_ratio"),
                    "intersection_size": row.get("intersection_size"),
                    "matched_genes": matched_genes,
                }
            )
        return saved_terms

    @staticmethod
    def _sources_from_terms(terms: Any) -> list[str]:
        sources: list[str] = []
        seen: set[str] = set()
        for row in terms or []:
            if not isinstance(row, dict):
                continue
            token = clean_text(row.get("source")).upper()
            if token and token not in seen:
                seen.add(token)
                sources.append(token)
        return sources

    @staticmethod
    def _normalize_pathway_sources(sources: Any) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in sources or []:
            token = clean_text(raw).upper()
            if token == "GO_BP":
                token = "GO:BP"
            if token in {"GO:BP", "REAC"} and token not in seen:
                seen.add(token)
                normalized.append(token)
        return normalized or ["REAC", "GO:BP"]

    @staticmethod
    def _normalize_source_values(sources: Any) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        raw_items = sources if isinstance(sources, (list, tuple, set)) else [sources]
        for raw in raw_items:
            token = clean_text(raw).upper()
            if token == "GO_BP":
                token = "GO:BP"
            if token and token not in seen:
                seen.add(token)
                normalized.append(token)
        return normalized

    def _plot_enrichment_barplot(self, terms: list[dict[str, Any]], *, stem: Path, title: str, top_n: int) -> str:
        ranked = self._rank_terms(terms, top_n=top_n)
        labels = [self._term_label(term) for term in ranked]
        x_values = [self._neg_log10(term.get("p_value")) for term in ranked]
        y_positions = list(range(len(ranked)))

        fig, ax = plt.subplots(figsize=(9, max(3.8, len(ranked) * 0.55)))
        ax.barh(y_positions, x_values, color="#7cc7ff", edgecolor="#11161d")
        ax.set_yticks(y_positions)
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel("-log10(p-value)")
        ax.set_title(title)
        ax.grid(axis="x", alpha=0.18, linestyle="--")
        ax.set_axisbelow(True)
        ax.invert_yaxis()
        fig.tight_layout()
        self._save_figure(fig, stem)
        return f"Bar plot of the top {len(ranked)} enriched terms ranked by significance."

    def _plot_enrichment_r_figure(
        self,
        artifact: Artifact,
        *,
        stem: Path,
        title: str,
        top_n: int,
        plot_type: str,
    ) -> dict[str, Any]:
        if self.r_runner is None:
            raise ProviderError("R-backed enrichment plots require a configured GLESKit settings context.")

        plot_input = self._enrichment_plot_input(artifact)
        if not plot_input:
            raise ProviderError("This artifact does not contain the saved pathway-enrichment inputs needed for an enrichplot figure.")

        png_path = stem.with_suffix(".png")
        svg_path = stem.with_suffix(".svg")
        payload = self.r_runner.run(
            "gleskit_enrichment_figure.R",
            {
                "genes": plot_input["genes"],
                "sources": plot_input["sources"],
                "saved_terms": plot_input.get("saved_terms") or [],
                "fold_changes": self._fold_change_rows_for_artifact(artifact, plot_input),
                "title": title,
                "top_n": max(3, min(int(top_n), 20)),
                "prefer_source": self._preferred_source(plot_input.get("sources") or []),
                "plot_type": plot_type,
                "output_png_path": str(png_path),
                "output_svg_path": str(svg_path),
            },
            capability=f"{plot_type} enrichment figure",
            timeout_seconds=self._r_figure_timeout_seconds(),
        )
        self._figure_file_payload(stem)
        return {
            "caption": clean_text(payload.get("caption")) or "ClusterProfiler/enrichplot enrichment figure.",
            "render_mode": clean_text(payload.get("render_mode")) or plot_type,
            "rendered_source": clean_text(payload.get("rendered_source")),
            "rendered_term_count": payload.get("rendered_term_count"),
            "rendered_top_n": payload.get("rendered_top_n"),
            "fallback_reason": clean_text(payload.get("fallback_reason")),
        }

    def _plot_compare_programs_barplot(
        self,
        rows: list[dict[str, Any]],
        *,
        stem: Path,
        title: str,
        top_n: int,
    ) -> str:
        ranked = self._rank_terms(rows, top_n=top_n)
        if not ranked:
            raise ProviderError("No comparison terms were available to plot.")

        program_labels = [clean_text(row.get("program_label")) or "Program" for row in ranked]
        unique_programs = []
        for label in program_labels:
            if label not in unique_programs:
                unique_programs.append(label)
        palette = ["#7cc7ff", "#8ad38a", "#ffbf69", "#f28fb3", "#a78bfa", "#62d2a2"]
        color_map = {label: palette[index % len(palette)] for index, label in enumerate(unique_programs)}

        labels = [
            f"{clean_text(row.get('program_label')) or 'Program'}: {self._term_label(row)}"
            for row in ranked
        ]
        x_values = [self._neg_log10(row.get("p_value")) for row in ranked]
        colors = [color_map[clean_text(row.get("program_label")) or "Program"] for row in ranked]
        y_positions = list(range(len(ranked)))

        fig, ax = plt.subplots(figsize=(10, max(4.2, len(ranked) * 0.58)))
        ax.barh(y_positions, x_values, color=colors, edgecolor="#11161d")
        ax.set_yticks(y_positions)
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel("-log10(p-value)")
        ax.set_title(title)
        ax.grid(axis="x", alpha=0.18, linestyle="--")
        ax.set_axisbelow(True)
        ax.invert_yaxis()

        handles = [
            plt.Rectangle((0, 0), 1, 1, color=color_map[label], ec="#11161d")
            for label in unique_programs
        ]
        ax.legend(handles, unique_programs, title="Program", loc="lower right", fontsize=8, title_fontsize=9)

        fig.tight_layout()
        self._save_figure(fig, stem)
        return f"Bar plot comparing the top {len(ranked)} enriched terms across the submitted gene programs."

    def _plot_compare_programs_graph(
        self,
        artifact: Artifact,
        *,
        stem: Path,
        title: str,
        top_n: int,
        plot_type: str,
    ) -> str:
        if self.r_runner is None:
            raise ProviderError("R-backed compareCluster plots require a configured GLESKit settings context.")

        plot_input = self._program_comparison_input(artifact)
        if not plot_input:
            raise ProviderError("This artifact does not contain the saved program inputs needed for a compareCluster graph.")

        png_path = stem.with_suffix(".png")
        svg_path = stem.with_suffix(".svg")
        payload = self.r_runner.run(
            "gleskit_compare_programs_figure.R",
            {
                "programs": plot_input["programs"],
                "database": plot_input["database"],
                "saved_terms": plot_input.get("saved_terms") or [],
                "title": title,
                "top_n": max(3, min(int(top_n), 20)),
                "plot_type": plot_type,
                "output_png_path": str(png_path),
                "output_svg_path": str(svg_path),
            },
            capability=f"compare-programs {plot_type}",
            timeout_seconds=self._r_figure_timeout_seconds(),
        )
        self._figure_file_payload(stem)
        default_caption = (
            "ClusterProfiler/enrichplot compareCluster enrichment map."
            if plot_type == "emapplot"
            else "ClusterProfiler/enrichplot compareCluster gene-concept network."
        )
        return clean_text(payload.get("caption")) or default_caption

    @staticmethod
    def _preferred_source(sources: list[str]) -> str:
        normalized = [clean_text(source).upper() for source in sources if clean_text(source)]
        if "REAC" in normalized:
            return "REAC"
        if normalized:
            return normalized[0]
        return "GO:BP"

    def _fold_change_rows_for_artifact(self, artifact: Artifact, plot_input: dict[str, Any]) -> list[dict[str, Any]]:
        if artifact.kind != "rnaseq_analysis":
            return []
        genes = normalize_gene_list(
            list(plot_input.get("genes") or [])
            + [
                gene
                for row in (plot_input.get("saved_terms") or [])
                for gene in (row.get("matched_genes") or [])
            ]
        )
        return self._rnaseq_fold_change_rows(artifact, genes)

    def _rnaseq_fold_change_rows(self, artifact: Artifact, genes: list[str]) -> list[dict[str, Any]]:
        payload = artifact.payload or {}
        input_payload = payload.get("input") or {}
        result_path = clean_text(input_payload.get("result_path"))
        if not result_path:
            return []
        path = Path(result_path).expanduser()
        if not path.exists():
            return []

        try:
            table_text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        delimiter = _infer_delimiter(table_text)
        rows = list(csv.DictReader(StringIO(table_text), delimiter=delimiter))
        if not rows:
            return []

        header = [str(key) for key in rows[0].keys()]
        table_meta = payload.get("table") or {}
        gene_column = _resolve_column(
            header,
            str((table_meta.get("column_map") or {}).get("gene") or ""),
            _COLUMN_ALIASES["gene"],
            required=True,
        )
        lfc_column = _resolve_column(
            header,
            str((table_meta.get("column_map") or {}).get("log2_fold_change") or ""),
            _COLUMN_ALIASES["log2_fold_change"],
            required=True,
        )
        target_genes = {clean_text(gene).upper() for gene in genes if clean_text(gene)}
        if not target_genes:
            return []

        best_scores: dict[str, float] = {}
        for row in rows:
            gene = clean_text(row.get(gene_column)).upper()
            if not gene or gene not in target_genes:
                continue
            log2fc = _float_or_none(row.get(lfc_column))
            if log2fc is None:
                continue
            current = best_scores.get(gene)
            if current is None or abs(float(log2fc)) > abs(current):
                best_scores[gene] = float(log2fc)
        return [{"gene": gene, "score": score} for gene, score in best_scores.items()]

    def _plot_ppi_network(
        self,
        artifact: Artifact,
        *,
        stem: Path,
        title: str,
        top_n: int,
    ) -> dict[str, Any]:
        if self.r_runner is None:
            raise ProviderError("R-backed PPI plots require a configured GLESKit settings context.")
        plot_input = self._enrichment_plot_input(artifact)
        if not plot_input:
            raise ProviderError("This artifact does not contain the saved term-gene inputs needed for a PPI network.")

        png_path = stem.with_suffix(".png")
        svg_path = stem.with_suffix(".svg")
        payload = self.r_runner.run(
            "gleskit_ppi_figure.R",
            {
                "genes": plot_input["genes"],
                "saved_terms": plot_input.get("saved_terms") or [],
                "fold_changes": self._fold_change_rows_for_artifact(artifact, plot_input),
                "title": title,
                "top_n": max(2, min(int(top_n), 20)),
                "prefer_source": self._preferred_source(plot_input.get("sources") or []),
                "add_nodes": 0,
                "output_png_path": str(png_path),
                "output_svg_path": str(svg_path),
            },
            capability="PPI network",
            timeout_seconds=self._r_figure_timeout_seconds(),
        )
        self._figure_file_payload(stem)
        return {
            "caption": clean_text(payload.get("caption")) or "clusterProfiler::getPPI gene-gene network.",
            "render_mode": clean_text(payload.get("render_mode")) or "ppi_network",
            "rendered_gene_count": payload.get("rendered_gene_count"),
            "selected_term_count": payload.get("selected_term_count"),
            "rendered_source": clean_text(payload.get("rendered_source")),
        }

    def _plot_rnaseq_ppi_network(
        self,
        artifact: Artifact,
        *,
        stem: Path,
        title: str,
        top_n: int,
        direction: str,
    ) -> dict[str, Any]:
        if self.r_runner is None:
            raise ProviderError("R-backed RNA-seq PPI plots require a configured GLESKit settings context.")
        plot_input = self._rnaseq_enrichment_plot_input(artifact, direction)
        if not plot_input:
            raise ProviderError(f"This RNA-seq artifact does not contain the saved {direction} term-gene inputs needed for a PPI network.")

        png_path = stem.with_suffix(".png")
        svg_path = stem.with_suffix(".svg")
        payload = self.r_runner.run(
            "gleskit_ppi_figure.R",
            {
                "genes": plot_input["genes"],
                "saved_terms": plot_input.get("saved_terms") or [],
                "fold_changes": self._rnaseq_fold_change_rows(artifact, plot_input["genes"]),
                "title": title,
                "top_n": max(2, min(int(top_n), 20)),
                "prefer_source": self._preferred_source(plot_input.get("sources") or []),
                "add_nodes": 0,
                "output_png_path": str(png_path),
                "output_svg_path": str(svg_path),
            },
            capability=f"{direction} RNA-seq PPI network",
            timeout_seconds=self._r_figure_timeout_seconds(),
        )
        self._figure_file_payload(stem)
        return {
            "caption": clean_text(payload.get("caption")) or "clusterProfiler::getPPI gene-gene network.",
            "render_mode": clean_text(payload.get("render_mode")) or "ppi_network",
            "rendered_gene_count": payload.get("rendered_gene_count"),
            "selected_term_count": payload.get("selected_term_count"),
            "rendered_source": clean_text(payload.get("rendered_source")),
        }

    def _plot_expression_context(
        self,
        rows: list[dict[str, Any]],
        *,
        artifact: Artifact,
        stem: Path,
        title: str,
        top_n: int,
    ) -> str:
        ranked = sorted(rows, key=lambda row: float(row.get("_value") or 0.0), reverse=True)[: max(1, min(int(top_n), 20))]
        labels = [clean_text(row.get("_label")) or "n/a" for row in ranked]
        values = [float(row.get("_value") or 0.0) for row in ranked]
        source = clean_text(ranked[0].get("_source")) or "Reference"
        unit = clean_text(ranked[0].get("unit")) or clean_text((artifact.payload.get("expression_atlas") or {}).get("query_gene_profile", {}).get("expression_unit")) or "expression"

        fig, ax = plt.subplots(figsize=(9, max(3.8, len(ranked) * 0.5)))
        ax.barh(list(range(len(ranked))), values, color="#3fb950", edgecolor="#11161d")
        ax.set_yticks(list(range(len(ranked))))
        ax.set_yticklabels(labels, fontsize=9)
        ax.set_xlabel(unit.upper() if len(unit) <= 6 else unit)
        ax.set_title(title)
        ax.grid(axis="x", alpha=0.18, linestyle="--")
        ax.set_axisbelow(True)
        ax.invert_yaxis()
        fig.tight_layout()
        self._save_figure(fig, stem)
        return f"{source} expression context across the top {len(ranked)} tissues by abundance."

    def _plot_rnaseq_volcano(self, artifact: Artifact, *, stem: Path, title: str) -> str:
        payload = artifact.payload or {}
        input_payload = payload.get("input") or {}
        result_path = clean_text(input_payload.get("result_path"))
        if not result_path:
            raise ProviderError("RNA-seq volcano plots currently require a saved result_path in the artifact input.")
        path = Path(result_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"RNA-seq result file no longer exists: {path}")
        table_text = path.read_text(encoding="utf-8")
        delimiter = _infer_delimiter(table_text)
        rows = list(csv.DictReader(StringIO(table_text), delimiter=delimiter))
        if not rows:
            raise ProviderError("The RNA-seq result file is empty.")

        table_meta = payload.get("table") or {}
        summary_meta = payload.get("summary_stats") or {}
        header = [str(key) for key in rows[0].keys()]
        column_map = {
            "gene": _resolve_column(header, str((table_meta.get("column_map") or {}).get("gene") or ""), _COLUMN_ALIASES["gene"], required=True),
            "log2_fold_change": _resolve_column(
                header,
                str((table_meta.get("column_map") or {}).get("log2_fold_change") or ""),
                _COLUMN_ALIASES["log2_fold_change"],
                required=True,
            ),
            "padj": _resolve_column(header, str((table_meta.get("column_map") or {}).get("padj") or ""), _COLUMN_ALIASES["padj"], required=False),
            "p_value": _resolve_column(header, str((table_meta.get("column_map") or {}).get("p_value") or ""), _COLUMN_ALIASES["p_value"], required=False),
        }

        parsed_rows: list[dict[str, Any]] = []
        for row in rows:
            gene = clean_text(row.get(column_map["gene"])) if column_map["gene"] else ""
            log2fc = _float_or_none(row.get(column_map["log2_fold_change"])) if column_map["log2_fold_change"] else None
            if not gene or log2fc is None:
                continue
            parsed_rows.append(
                {
                    "gene": gene,
                    "log2_fold_change": float(log2fc),
                    "padj": _float_or_none(row.get(column_map["padj"])) if column_map["padj"] else None,
                    "p_value": _float_or_none(row.get(column_map["p_value"])) if column_map["p_value"] else None,
                    "base_mean": _float_or_none(row.get(column_map.get("base_mean", ""))) if column_map.get("base_mean") else None,
                }
            )
        if not parsed_rows:
            raise ProviderError("The RNA-seq result file did not contain usable volcano-plot rows.")

        significance_mode = _significance_mode(parsed_rows)
        padj_threshold = float(summary_meta.get("padj_threshold") or 0.05)
        p_value_threshold = float(summary_meta.get("p_value_threshold") or 0.05)
        base_mean_threshold = float(summary_meta.get("base_mean_threshold") or 0.0)
        lfc_threshold = float(summary_meta.get("log2_fold_change_threshold") or 1.0)

        x_values: list[float] = []
        y_values: list[float] = []
        colors: list[str] = []
        up_rows: list[tuple[float, float, str]] = []
        down_rows: list[tuple[float, float, str]] = []
        for row in parsed_rows:
            log2fc = float(row["log2_fold_change"])
            p_value = row.get("padj")
            if p_value is None:
                p_value = row.get("p_value")
            y_value = self._neg_log10(p_value)
            significant = _is_significant(
                row,
                significance_mode=significance_mode,
                padj_threshold=padj_threshold,
                p_value_threshold=p_value_threshold,
                base_mean_threshold=base_mean_threshold,
                log2_fold_change_threshold=lfc_threshold,
            )
            if significant and log2fc >= lfc_threshold:
                color = "#3fb950"
                up_rows.append((y_value, log2fc, clean_text(row.get("gene"))))
            elif significant and log2fc <= -lfc_threshold:
                color = "#f85149"
                down_rows.append((y_value, log2fc, clean_text(row.get("gene"))))
            else:
                color = "#8b949e"
            x_values.append(log2fc)
            y_values.append(y_value)
            colors.append(color)

        fig, ax = plt.subplots(figsize=(8.8, 6.2))
        ax.scatter(x_values, y_values, c=colors, s=12, alpha=0.72, linewidths=0)
        ax.axvline(lfc_threshold, color="#7cc7ff", linestyle="--", linewidth=1)
        ax.axvline(-lfc_threshold, color="#7cc7ff", linestyle="--", linewidth=1)
        if significance_mode == "padj":
            ax.axhline(self._neg_log10(padj_threshold), color="#7cc7ff", linestyle="--", linewidth=1)
        elif significance_mode == "p_value":
            ax.axhline(self._neg_log10(p_value_threshold), color="#7cc7ff", linestyle="--", linewidth=1)
        ax.set_xlabel("log2 fold change")
        ax.set_ylabel("-log10(significance)")
        ax.set_title(title)
        ax.grid(alpha=0.14, linestyle="--")
        ax.set_axisbelow(True)

        for y_value, log2fc, gene in sorted(up_rows, reverse=True)[:5]:
            ax.text(log2fc, y_value, f" {gene}", fontsize=8, color="#3fb950")
        for y_value, log2fc, gene in sorted(down_rows, reverse=True)[:5]:
            ax.text(log2fc, y_value, f" {gene}", fontsize=8, color="#f85149")

        fig.tight_layout()
        self._save_figure(fig, stem)
        return (
            f"Volcano plot of differential expression with {len(parsed_rows)} ranked rows. "
            f"Green points are significantly upregulated and red points are significantly downregulated."
        )

    def _plot_cross_modal_concordance(self, artifact: Artifact, *, stem: Path, title: str) -> str:
        payload = artifact.payload or {}
        points = self._cross_modal_points(payload)
        if not points:
            raise ProviderError("This integration artifact has no direction-comparable shared gene observations.")
        labels = [clean_text(item.get("label")) for item in (payload.get("inputs") or [])[:2]]
        colors = {"shared_concordant": "#238b45", "shared_discordant": "#cb181d", "within_modality_conflicted": "#756bb1"}
        fig, axis = plt.subplots(figsize=(7.5, 6.5))
        for state in sorted({clean_text(point.get("state")) for point in points}):
            selected = [point for point in points if clean_text(point.get("state")) == state]
            axis.scatter([float(point["x"]) for point in selected], [float(point["y"]) for point in selected], s=34, alpha=0.8, color=colors.get(state, "#636363"), label=state.replace("_", " "))
        axis.axhline(0, color="#9e9e9e", linewidth=0.8)
        axis.axvline(0, color="#9e9e9e", linewidth=0.8)
        axis.set_xlabel(f"{labels[0]} log2 fold change")
        axis.set_ylabel(f"{labels[1]} log2 fold change")
        axis.set_title(title)
        axis.legend(frameon=False, fontsize=8)
        fig.tight_layout()
        self._save_figure(fig, stem)
        return "Cross-modal scatter of shared gene effect sizes; colors indicate concordance states without candidate ranking."

    def _plot_rnaseq_enrichment_graph(
        self,
        artifact: Artifact,
        *,
        stem: Path,
        title: str,
        top_n: int,
        direction: str,
        plot_type: str,
    ) -> dict[str, Any]:
        if self.r_runner is None:
            raise ProviderError("R-backed RNA-seq enrichment maps require a configured GLESKit settings context.")

        plot_input = self._rnaseq_enrichment_plot_input(artifact, direction)
        if not plot_input:
            raise ProviderError(f"This RNA-seq artifact does not contain the saved {direction} enrichment inputs needed for an enrichplot graph.")

        png_path = stem.with_suffix(".png")
        svg_path = stem.with_suffix(".svg")
        payload = self.r_runner.run(
            "gleskit_enrichment_figure.R",
            {
                "genes": plot_input["genes"],
                "sources": plot_input["sources"],
                "saved_terms": plot_input.get("saved_terms") or [],
                "fold_changes": self._rnaseq_fold_change_rows(artifact, plot_input["genes"]),
                "title": title,
                "top_n": max(3, min(int(top_n), 20)),
                "prefer_source": self._preferred_source(plot_input.get("sources") or []),
                "plot_type": plot_type,
                "output_png_path": str(png_path),
                "output_svg_path": str(svg_path),
            },
            capability=f"{direction} RNA-seq {plot_type}",
            timeout_seconds=self._r_figure_timeout_seconds(),
        )
        self._figure_file_payload(stem)
        default_caption = "ClusterProfiler/enrichplot enrichment map." if plot_type == "emapplot" else "ClusterProfiler/enrichplot gene-concept network."
        return {
            "caption": clean_text(payload.get("caption")) or default_caption,
            "render_mode": clean_text(payload.get("render_mode")) or plot_type,
            "rendered_source": clean_text(payload.get("rendered_source")),
            "rendered_term_count": payload.get("rendered_term_count"),
            "rendered_top_n": payload.get("rendered_top_n"),
            "fallback_reason": clean_text(payload.get("fallback_reason")),
        }

    def _plot_ranked_gsea_figure(self, artifact: Artifact, *, stem: Path, title: str, top_n: int) -> str:
        if self.r_runner is None:
            raise ProviderError("R-backed GSEA plots require a configured GLESKit settings context.")

        plot_input = self._ranked_gsea_input(artifact)
        if not plot_input:
            raise ProviderError("This artifact does not contain the saved ranked-gene inputs needed for a GSEA plot.")

        png_path = stem.with_suffix(".png")
        svg_path = stem.with_suffix(".svg")
        payload = self.r_runner.run(
            "gleskit_ranked_gsea_figure.R",
            {
                "gene_scores": plot_input["gene_scores"],
                "sources": plot_input["sources"],
                "title": title,
                "top_n": max(5, min(int(top_n), 30)),
                "prefer_source": "REAC",
                "output_png_path": str(png_path),
                "output_svg_path": str(svg_path),
            },
            capability="ranked GSEA plot",
            timeout_seconds=self._r_figure_timeout_seconds(),
        )
        self._figure_file_payload(stem)
        return clean_text(payload.get("caption")) or "ClusterProfiler/enrichplot ranked GSEA plot."

    @staticmethod
    def _save_figure(fig, stem: Path) -> None:
        png_path = stem.with_suffix(".png")
        svg_path = stem.with_suffix(".svg")
        fig.savefig(png_path, dpi=150, bbox_inches="tight", facecolor="#ffffff")
        fig.savefig(svg_path, bbox_inches="tight", facecolor="#ffffff")
        plt.close(fig)

    @staticmethod
    def _rank_terms(terms: list[dict[str, Any]], *, top_n: int) -> list[dict[str, Any]]:
        ranked = [dict(term) for term in terms if term.get("term_name")]
        ranked.sort(key=lambda row: (float(row.get("p_value") or 1.0), -int(row.get("intersection_size") or 0)))
        return ranked[: max(1, min(int(top_n), 20))]

    @staticmethod
    def _term_label(term: dict[str, Any]) -> str:
        name = clean_text(term.get("term_name")) or clean_text(term.get("term_id")) or "term"
        source = clean_text(term.get("source"))
        return f"{name} [{source}]" if source else name

    @staticmethod
    def _neg_log10(value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = 1.0
        safe = max(numeric, 1e-300)
        return -math.log10(safe)
