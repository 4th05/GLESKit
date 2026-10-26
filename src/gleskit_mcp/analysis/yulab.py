from __future__ import annotations

import os
import subprocess
from typing import Any, Callable

from gleskit_mcp.analysis.r_runner import RJsonScriptRunner
from gleskit_mcp.config import Settings
from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text


class YuLabAnalysisBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.settings = settings
        self.rscript_bin = settings.rscript_bin
        self.command_runner = command_runner or subprocess.run
        self.runner = RJsonScriptRunner(
            settings,
            command_runner=self.command_runner,
            rscript_bin=self.rscript_bin,
        )

    def _execution_timeout_seconds(self, requested_timeout_seconds: int | None = None) -> int:
        """Honor configured, enclosing, and explicit workflow runtime budgets."""
        configured_timeout = max(20, int(getattr(self.settings, "local_r_timeout_seconds", 45) or 45))
        try:
            evaluation_timeout = int(os.environ.get("GLESKIT_EVAL_TOOL_TIMEOUT_SECONDS", "0") or 0)
        except ValueError:
            evaluation_timeout = 0
        try:
            requested_timeout = max(0, int(requested_timeout_seconds or 0))
        except (TypeError, ValueError):
            requested_timeout = 0
        return max(configured_timeout, evaluation_timeout, requested_timeout)

    def gene_set_disease_context(
        self,
        *,
        entrez_gene_ids: list[str],
        database: str = "DO",
        limit: int = 15,
    ) -> ProviderResponse:
        normalized_ids = []
        seen: set[str] = set()
        for gene_id in entrez_gene_ids:
            token = clean_text(gene_id)
            if not token or token in seen:
                continue
            seen.add(token)
            normalized_ids.append(token)
        if not normalized_ids:
            raise ProviderError("Local disease enrichment requires at least one resolved Entrez gene identifier.")

        normalized_database = clean_text(database).upper() or "DO"
        if normalized_database not in {"DO", "DGN", "NCG"}:
            raise ProviderError("gene_set_disease_context supports database='DO', 'DGN', or 'NCG'.")

        payload = self._run_r_script(
            "gleskit_disease_enrichment.R",
            {
                "database": normalized_database,
                "limit": max(1, min(int(limit), 30)),
                "entrez_gene_ids": normalized_ids,
            },
            capability="disease enrichment",
            timeout_seconds=self._execution_timeout_seconds(),
        )
        summary = clean_text(payload.get("summary"))
        terms = payload.get("terms") or []
        if not summary:
            summary = (
                f"Retrieved {len(terms)} disease enrichment term(s) from {normalized_database} "
                f"for {len(normalized_ids)} resolved gene(s)."
            )
        return ProviderResponse(
            provider=clean_text(payload.get("provider")) or "yulab_dose",
            summary=summary,
            payload=payload,
        )

    def pathway_enrichment(
        self,
        *,
        genes: list[str],
        species: str = "human",
        sources: list[str] | None = None,
        limit: int = 15,
        background_genes: list[str] | None = None,
        simplify_go_similarity: float = 0.0,
        focus_terms: list[str] | None = None,
    ) -> ProviderResponse:
        normalized_genes = self._normalize_gene_symbols(genes, species=species)
        if not normalized_genes:
            raise ProviderError("Local pathway enrichment requires at least one gene symbol.")
        normalized_background = self._normalize_gene_symbols(background_genes or [], species=species)

        normalized_sources = self._normalize_pathway_sources(sources)
        payload = self._run_r_script(
            "gleskit_pathway_enrichment.R",
            {
                "genes": normalized_genes,
                "sources": normalized_sources,
                "limit": max(1, min(int(limit), 30)),
                "background_genes": normalized_background,
                "simplify_go_similarity": max(0.0, min(float(simplify_go_similarity or 0.0), 0.99)),
                "focus_terms": [str(term).strip() for term in focus_terms or [] if str(term).strip()][:10],
                "species": species,
            },
            capability="pathway enrichment",
            timeout_seconds=self._execution_timeout_seconds(),
        )
        summary = clean_text(payload.get("summary"))
        terms = payload.get("terms") or []
        if not summary:
            summary = (
                f"Retrieved {len(terms)} pathway enrichment term(s) "
                f"for {len(normalized_genes)} input gene(s)."
            )
        return ProviderResponse(
            provider=clean_text(payload.get("provider")) or "yulab_clusterprofiler",
            summary=summary,
            payload=payload,
        )

    def ranked_pathway_gsea(
        self,
        *,
        ranked_gene_scores: list[dict[str, Any]],
        species: str = "human",
        sources: list[str] | None = None,
        limit: int = 15,
        execution_timeout_seconds: int | None = None,
    ) -> ProviderResponse:
        normalized_scores = self._normalize_ranked_gene_scores(ranked_gene_scores, species=species)
        if len(normalized_scores) < 4:
            raise ProviderError("Local ranked GSEA requires at least 4 ranked genes with usable scores.")

        normalized_sources = self._normalize_pathway_sources(sources)
        payload = self._run_r_script(
            "gleskit_ranked_gsea.R",
            {
                "gene_scores": normalized_scores,
                "sources": normalized_sources,
                "limit": max(1, min(int(limit), 30)),
                "species": species,
            },
            capability="ranked GSEA",
            timeout_seconds=self._execution_timeout_seconds(execution_timeout_seconds),
        )
        summary = clean_text(payload.get("summary"))
        terms = payload.get("terms") or []
        if not summary:
            summary = f"Retrieved {len(terms)} ranked GSEA term(s) for {len(normalized_scores)} ranked genes."
        return ProviderResponse(
            provider=clean_text(payload.get("provider")) or "yulab_clusterprofiler",
            summary=summary,
            payload=payload,
        )

    def compare_gene_programs(
        self,
        *,
        programs: dict[str, list[str]],
        species: str = "human",
        database: str = "GO_BP",
        limit: int = 10,
    ) -> ProviderResponse:
        normalized_programs: dict[str, list[str]] = {}
        for label, genes in (programs or {}).items():
            normalized_label = clean_text(label)
            normalized_genes = self._normalize_gene_symbols(genes, species=species)
            if normalized_label and normalized_genes:
                normalized_programs[normalized_label] = normalized_genes
        if len(normalized_programs) < 2:
            raise ProviderError("Gene-program comparison requires at least two non-empty named gene programs.")

        normalized_database = clean_text(database).upper() or "GO_BP"
        if normalized_database not in {"GO_BP", "REAC"}:
            raise ProviderError("compare_gene_programs supports database='GO_BP' or 'REAC'.")

        payload = self._run_r_script(
            "gleskit_compare_programs.R",
            {
                "programs": normalized_programs,
                "database": normalized_database,
                "limit": max(1, min(int(limit), 20)),
                "species": species,
            },
            capability="gene-program comparison",
            timeout_seconds=self._execution_timeout_seconds(),
        )
        summary = clean_text(payload.get("summary"))
        rows = payload.get("comparison_terms") or []
        if not summary:
            summary = (
                f"Compared {len(normalized_programs)} gene program(s) "
                f"and retrieved {len(rows)} enrichment row(s)."
            )
        return ProviderResponse(
            provider=clean_text(payload.get("provider")) or "yulab_clusterprofiler",
            summary=summary,
            payload=payload,
        )

    def _run_r_script(
        self,
        script_name: str,
        payload: dict[str, Any],
        *,
        capability: str,
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        return self.runner.run(script_name, payload, capability=capability, timeout_seconds=timeout_seconds)

    @staticmethod
    def _normalize_gene_symbols(genes: list[str], *, species: str = "human") -> list[str]:
        normalized_genes: list[str] = []
        seen: set[str] = set()
        for gene in genes:
            token = clean_text(gene)
            token = token.upper() if _species_is_human(species) else token
            if not token or token in seen:
                continue
            seen.add(token)
            normalized_genes.append(token)
        return normalized_genes

    @staticmethod
    def _normalize_pathway_sources(sources: list[str] | None) -> list[str]:
        normalized_sources: list[str] = []
        seen: set[str] = set()
        for source in sources or ["GO:BP", "REAC"]:
            token = clean_text(source).upper()
            if token == "GO_BP":
                token = "GO:BP"
            if token in {"GO:BP", "REAC", "KEGG"} and token not in seen:
                seen.add(token)
                normalized_sources.append(token)
        return normalized_sources or ["GO:BP", "REAC"]

    @staticmethod
    def _normalize_ranked_gene_scores(
        ranked_gene_scores: list[dict[str, Any]],
        *,
        species: str = "human",
    ) -> list[dict[str, float | str]]:
        normalized: dict[str, float] = {}
        for row in ranked_gene_scores:
            gene = clean_text((row or {}).get("gene"))
            gene = gene.upper() if _species_is_human(species) else gene
            score_raw = (row or {}).get("score")
            if not gene or score_raw is None:
                continue
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                continue
            current = normalized.get(gene)
            if current is None or abs(score) > abs(current):
                normalized[gene] = score
        ranked = sorted(normalized.items(), key=lambda item: item[1], reverse=True)
        return [{"gene": gene, "score": score} for gene, score in ranked]


def _species_is_human(species: str) -> bool:
    token = clean_text(species).lower().replace(" ", "_")
    return token in {"human", "homo_sapiens", "9606"}
