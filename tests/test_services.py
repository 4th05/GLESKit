from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gleskit_mcp.analysis import ExpressionMatrixAnalysisBackend, ExpressionStatisticsBackend
from gleskit_mcp.config import load_settings
from gleskit_mcp.models import dump_json
from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.reference_data import ReferenceCatalog
from gleskit_mcp.services import GLESKitService, WorkspaceUnavailableError
from gleskit_mcp.workspace import WorkspaceGateway, write_runtime_manifest


class _FakeMyGeneProvider:
    def resolve_gene(self, query: str, *, species: str = "human", size: int = 5) -> ProviderResponse:
        normalized = query.upper()
        synthetic_entrez = sum(ord(ch) for ch in normalized)
        return ProviderResponse(
            provider="mygene",
            summary=f"Resolved {query}.",
            payload={
                "query": query,
                "species": species,
                "match_count": 1,
                "matches": [
                    {
                        "symbol": normalized,
                        "name": f"{normalized} name",
                        "summary": f"{normalized} summary",
                        "ensembl_gene_id": f"ENSG_{normalized}",
                        "entrez_gene_id": synthetic_entrez,
                        "aliases": [],
                        "uniprot_ids": [],
                        "genomic_position": None,
                        "biotype": "protein_coding",
                        "score": 10.0,
                    }
                ],
            },
        )


class _FailingMyGeneProvider:
    def resolve_gene(self, query: str, *, species: str = "human", size: int = 5) -> ProviderResponse:
        raise ProviderError("MyGene network error: timeout")


class _EmptyMyGeneProvider:
    def resolve_gene(self, query: str, *, species: str = "human", size: int = 5) -> ProviderResponse:
        return ProviderResponse(
            provider="mygene",
            summary=f"No gene matches found for '{query}'.",
            payload={
                "query": query,
                "species": species,
                "match_count": 0,
                "matches": [],
            },
        )


class _FakeGProfilerProvider:
    def enrich_genes(self, genes: list[str], *, species: str = "human", limit: int = 15, sources: list[str] | None = None) -> ProviderResponse:
        active_source = (sources or ["GO:BP"])[0]
        if active_source == "KEGG":
            terms = [
                {"term_id": "KEGG:00010", "term_name": "Glycolysis / Gluconeogenesis", "source": "KEGG", "p_value": 1e-5},
                {"term_id": "KEGG:00020", "term_name": "Citrate cycle (TCA cycle)", "source": "KEGG", "p_value": 2e-4},
            ]
        elif active_source == "WP":
            terms = [
                {"term_id": "WP:1", "term_name": "Apoptosis", "source": "WP", "p_value": 1e-5},
                {"term_id": "WP:2", "term_name": "Cell cycle", "source": "WP", "p_value": 2e-4},
            ]
        elif active_source == "REAC":
            terms = [
                {"term_id": "R-HSA-1", "term_name": "Cell cycle checkpoints", "source": "REAC", "p_value": 2e-4},
                {"term_id": "R-HSA-2", "term_name": "DNA repair", "source": "REAC", "p_value": 1e-5},
            ]
        else:
            terms = [
                {"term_id": "GO:0001", "term_name": "DNA repair", "source": "GO:BP", "p_value": 1e-5},
                {"term_id": "GO:0002", "term_name": "Cell cycle checkpoints", "source": "GO:BP", "p_value": 2e-4},
            ]
        return ProviderResponse(
            provider="gprofiler",
            summary=f"Retrieved enrichment for {len(genes)} gene(s).",
            payload={
                "species": species,
                "genes": genes,
                "term_count": len(terms),
                "terms": terms,
            },
        )


class _FakeOpenTargetsProvider:
    def associated_diseases(self, ensembl_gene_id: str, *, limit: int = 10) -> ProviderResponse:
        symbol = ensembl_gene_id.replace("ENSG_", "")
        return ProviderResponse(
            provider="open_targets",
            summary=f"Diseases for {symbol}.",
            payload={
                "ensembl_gene_id": ensembl_gene_id,
                "approved_symbol": symbol,
                "approved_name": symbol,
                "association_count": 1,
                "diseases": [
                    {
                        "disease_id": "MONDO:1",
                        "disease_name": f"{symbol} syndrome",
                        "description": "Test disease",
                        "score": 0.9,
                        "top_datasources": [{"datasource_id": "europepmc", "score": 0.5}],
                    }
                ],
            },
        )


class _FakeDESeq2Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def analyze_counts(
        self,
        *,
        counts_path: str,
        sample_metadata_path: str = "",
        sample_metadata_text: str = "",
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
        log2_fold_change_threshold: float = 1.0,
        lfc_shrinkage: str = "none",
        **extra: object,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "analyze_counts",
                {
                    "counts_path": counts_path,
                    "sample_metadata_path": sample_metadata_path,
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
                    **extra,
                },
            )
        )
        return {
            "provider": "deseq2",
            "summary": "Ran DESeq2 on 3 genes across 2 samples.",
            "input": {
                "counts_path": counts_path,
                "sample_metadata_path": sample_metadata_path,
                "sample_metadata_source": "inline" if sample_metadata_text else "file",
                "label": label,
                "comparison": comparison,
                "gene_column": gene_column,
                "sample_column": sample_column,
                "condition_column": condition_column,
                "condition_a": condition_a,
                "condition_b": condition_b,
                "design_formula": design_formula,
                "lfc_shrinkage": lfc_shrinkage,
            },
            "counts": {
                "row_count": 3,
                "filtered_row_count": 3,
                "sample_count": 2,
                "condition_column": condition_column,
                "sample_column": sample_column,
                "condition_levels": ["A", "B"],
                "contrast_label": "B_vs_A",
                "min_count": min_count,
                "padj_threshold": padj_threshold,
                "log2_fold_change_threshold": log2_fold_change_threshold,
                "lfc_shrinkage": lfc_shrinkage,
            },
            "summary_stats": {
                "total_gene_count": 3,
                "filtered_gene_count": 3,
                "sample_count": 2,
                "significant_gene_count": 2,
                "upregulated_count": 1,
                "downregulated_count": 1,
                "contrast_label": "B_vs_A",
                "padj_threshold": padj_threshold,
                "log2_fold_change_threshold": log2_fold_change_threshold,
                "lfc_shrinkage": lfc_shrinkage,
            },
            "top_genes": {
                "upregulated": [
                    {"gene": "TP53", "log2_fold_change": 2.1, "padj": 0.01, "p_value": 0.001},
                ],
                "downregulated": [
                    {"gene": "BRCA1", "log2_fold_change": -1.8, "padj": 0.02, "p_value": 0.003},
                ],
            },
            "significant_gene_sets": {
                "upregulated": ["TP53"],
                "downregulated": ["BRCA1"],
                "all_significant": ["TP53", "BRCA1"],
            },
            "ranked_gene_scores": [
                {"gene": "TP53", "score": 4.2, "score_source": "stat", "log2_fold_change": 2.1, "padj": 0.01, "p_value": 0.001},
                {"gene": "BRCA1", "score": -3.8, "score_source": "stat", "log2_fold_change": -1.8, "padj": 0.02, "p_value": 0.003},
            ],
            "results": [
                {"gene": "TP53", "base_mean": 100.0, "log2_fold_change": 2.1, "lfc_se": 0.2, "stat": 4.2, "p_value": 0.001, "padj": 0.01},
                {"gene": "BRCA1", "base_mean": 80.0, "log2_fold_change": -1.8, "lfc_se": 0.3, "stat": -3.8, "p_value": 0.003, "padj": 0.02},
                {"gene": "GAPDH", "base_mean": 300.0, "log2_fold_change": 0.1, "lfc_se": 0.1, "stat": 0.2, "p_value": 0.8, "padj": 0.9},
            ],
        }


class _AnnotationGroupFakeDESeq2Backend(_FakeDESeq2Backend):
    """Adds a second non-target annotation result for group-comparison tests."""

    def analyze_counts(self, **kwargs: object) -> dict[str, object]:
        result = super().analyze_counts(**kwargs)
        rows = list(result["results"])
        rows.append(
            {
                "gene": "ACTB",
                "base_mean": 250.0,
                "log2_fold_change": 0.7,
                "lfc_se": 0.2,
                "stat": 3.5,
                "p_value": 0.004,
                "padj": 0.03,
            }
        )
        result["results"] = rows
        return result


class _FakeExpressionMatrixBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def normalize_analysis_axis(axis: str, *, default: str = "columns") -> str:
        token = str(axis or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not token:
            return default
        alias_map = {
            "rows": "rows",
            "row": "rows",
            "samples_in_rows": "rows",
            "genes_in_columns": "rows",
            "columns": "columns",
            "column": "columns",
            "cols": "columns",
            "col": "columns",
            "samples_in_columns": "columns",
            "genes_in_rows": "columns",
        }
        if token in alias_map:
            return alias_map[token]
        raise ProviderError(
            "analysis_axis must be 'rows' or 'columns'. Use 'columns' when samples are in the columns."
        )

    def load_matrix(self, **kwargs):
        self.calls.append(("load_matrix", dict(kwargs)))
        import numpy as np

        return type(
            "LoadedMatrix",
            (),
            {
                "source_kind": "file",
                "path": str(kwargs.get("matrix_path") or ""),
                "source_text": str(kwargs.get("table_text") or ""),
                "delimiter": str(kwargs.get("delimiter") or "\t"),
                "row_labels": ["g1", "g2"],
                "column_labels": ["s1", "s2"],
                "values": np.array([[1.0, 2.0], [3.0, 4.0]], dtype=float),
                "row_id_column": str(kwargs.get("row_id_column") or "gene"),
                "analysis_axis_default": self.normalize_analysis_axis(str(kwargs.get("analysis_axis") or "columns")),
            },
        )()

    def run_log_transform(self, matrix, **kwargs):
        self.calls.append(("run_log_transform", dict(kwargs)))
        import numpy as np

        transformed = type(
            "LoadedMatrix",
            (),
            {
                "source_kind": matrix.source_kind,
                "path": matrix.path,
                "delimiter": matrix.delimiter,
                "row_labels": list(matrix.row_labels),
                "column_labels": list(matrix.column_labels),
                "values": np.log2(np.asarray(matrix.values, dtype=float) + float(kwargs.get("offset", 1.0))),
                "row_id_column": matrix.row_id_column,
                "analysis_axis_default": getattr(matrix, "analysis_axis_default", "columns"),
            },
        )()
        return transformed, {
            "summary": "Log transformed.",
            "provider": "expression_matrix",
            "analysis": "log_transform",
            "input": {"path": matrix.path, "source_kind": matrix.source_kind},
            "parameters": {
                "base": float(kwargs.get("base", 2.0)),
                "offset": float(kwargs.get("offset", 1.0)),
                "preset": str(kwargs.get("preset", "")),
            },
            "output": {
                "row_count": len(matrix.row_labels),
                "column_count": len(matrix.column_labels),
                "preview": [],
            },
        }

    def run_pca(self, matrix, **kwargs):
        self.calls.append(("run_pca", dict(kwargs)))
        return {
            "summary": "PCA done.",
            "provider": "expression_matrix",
            "analysis": "pca",
            "input": {"path": matrix.path, "source_kind": matrix.source_kind},
            "scores": [{"label": "s1", "PC1": 1.0}],
            "components": {"names": ["PC1"], "explained_variance": [1.0], "explained_variance_ratio": [1.0]},
            "loadings": [],
        }

    def run_hierarchical_clustering(self, matrix, **kwargs):
        self.calls.append(("run_hierarchical_clustering", dict(kwargs)))
        return {
            "summary": "Clustered.",
            "provider": "expression_matrix",
            "analysis": "hierarchical_clustering",
            "input": {"path": matrix.path, "source_kind": matrix.source_kind},
            "cluster_count": 2,
            "cluster_labels": {"s1": 1, "s2": 2},
            "clusters": [{"cluster": 1, "members": ["s1"], "size": 1}, {"cluster": 2, "members": ["s2"], "size": 1}],
            "merges": [],
            "order": ["s1", "s2"],
        }

    def run_correlation(self, matrix, **kwargs):
        self.calls.append(("run_correlation", dict(kwargs)))
        return {
            "summary": "Correlated.",
            "provider": "expression_matrix",
            "analysis": "correlation",
            "mode": "pairwise",
            "input": {"path": matrix.path, "source_kind": matrix.source_kind},
            "correlations": [{"label_a": "s1", "label_b": "s2", "correlation": 1.0, "p_value": 0.0, "padj": 0.0}],
        }

    def run_consensus_clustering(self, matrix, **kwargs):
        self.calls.append(("run_consensus_clustering", dict(kwargs)))
        return {
            "summary": "Consensus clustered.",
            "provider": "expression_matrix",
            "analysis": "consensus_clustering",
            "input": {"path": matrix.path, "source_kind": matrix.source_kind},
            "cluster_count": 2,
            "cluster_labels": {"s1": 1, "s2": 2},
            "clusters": [{"cluster": 1, "members": ["s1"], "size": 1}, {"cluster": 2, "members": ["s2"], "size": 1}],
            "merges": [],
            "order": ["s1", "s2"],
            "consensus_matrix": [[1.0, 0.0], [0.0, 1.0]],
            "stability": [{"label": "s1", "cluster": 1, "stability": 1.0}, {"label": "s2", "cluster": 2, "stability": 1.0}],
            "consistency_summary": {
                "scope": "all_observations",
                "observation_count": 2,
                "mean_stability": 1.0,
                "median_stability": 1.0,
                "minimum_stability": 1.0,
                "maximum_stability": 1.0,
                "counts_at_thresholds": [{"threshold": 0.9, "count": 2}, {"threshold": 1.0, "count": 2}],
                "per_cluster": [
                    {"cluster": 1, "sample_count": 1, "mean_stability": 1.0, "median_stability": 1.0, "minimum_stability": 1.0, "maximum_stability": 1.0},
                    {"cluster": 2, "sample_count": 1, "mean_stability": 1.0, "median_stability": 1.0, "minimum_stability": 1.0, "maximum_stability": 1.0},
                ],
            },
            "iterations": 10,
            "sample_fraction": 1.0,
            "feature_fraction": 1.0,
        }


class _FailingOpenTargetsProvider:
    def associated_diseases(self, ensembl_gene_id: str, *, limit: int = 10) -> ProviderResponse:
        raise ProviderError("Open Targets timed out while retrieving disease associations for ENSG_BGN.")


class _FakeSupervisedExpressionBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run_supervised_model(self, matrix, **kwargs):
        self.calls.append(("run_supervised_model", {"matrix_path": matrix.path, **dict(kwargs)}))
        return {
            "summary": "Supervised model completed.",
            "analysis": "supervised_expression_model",
            "input": {"path": matrix.path, "source_kind": matrix.source_kind},
            "task": str(kwargs.get("task") or "classification"),
            "model_family": str(kwargs.get("model_family") or "logistic_regression"),
            "split_strategy": str(kwargs.get("split_strategy") or "train_test"),
            "metrics": {"accuracy": 1.0},
            "predictions": [],
        }

    def evaluate_cluster_agreement(self, matrix, **kwargs):
        self.calls.append(("evaluate_cluster_agreement", {"matrix_path": matrix.path, **dict(kwargs)}))
        return {
            "summary": "Cluster agreement completed.",
            "analysis": "cluster_agreement",
            "input": {"path": matrix.path, "source_kind": matrix.source_kind},
            "evaluation_scope": {"agreement_population": "test_only", "split_count": 1},
            "agreement": {"count": 2, "rate": 1.0, "count_test_only": 2, "rate_test_only": 1.0},
            "parameters": {
                "cluster_count": int(kwargs.get("cluster_count", 3)),
                "iterations": int(kwargs.get("iterations", 50)),
                "train_fraction": float(kwargs.get("train_fraction", 0.7)),
                "metric": str(kwargs.get("metric") or "correlation"),
                "linkage": str(kwargs.get("linkage") or "average"),
            },
        }


class _FakeEnsemblProvider:
    def resolve_gene(self, query: str, *, species: str = "human") -> ProviderResponse | None:
        cleaned = query.upper().strip()
        if cleaned == "UNRESOLVABLE":
            return None
        symbol = cleaned
        ensembl_id = f"ENSG_{cleaned}"
        return ProviderResponse(
            provider="ensembl",
            summary=f"Resolved 1 candidate gene match for '{cleaned}' from Ensembl. Matched on symbol.",
            payload={
                "query": cleaned,
                "species": species,
                "match_count": 1,
                "matches": [
                    {
                        "query": cleaned,
                        "symbol": symbol,
                        "name": f"{symbol} description",
                        "summary": None,
                        "ensembl_gene_id": ensembl_id,
                        "entrez_gene_id": None,
                        "aliases": [],
                        "uniprot_ids": [],
                        "genomic_position": None,
                        "biotype": "protein_coding",
                        "score": 90.0,
                        "matched_on": "symbol",
                        "matched_value": symbol,
                    }
                ],
            },
        )

    def lookup_gene(self, ensembl_gene_id: str) -> ProviderResponse:
        symbol = ensembl_gene_id.replace("ENSG_", "")
        return ProviderResponse(
            provider="ensembl",
            summary=f"Ensembl context for {symbol}.",
            payload={
                "ensembl_gene_id": ensembl_gene_id,
                "display_name": symbol,
                "description": f"{symbol} genomic context",
                "species": "homo_sapiens",
                "assembly_name": "GRCh38",
                "seq_region_name": "17",
                "start": 100,
                "end": 900,
                "strand": 1,
                "biotype": "protein_coding",
                "canonical_transcript_id": f"ENST_{symbol}",
                "transcript_count": 2,
                "protein_coding_transcript_count": 2,
                "protein_ids": [f"ENSP_{symbol}"],
                "transcripts": [
                    {
                        "transcript_id": f"ENST_{symbol}",
                        "biotype": "protein_coding",
                        "is_canonical": True,
                        "protein_id": f"ENSP_{symbol}",
                    }
                ],
            },
        )


class _FakeUniProtProvider:
    def search_proteins(self, query: str, *, species: str = "human", size: int = 5) -> ProviderResponse:
        token = query.upper()
        return ProviderResponse(
            provider="uniprot",
            summary=f"Resolved protein candidates for {query}.",
            payload={
                "query": query,
                "effective_query": query,
                "species": species,
                "match_count": 1,
                "matches": [
                    {
                        "query": query,
                        "accession": f"P_{token}",
                        "entry_name": f"{token}_HUMAN",
                        "protein_name": f"{token} protein",
                        "gene_primary": token,
                        "gene_names": [token],
                        "organism_name": "Homo sapiens",
                        "organism_id": 9606,
                        "reviewed": True,
                        "length": 500,
                        "ensembl_gene_ids": [f"ENSG_{token}"],
                        "matched_on": "gene_primary",
                        "matched_value": token,
                    }
                ],
            },
        )

    def get_protein(self, accession: str) -> ProviderResponse:
        token = accession.replace("P_", "")
        return ProviderResponse(
            provider="uniprot",
            summary=f"Protein context for {accession}.",
            payload={
                "accession": accession,
                "entry_name": f"{token}_HUMAN",
                "protein_name": f"{token} protein",
                "recommended_name": f"{token} protein",
                "alternative_names": [f"{token} alt"],
                "gene_primary": token,
                "gene_names": [token, f"{token}_ALT"],
                "organism_name": "Homo sapiens",
                "organism_id": 9606,
                "reviewed": True,
                "length": 500,
                "function_summary": f"{token} controls test biology.",
                "subcellular_locations": ["Nucleus"],
                "ensembl_gene_ids": [f"ENSG_{token}"],
                "domains": [{"type": "Domain", "description": "Test domain", "start": 10, "end": 50}],
                "keywords": ["Reference proteome"],
            },
        )


class _FakeQuickGOProvider:
    def gene_product_annotations(self, gene_product_id: str, *, species: str = "human", limit: int = 10) -> ProviderResponse:
        return ProviderResponse(
            provider="quickgo",
            summary=f"QuickGO annotations for {gene_product_id}.",
            payload={
                "gene_product_id": gene_product_id,
                "species": species,
                "annotation_count": 2,
                "annotations": [
                    {
                        "gene_product_id": gene_product_id,
                        "gene_product_symbol": "TP53",
                        "go_id": "GO:0006977",
                        "go_name": "DNA damage response",
                        "aspect": "biological_process",
                        "evidence_code": "IDA",
                        "qualifier": None,
                        "assigned_by": "UniProt",
                        "references": ["PMID:1"],
                    }
                ],
                "terms": [
                    {
                        "go_id": "GO:0006977",
                        "go_name": "DNA damage response",
                        "aspect": "biological_process",
                        "evidence_code": "IDA",
                    }
                ],
            },
        )


class _FakeExpressionAtlasProvider:
    def reference_expression(
        self,
        query: str,
        *,
        species: str = "human",
        alternate_identifiers: list[str] | None = None,
        limit: int = 10,
    ) -> ProviderResponse:
        return ProviderResponse(
            provider="expression_atlas",
            summary=f"Expression Atlas context for {query}.",
            payload={
                "query": query,
                "species": "Homo sapiens",
                "reference_experiment": {
                    "accession": "E-MTAB-2836",
                    "type": "rnaseq_mrna_baseline",
                    "description": "Reference human tissue baseline RNA-seq study.",
                    "species": "Homo sapiens",
                    "main_page_url": "https://www.ebi.ac.uk/gxa/experiments/E-MTAB-2836",
                    "download_url": "https://www.ebi.ac.uk/gxa/download",
                    "genome_browser_url": "https://www.ebi.ac.uk/gxa/genome-browser",
                },
                "assay_group_count": 3,
                "assay_groups": [
                    {"assay_group_id": "g1", "tissue": "adipose tissue", "ontology_term_id": "UBERON_0001013", "replicate_count": 3},
                    {"assay_group_id": "g2", "tissue": "liver", "ontology_term_id": "UBERON_0002107", "replicate_count": 3},
                    {"assay_group_id": "g3", "tissue": "lung", "ontology_term_id": "UBERON_0002048", "replicate_count": 5},
                ],
                "query_gene_profile": {
                    "matched_on": "name",
                    "matched_value": "TP53",
                    "gene_id": "ENSG00000141510",
                    "gene_name": "TP53",
                    "expression_unit": "tpms",
                    "tissue_count": 3,
                    "tissues": [
                        {"assay_group_id": "g2", "tissue": "liver", "ontology_term_id": "UBERON_0002107", "replicate_count": 3, "value": 21.0},
                        {"assay_group_id": "g3", "tissue": "lung", "ontology_term_id": "UBERON_0002048", "replicate_count": 5, "value": 17.5},
                    ],
                    "gene_page_url": "https://www.ebi.ac.uk/gxa/genes/ENSG00000141510",
                },
                "search_result_total": 9570,
            },
        )


class _FakeGTExProvider:
    def expression_context(self, gene_query: str, *, limit: int = 10, dataset_id: str = "") -> ProviderResponse:
        return ProviderResponse(
            provider="gtex",
            summary=f"GTEx expression for {gene_query}.",
            payload={
                "query": gene_query,
                "dataset_id": "gtex_v8",
                "reference_gene": {
                    "gene_symbol": "TP53",
                    "gene_symbol_upper": "TP53",
                    "gencode_id": "ENSG00000141510.16",
                    "gencode_version": "v26",
                    "gene_type": "protein coding",
                    "description": "tumor protein p53",
                    "chromosome": "chr17",
                    "start": 7661779,
                    "end": 7687550,
                    "strand": "-",
                    "entrez_gene_id": 7157,
                    "dataset_source": "HAVANA",
                    "genome_build": "GRCh38/hg38",
                },
                "tissue_count": 3,
                "tissues": [
                    {"tissue_site_detail_id": "Cells_EBV-transformed_lymphocytes", "ontology_id": "EFO:0000572", "median": 72.9, "unit": "TPM", "dataset_id": "gtex_v8", "gencode_id": "ENSG00000141510.16", "gene_symbol": "TP53"},
                    {"tissue_site_detail_id": "Skin_Sun_Exposed_Lower_leg", "ontology_id": "UBERON:0004264", "median": 37.0, "unit": "TPM", "dataset_id": "gtex_v8", "gencode_id": "ENSG00000141510.16", "gene_symbol": "TP53"},
                    {"tissue_site_detail_id": "Breast_Mammary_Tissue", "ontology_id": "UBERON:0008367", "median": 24.6, "unit": "TPM", "dataset_id": "gtex_v8", "gencode_id": "ENSG00000141510.16", "gene_symbol": "TP53"},
                ],
            },
        )


class _FakeReactomeProvider:
    def pathway(
        self,
        pathway_id: str,
        *,
        participant_limit: int = 10,
        timeout_seconds: int = 5,
        participant_timeout_seconds: int = 3,
    ) -> ProviderResponse:
        return ProviderResponse(
            provider="reactome",
            summary=f"Reactome pathway for {pathway_id}.",
            payload={
                "pathway_id": pathway_id,
                "display_name": "Cell Cycle Checkpoints",
                "species_name": "Homo sapiens",
                "schema_class": "Pathway",
                "is_in_disease": False,
                "is_inferred": False,
                "summary": "Checkpoint control of the cell cycle.",
                "compartment": "nucleoplasm",
                "participant_count": 2,
                "participants": [
                    {"identifier": "R-HSA-TP53", "display_name": "TP53", "schema_class": "EntityWithAccessionedSequence"},
                    {"identifier": "R-HSA-CDK2", "display_name": "CDK2", "schema_class": "EntityWithAccessionedSequence"},
                ],
            },
        )


class _FakeStringDBProvider:
    def interaction_network(
        self,
        identifiers: list[str],
        *,
        species: str = "human",
        limit: int = 25,
        required_score: int = 700,
    ) -> ProviderResponse:
        return ProviderResponse(
            provider="string",
            summary=f"STRING interactions for {len(identifiers)} identifiers.",
            payload={
                "identifiers": identifiers,
                "species": species,
                "taxon_id": 9606,
                "edge_count": 2,
                "edges": [
                    {
                        "protein_a": "TP53",
                        "protein_b": "MDM2",
                        "string_id_a": "9606.ENSPTP53",
                        "string_id_b": "9606.ENSPMDM2",
                        "score": 0.99,
                        "neighborhood_score": 0.0,
                        "fusion_score": 0.0,
                        "cooccurrence_score": 0.0,
                        "coexpression_score": 0.1,
                        "experimental_score": 0.9,
                        "database_score": 0.8,
                        "textmining_score": 0.7,
                    }
                ],
                "ppi_enrichment": {
                    "number_of_nodes": 2,
                    "number_of_edges": 1,
                    "expected_number_of_edges": 0.1,
                    "average_node_degree": 1.0,
                    "local_clustering_coefficient": 0.0,
                    "p_value": 1e-5,
                },
            },
        )


class _FakeGWASCatalogProvider:
    def variant_trait_associations(self, gene_symbol: str, *, limit: int = 10) -> ProviderResponse:
        symbol = gene_symbol.upper()
        return ProviderResponse(
            provider="gwas_catalog",
            summary=f"GWAS associations for {symbol}.",
            payload={
                "gene_symbol": symbol,
                "association_count": 1,
                "associations": [
                    {
                        "association_id": "601",
                        "variant_id": "rs78378222-T",
                        "trait": f"{symbol} trait",
                        "efo_ids": ["EFO:0000305"],
                        "p_value": 1e-12,
                        "study_accession": "GCST000001",
                        "pubmed_id": "12345678",
                        "mapped_genes": [symbol],
                        "reported_genes": [symbol],
                    }
                ],
            },
        )


class _FakeChEMBLProvider:
    def target_drug_context(self, *, uniprot_accession: str, gene_symbol: str = "", limit: int = 10) -> ProviderResponse:
        return ProviderResponse(
            provider="chembl",
            summary=f"ChEMBL context for {gene_symbol or uniprot_accession}.",
            payload={
                "query_uniprot_accession": uniprot_accession,
                "query_gene_symbol": gene_symbol or None,
                "target": {
                    "target_chembl_id": "CHEMBL123",
                    "pref_name": f"{gene_symbol or 'Target'} protein",
                    "target_type": "SINGLE PROTEIN",
                    "organism": "Homo sapiens",
                    "accessions": [uniprot_accession],
                },
                "mechanism_count": 1,
                "mechanisms": [
                    {
                        "molecule_chembl_id": "CHEMBL25",
                        "molecule_name": "Nutlin-3",
                        "action_type": "INHIBITOR",
                        "mechanism_of_action": "Blocks signaling",
                        "max_phase": 2,
                        "first_approval": None,
                    }
                ],
                "drug_count": 1,
                "drugs": [
                    {
                        "molecule_chembl_id": "CHEMBL25",
                        "molecule_name": "Nutlin-3",
                        "max_phase": 2,
                        "first_approval": None,
                        "action_types": ["INHIBITOR"],
                        "mechanisms_of_action": ["Blocks signaling"],
                        "indications": [
                            {
                                "mesh_heading": "Neoplasms",
                                "efo_id": "EFO:0000616",
                                "efo_term": "neoplasm",
                                "max_phase_for_indication": 2,
                            }
                        ],
                    }
                ],
            },
        )


class _FakeEuropePMCProvider:
    def search_papers(self, query: str, *, limit: int = 5, open_access_only: bool = False) -> ProviderResponse:
        return ProviderResponse(
            provider="europe_pmc",
            summary=f"Papers for {query}.",
            payload={
                "query": query,
                "paper_count": 1,
                "papers": [
                    {
                        "id": "123",
                        "source": "MED",
                        "doi": "10.1000/test",
                        "title": "DNA repair in cancer",
                        "year": "2025",
                        "snippet": "A paper about TP53 and DNA repair.",
                        "cited_by_count": 42,
                    }
                ],
            },
        )


class _FakeYuLabBackend:
    def pathway_enrichment(
        self,
        *,
        genes: list[str],
        sources: list[str] | None = None,
        limit: int = 15,
        background_genes: list[str] | None = None,
        simplify_go_similarity: float = 0.0,
        focus_terms: list[str] | None = None,
    ) -> ProviderResponse:
        active_sources = sources or ["GO:BP", "REAC"]
        focused_terms = [
            {
                "term_id": "GO:0050863",
                "term_name": "regulation of T cell activation",
                "source": "GO:BP",
                "p_value": 0.0001,
                "adjusted_p_value": 0.0002,
                "intersection_size": min(3, len(genes)),
                "gene_ids": genes[: min(3, len(genes))],
                "matched_genes": genes[: min(3, len(genes))],
            }
        ] if focus_terms else []
        return ProviderResponse(
            provider="yulab_clusterprofiler",
            summary=f"Retrieved 2 pathway enrichment term(s) for {len(genes)} input gene(s).",
            payload={
                "analysis_mode": "ora",
                "sources": active_sources,
                "input_gene_count": len(genes),
                "mapped_gene_count": len(genes),
                "background_genes": background_genes or [],
                "simplify_go_similarity": float(simplify_go_similarity),
                "term_count": 2,
                "terms": [
                    {
                        "term_id": "GO:0006281",
                        "term_name": "DNA repair",
                        "source": "GO:BP",
                        "p_value": 1e-6,
                        "adjusted_p_value": 1e-4,
                        "intersection_size": min(2, len(genes)),
                        "gene_ids": genes[:2],
                        "matched_genes": genes[:2],
                    },
                    {
                        "term_id": "R-HSA-69620",
                        "term_name": "Cell cycle checkpoints",
                        "source": "REAC",
                        "p_value": 2e-5,
                        "adjusted_p_value": 3e-4,
                        "intersection_size": min(2, len(genes)),
                        "gene_ids": genes[:2],
                        "matched_genes": genes[:2],
                    },
                ],
                "focus_terms_requested": focus_terms or [],
                "focus_terms_found": focus_terms or [],
                "focused_terms": focused_terms,
            },
        )

    def gene_set_disease_context(
        self,
        *,
        entrez_gene_ids: list[str],
        database: str = "DO",
        limit: int = 15,
    ) -> ProviderResponse:
        return ProviderResponse(
            provider="yulab_dose",
            summary=f"Retrieved 2 disease enrichment term(s) from {database} for {len(entrez_gene_ids)} resolved gene(s).",
            payload={
                "database": database,
                "analysis_mode": "ora",
                "input_gene_count": len(entrez_gene_ids),
                "term_count": 2,
                "terms": [
                    {
                        "term_id": "DOID:162",
                        "term_name": "cancer",
                        "source": database,
                        "p_value": 1e-8,
                        "adjusted_p_value": 1e-6,
                        "intersection_size": 2,
                        "gene_ids": [entrez_gene_ids[0], entrez_gene_ids[1]],
                    },
                    {
                        "term_id": "DOID:3908",
                        "term_name": "lung carcinoma",
                        "source": database,
                        "p_value": 4e-5,
                        "adjusted_p_value": 6e-4,
                        "intersection_size": 1,
                        "gene_ids": [entrez_gene_ids[0]],
                    },
                ],
            },
        )

    def ranked_pathway_gsea(
        self,
        *,
        ranked_gene_scores: list[dict[str, object]],
        sources: list[str] | None = None,
        limit: int = 15,
    ) -> ProviderResponse:
        return ProviderResponse(
            provider="yulab_clusterprofiler",
            summary=f"Retrieved 2 ranked GSEA term(s) for {len(ranked_gene_scores)} ranked gene(s).",
            payload={
                "analysis_mode": "gsea",
                "sources": sources or ["GO:BP", "REAC"],
                "input_gene_count": len(ranked_gene_scores),
                "mapped_gene_count": len(ranked_gene_scores),
                "term_count": 2,
                "terms": [
                    {
                        "term_id": "GO:0007623",
                        "term_name": "circadian rhythm",
                        "source": "GO:BP",
                        "p_value": 1e-7,
                        "adjusted_p_value": 1e-5,
                        "normalized_enrichment_score": 2.1,
                        "direction": "positive",
                        "core_gene_ids": ["CLOCK", "ARNTL"],
                        "core_matched_genes": ["CLOCK", "ARNTL"],
                    },
                    {
                        "term_id": "R-HSA-156590",
                        "term_name": "G1/S transition",
                        "source": "REAC",
                        "p_value": 3e-4,
                        "adjusted_p_value": 2e-3,
                        "normalized_enrichment_score": -1.8,
                        "direction": "negative",
                        "core_gene_ids": ["CCND1", "MYC"],
                        "core_matched_genes": ["CCND1", "MYC"],
                    },
                ],
                "positive_terms": [
                    {
                        "term_id": "GO:0007623",
                        "term_name": "circadian rhythm",
                        "source": "GO:BP",
                        "adjusted_p_value": 1e-5,
                        "normalized_enrichment_score": 2.1,
                        "direction": "positive",
                    }
                ],
                "negative_terms": [
                    {
                        "term_id": "R-HSA-156590",
                        "term_name": "G1/S transition",
                        "source": "REAC",
                        "adjusted_p_value": 2e-3,
                        "normalized_enrichment_score": -1.8,
                        "direction": "negative",
                    }
                ],
            },
        )

    def compare_gene_programs(
        self,
        *,
        programs: dict[str, list[str]],
        database: str = "GO_BP",
        limit: int = 10,
    ) -> ProviderResponse:
        labels = list(programs.keys())
        return ProviderResponse(
            provider="yulab_clusterprofiler",
            summary=f"Compared {len(labels)} gene program(s) and retrieved 2 enrichment row(s).",
            payload={
                "analysis_mode": "ora_compare",
                "database": database,
                "program_count": len(labels),
                "programs": {
                    label: {
                        "gene_count": len(programs[label]),
                        "term_count": 1,
                        "terms": [
                            {
                                "program_label": label,
                                "term_id": f"GO:{index+1:07d}",
                                "term_name": f"{label} program term",
                                "source": "GO:BP" if database == "GO_BP" else "REAC",
                                "p_value": 1e-4 * (index + 1),
                                "adjusted_p_value": 2e-4 * (index + 1),
                                "intersection_size": 2,
                                "gene_ids": programs[label][:2],
                                "matched_genes": programs[label][:2],
                            }
                        ],
                    }
                    for index, label in enumerate(labels)
                },
                "comparison_terms": [
                    {
                        "program_label": label,
                        "term_id": f"GO:{index+1:07d}",
                        "term_name": f"{label} program term",
                        "source": "GO:BP" if database == "GO_BP" else "REAC",
                        "p_value": 1e-4 * (index + 1),
                        "adjusted_p_value": 2e-4 * (index + 1),
                        "intersection_size": 2,
                        "gene_ids": programs[label][:2],
                        "matched_genes": programs[label][:2],
                    }
                    for index, label in enumerate(labels)
                ],
            },
        )


class _FailingYuLabBackend(_FakeYuLabBackend):
    def pathway_enrichment(
        self,
        *,
        genes: list[str],
        sources: list[str] | None = None,
        limit: int = 15,
        background_genes: list[str] | None = None,
        simplify_go_similarity: float = 0.0,
    ) -> ProviderResponse:
        raise ProviderError("Local pathway backend failed.")


class _TransientYuLabBackend(_FakeYuLabBackend):
    def __init__(self) -> None:
        self.calls = 0

    def pathway_enrichment(
        self,
        *,
        genes: list[str],
        sources: list[str] | None = None,
        limit: int = 15,
        background_genes: list[str] | None = None,
        simplify_go_similarity: float = 0.0,
    ) -> ProviderResponse:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("provider header timeout")
        return super().pathway_enrichment(
            genes=genes,
            sources=sources,
            limit=limit,
            background_genes=background_genes,
            simplify_go_similarity=simplify_go_similarity,
        )


class _TimeoutYuLabBackend(_FakeYuLabBackend):
    def pathway_enrichment(
        self,
        *,
        genes: list[str],
        sources: list[str] | None = None,
        limit: int = 15,
        background_genes: list[str] | None = None,
        simplify_go_similarity: float = 0.0,
    ) -> ProviderResponse:
        raise ProviderError("provider header timeout")


class GLESKitServiceTests(unittest.TestCase):
    def build_service(
        self,
        root: Path,
        *,
        reference_catalog: ReferenceCatalog | None = None,
        yulab_backend: _FakeYuLabBackend | None = None,
        deseq2_backend: _FakeDESeq2Backend | None = None,
        expression_matrix_backend: _FakeExpressionMatrixBackend | None = None,
        expression_statistics_backend=None,
        single_cell_backend=None,
        supervised_expression_backend=None,
    ) -> GLESKitService:
        settings = load_settings(root)
        return GLESKitService(
            settings=settings,
            mygene=_FakeMyGeneProvider(),
            ensembl=_FakeEnsemblProvider(),
            uniprot=_FakeUniProtProvider(),
            expression_atlas=_FakeExpressionAtlasProvider(),
            gtex=_FakeGTExProvider(),
            quickgo=_FakeQuickGOProvider(),
            reactome=_FakeReactomeProvider(),
            stringdb=_FakeStringDBProvider(),
            gwas_catalog=_FakeGWASCatalogProvider(),
            chembl=_FakeChEMBLProvider(),
            gprofiler=_FakeGProfilerProvider(),
            europepmc=_FakeEuropePMCProvider(),
            opentargets=_FakeOpenTargetsProvider(),
            deseq2_backend=deseq2_backend or _FakeDESeq2Backend(),
            expression_matrix_backend=expression_matrix_backend or _FakeExpressionMatrixBackend(),
            expression_statistics_backend=expression_statistics_backend,
            single_cell_backend=single_cell_backend,
            supervised_expression_backend=supervised_expression_backend,
            yulab_backend=yulab_backend or _FakeYuLabBackend(),
            reference_catalog=reference_catalog,
            workspace_gateway=WorkspaceGateway(settings),
        )

    @staticmethod
    def build_reference_catalog(root: Path) -> ReferenceCatalog:
        settings = load_settings(root)
        sample_tsv = "\n".join(
            [
                "hgnc_id\tsymbol\tname\tstatus\tlocus_group\tlocus_type\talias_symbol\tprev_symbol\tuniprot_ids\tensembl_gene_id\tentrez_id",
                "HGNC:1040\tBGN\tbiglycan\tApproved\tprotein-coding gene\tgene with protein product\tDSPG1|DSPG2\tPGS1\tP21810\tENSG_BGN\t633",
                "HGNC:11998\tTP53\ttumor protein p53\tApproved\tprotein-coding gene\tgene with protein product\tP53\tTRP53\tP04637\tENSG_TP53\t7157",
                "HGNC:1680\tCLOCK\tcircadian locomotor output cycles kaput\tApproved\tprotein-coding gene\tgene with protein product\tKAT13D\t\tQ9Y6Q9\tENSG_CLOCK\t9576",
                "HGNC:701\tARNTL\taryl hydrocarbon receptor nuclear translocator like\tApproved\tprotein-coding gene\tgene with protein product\tBMAL1\t\tO00327\tENSG_ARNTL\t406",
            ]
        )
        catalog = ReferenceCatalog(settings, fetch_text=lambda _url: sample_tsv)
        catalog.refresh_all()
        return catalog

    def test_resolve_gene_is_stateless_when_workspace_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.resolve_gene(query="TP53")

            self.assertEqual(result["provider"], "mygene")
            self.assertFalse(result["workspace"]["available"])
            self.assertFalse(result["workspace"]["saved"])

    def test_resolve_gene_uses_local_reference_before_mygene(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            catalog = self.build_reference_catalog(root)
            service = GLESKitService(
                settings=settings,
                mygene=_FailingMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                expression_atlas=_FakeExpressionAtlasProvider(),
                gtex=_FakeGTExProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FakeOpenTargetsProvider(),
                reference_catalog=catalog,
                workspace_gateway=WorkspaceGateway(settings),
            )

            result = service.resolve_gene(query="DSPG2")

            self.assertEqual(result["provider"], "local_reference")
            self.assertEqual(result["structured_data"]["matches"][0]["symbol"], "BGN")
            self.assertEqual(result["structured_data"]["matches"][0]["matched_on"], "alias")

    def test_resolve_gene_falls_back_to_ensembl_when_mygene_returns_no_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            service = GLESKitService(
                settings=settings,
                mygene=_EmptyMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                expression_atlas=_FakeExpressionAtlasProvider(),
                gtex=_FakeGTExProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FakeOpenTargetsProvider(),
                workspace_gateway=WorkspaceGateway(settings),
            )

            result = service.resolve_gene(query="TP53")

            self.assertEqual(result["provider"], "ensembl")
            self.assertEqual(result["structured_data"]["matches"][0]["symbol"], "TP53")

    def test_resolve_gene_returns_mygene_empty_when_ensembl_also_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            service = GLESKitService(
                settings=settings,
                mygene=_EmptyMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                expression_atlas=_FakeExpressionAtlasProvider(),
                gtex=_FakeGTExProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FakeOpenTargetsProvider(),
                workspace_gateway=WorkspaceGateway(settings),
            )

            result = service.resolve_gene(query="UNRESOLVABLE")

            self.assertEqual(result["provider"], "mygene")
            self.assertEqual(result["structured_data"]["match_count"], 0)

    def test_resolve_gene_ensembl_fallback_handles_non_human_species(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            service = GLESKitService(
                settings=settings,
                mygene=_EmptyMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                expression_atlas=_FakeExpressionAtlasProvider(),
                gtex=_FakeGTExProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FakeOpenTargetsProvider(),
                workspace_gateway=WorkspaceGateway(settings),
            )

            result = service.resolve_gene(query="TP53", species="Mus musculus")

            self.assertEqual(result["provider"], "ensembl")
            self.assertIn(result["structured_data"]["species"], ("Mus musculus", "mus_musculus"))

    def test_resolve_gene_set_batches_resolution_and_reports_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            catalog = self.build_reference_catalog(root)
            service = GLESKitService(
                settings=settings,
                mygene=_FailingMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                expression_atlas=_FakeExpressionAtlasProvider(),
                gtex=_FakeGTExProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FakeOpenTargetsProvider(),
                reference_catalog=catalog,
                workspace_gateway=WorkspaceGateway(settings),
            )

            result = service.resolve_gene_set(genes=["DSPG2", "TP53", "MISSING"])

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["resolved_gene_count"], 2)
            self.assertEqual(result["structured_data"]["resolved_with_entrez_count"], 2)
            self.assertEqual(result["structured_data"]["unresolved_genes"], ["MISSING"])
            self.assertIn("investigate_gene_set", result["structured_data"]["recommended_next_tools"])
            self.assertIn("ordinary gene-set analysis", result["structured_data"]["usage_guidance"])

    def test_gene_disease_associations_can_use_local_reference_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            catalog = self.build_reference_catalog(root)
            service = GLESKitService(
                settings=settings,
                mygene=_FailingMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                expression_atlas=_FakeExpressionAtlasProvider(),
                gtex=_FakeGTExProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FakeOpenTargetsProvider(),
                reference_catalog=catalog,
                workspace_gateway=WorkspaceGateway(settings),
            )

            result = service.gene_disease_associations(gene="DSPG1")

            self.assertEqual(result["provider"], "open_targets")
            self.assertEqual(result["structured_data"]["approved_symbol"], "BGN")

    def test_workspace_capture_saves_to_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Circadian clock x cancer",
                description="Saved investigation workspace.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            result = service.resolve_gene(query="TP53")

            self.assertTrue(result["workspace"]["available"])
            self.assertTrue(result["workspace"]["saved"])
            artifacts = service.list_artifacts(created["session_id"])
            self.assertEqual(artifacts["artifact_count"], 1)
            self.assertEqual(artifacts["artifacts"][0]["kind"], "gene_resolution")

    def test_analyze_deseq2_counts_saves_results_and_qc_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="DESeq2 session",
                description="DESeq2 outputs should be captured as artifacts.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\tS1\tS2\nTP53\t10\t20\nBRCA1\t5\t2\n")

            result = service.analyze_deseq2_counts(
                counts_path=str(counts_path),
                sample_metadata_text="sample\tcondition\nS1\tA\nS2\tB\n",
                label="Counts vs metadata",
                comparison="B vs A",
            )
            artifacts = service.list_artifacts(created["session_id"])
            deseq2_results = [item for item in artifacts["artifacts"] if item["kind"] == "deseq2_results"]
            deseq2_qc = [item for item in artifacts["artifacts"] if item["kind"] == "deseq2_qc"]

            self.assertEqual(result["provider"], "deseq2")
            self.assertTrue(result["workspace"]["saved"])
            self.assertEqual(artifacts["artifact_count"], 2)
            self.assertEqual(len(deseq2_results), 1)
            self.assertEqual(len(deseq2_qc), 1)
            self.assertEqual(deseq2_results[0]["provider"], "deseq2")
            self.assertEqual(deseq2_results[0]["title"], "Counts vs metadata")
            self.assertEqual(deseq2_qc[0]["kind"], "deseq2_qc")
            self.assertIn("TP53", result["structured_data"]["significant_gene_sets"]["upregulated"])
            derived_inputs = result["structured_data"]["derived_inputs"]
            self.assertTrue(Path(derived_inputs["all_significant_genes_path"]).exists())
            self.assertTrue(Path(derived_inputs["ranked_gene_scores_path"]).exists())
            self.assertEqual(Path(derived_inputs["all_significant_genes_path"]).read_text(encoding="utf-8").splitlines()[0], "gene")

    def test_analyze_deseq2_counts_omits_inspection_only_column_1_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            counts_path = root / "counts.tsv"
            counts_path.write_text("\tS1\tS2\nTP53\t10\t20\n", encoding="utf-8")

            service.analyze_deseq2_counts(
                counts_path=str(counts_path),
                sample_metadata_text="sample\tcondition\nS1\tA\nS2\tB\n",
                gene_column="column_1",
            )

            self.assertEqual(backend.calls[-1][1]["gene_column"], "")

    def test_raw_counts_sample_group_map_materializes_complete_validated_metadata(self) -> None:
        import pandas as pd

        class _MetadataCaptureBackend(_FakeDESeq2Backend):
            def __init__(self) -> None:
                super().__init__()
                self.generated_metadata: list[dict[str, object]] = []

            def analyze_counts(self, **kwargs: object) -> dict[str, object]:
                metadata_path = Path(str(kwargs["sample_metadata_path"]))
                self.generated_metadata = pd.read_csv(metadata_path, sep="\t").to_dict(orient="records")
                return super().analyze_counts(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _MetadataCaptureBackend()
            service = self.build_service(root, deseq2_backend=backend)
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\tKD_1\tKD_2\tWT_1\tWT_2\nG1\t10\t12\t5\t6\nG2\t8\t9\t4\t5\n",
                encoding="utf-8",
            )

            inspection = service.inspect_tabular_data(path=str(counts_path))["structured_data"]
            self.assertEqual(inspection["matrix_sample_columns"], ["KD_1", "KD_2", "WT_1", "WT_2"])
            self.assertFalse(inspection["matrix_sample_columns_truncated"])

            result = service.analyze_deseq2_counts(
                counts_path=str(counts_path),
                sample_group_map={"KD_1": "KD", "KD_2": "KD", "WT_1": "WT", "WT_2": "WT"},
                condition_a="WT",
                condition_b="KD",
            )

            self.assertEqual(
                backend.generated_metadata,
                [
                    {"sample": "KD_1", "condition": "KD"},
                    {"sample": "KD_2", "condition": "KD"},
                    {"sample": "WT_1", "condition": "WT"},
                    {"sample": "WT_2", "condition": "WT"},
                ],
            )
            self.assertEqual(result["structured_data"]["input"]["sample_metadata_source"], "sample_group_map")
            self.assertEqual(result["structured_data"]["input"]["sample_group_map_sample_count"], 4)

            with self.assertRaises(ProviderError) as error:
                service.analyze_deseq2_counts(
                    counts_path=str(counts_path),
                    sample_group_map={"KD_1": "KD", "KD_2": "KD", "WT_1": "WT"},
                )
            self.assertEqual(error.exception.error_code, "invalid_input")
            self.assertIn("WT_2", str(error.exception))

    def test_prepare_rnaseq_inputs_accepts_complete_sample_group_map_without_metadata_file(self) -> None:
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\tA_1\tA_2\tB_1\tB_2\nG1\t10\t12\t5\t6\nG2\t8\t9\t4\t5\n",
                encoding="utf-8",
            )

            prepared = service.prepare_rnaseq_inputs(
                counts_path=str(counts_path),
                sample_group_map={"A_1": "A", "A_2": "A", "B_1": "B", "B_2": "B"},
            )["structured_data"]

            metadata = pd.read_csv(prepared["sanitized_metadata_path"], sep="\t")
            self.assertEqual(metadata.to_dict(orient="records"), [
                {"sample": "A_1", "condition": "A"},
                {"sample": "A_2", "condition": "A"},
                {"sample": "B_1", "condition": "B"},
                {"sample": "B_2", "condition": "B"},
            ])
            self.assertEqual(prepared["resolved_columns"]["condition_column"], "condition")

    def test_large_deseq2_artifact_is_previewed_for_the_model_but_remains_raw_in_workspace(self) -> None:
        class _HugeDESeq2Backend:
            def analyze_counts(
                self,
                *,
                counts_path: str,
                sample_metadata_path: str = "",
                sample_metadata_text: str = "",
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
                log2_fold_change_threshold: float = 1.0,
                lfc_shrinkage: str = "none",
            ) -> dict[str, object]:
                rows = [
                    {
                        "gene": f"GENE{i:04d}",
                        "log2_fold_change": float(i),
                        "padj": 0.001,
                        "p_value": 0.001,
                        "base_mean": float(i * 10),
                        "stat": float(i),
                    }
                    for i in range(150)
                ]
                return {
                    "provider": "deseq2",
                    "summary": "Huge DESeq2 result.",
                    "input": {
                        "counts_path": counts_path,
                        "sample_metadata_path": sample_metadata_path,
                        "sample_metadata_source": "inline" if sample_metadata_text else "file",
                        "label": label,
                        "comparison": comparison,
                        "gene_column": gene_column,
                        "sample_column": sample_column,
                        "condition_column": condition_column,
                        "condition_a": condition_a,
                        "condition_b": condition_b,
                        "design_formula": design_formula,
                        "lfc_shrinkage": lfc_shrinkage,
                    },
                    "counts": {
                        "row_count": 150,
                        "sample_count": 2,
                    },
                    "summary_stats": {
                        "total_gene_count": 150,
                        "filtered_gene_count": 150,
                        "sample_count": 2,
                        "significant_gene_count": 150,
                        "upregulated_count": 75,
                        "downregulated_count": 75,
                    },
                    "top_genes": {
                        "upregulated": rows[:10],
                        "downregulated": rows[75:85],
                    },
                    "significant_gene_sets": {
                        "upregulated": [row["gene"] for row in rows[:10]],
                        "downregulated": [row["gene"] for row in rows[75:85]],
                        "all_significant": [row["gene"] for row in rows],
                    },
                    "ranked_gene_scores": [
                        {"gene": row["gene"], "score": row["stat"]} for row in rows
                    ],
                    "results": rows,
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root, deseq2_backend=_HugeDESeq2Backend())
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Large artifact workspace",
                description="Large analysis artifacts should be previewed before reaching the model.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nGENE0001\t1\t2\n", encoding="utf-8")

            result = service.analyze_deseq2_counts(
                counts_path=str(counts_path),
                sample_metadata_text="sample\tcondition\ns1\tA\ns2\tB\n",
                label="Huge counts",
                comparison="B vs A",
            )
            result_artifact_id = str(result["artifacts"]["result"]["artifact_id"])

            preview = service.get_artifact(created["session_id"], result_artifact_id)
            _, workspace = service.workspace_gateway.require_workspace()
            raw_artifact = workspace.store.get_artifact(created["session_id"], result_artifact_id)

            self.assertEqual(preview["kind"], "deseq2_results")
            self.assertEqual(preview["preview"]["source_kind"], "artifact")
            self.assertEqual(len(preview["payload"]["results"]), 100)
            self.assertEqual(len(preview["payload"]["ranked_gene_scores"]), 100)
            self.assertEqual(len(raw_artifact.payload["results"]), 150)
            self.assertEqual(len(raw_artifact.payload["ranked_gene_scores"]), 150)

    def test_analyze_deseq2_counts_compacts_agent_payload_when_enabled(self) -> None:
        class _HugeDESeq2Backend:
            def analyze_counts(
                self,
                *,
                counts_path: str,
                sample_metadata_path: str = "",
                sample_metadata_text: str = "",
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
                log2_fold_change_threshold: float = 1.0,
                lfc_shrinkage: str = "none",
            ) -> dict[str, object]:
                rows = [
                    {
                        "gene": f"GENE{i:04d}",
                        "log2_fold_change": float(i),
                        "padj": 0.001,
                        "p_value": 0.001,
                        "base_mean": float(i * 10),
                        "stat": float(i),
                    }
                    for i in range(150)
                ]
                return {
                    "provider": "deseq2",
                    "summary": "Huge DESeq2 result.",
                    "input": {"counts_path": counts_path, "sample_metadata_path": sample_metadata_path},
                    "counts": {"row_count": 150, "sample_count": 2},
                    "summary_stats": {
                        "total_gene_count": 150,
                        "filtered_gene_count": 150,
                        "sample_count": 2,
                        "significant_gene_count": 150,
                        "upregulated_count": 75,
                        "downregulated_count": 75,
                    },
                    "top_genes": {
                        "upregulated": rows[:25],
                        "downregulated": rows[75:100],
                    },
                    "significant_gene_sets": {
                        "upregulated": [row["gene"] for row in rows[:75]],
                        "downregulated": [row["gene"] for row in rows[75:150]],
                        "all_significant": [row["gene"] for row in rows],
                    },
                    "ranked_gene_scores": [{"gene": row["gene"], "score": row["stat"]} for row in rows],
                    "results": rows,
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root, deseq2_backend=_HugeDESeq2Backend())
            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nGENE0001\t1\t2\n", encoding="utf-8")

            with patch.dict(os.environ, {"GLESKIT_COMPACT_TOOL_PAYLOADS": "1"}):
                result = service.analyze_deseq2_counts(
                    counts_path=str(counts_path),
                    sample_metadata_text="sample\tcondition\ns1\tA\ns2\tB\n",
                    label="Huge counts",
                    comparison="B vs A",
                )

            structured = result["structured_data"]
            self.assertEqual(structured["agent_payload_mode"], "compact")
            self.assertIn("significant_gene_set_preview", structured)
            self.assertNotIn("significant_gene_sets", structured)
            self.assertEqual(len(structured["significant_gene_set_preview"]["all_significant"]), 10)
            self.assertEqual(len(structured["top_genes"]["upregulated"]), 10)
            self.assertTrue(Path(structured["result_table_path"]).exists())

    def test_analyze_deseq2_counts_can_surface_focus_gene_metrics(self) -> None:
        class _FocusedDESeq2Backend:
            def analyze_counts(
                self,
                *,
                counts_path: str,
                sample_metadata_path: str = "",
                sample_metadata_text: str = "",
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
                log2_fold_change_threshold: float = 1.0,
                lfc_shrinkage: str = "none",
            ) -> dict[str, object]:
                return {
                    "provider": "deseq2",
                    "summary": "Focused DESeq2 result.",
                    "input": {"counts_path": counts_path, "sample_metadata_path": sample_metadata_path},
                    "counts": {"row_count": 2, "sample_count": 2},
                    "summary_stats": {
                        "total_gene_count": 2,
                        "filtered_gene_count": 2,
                        "sample_count": 2,
                        "significant_gene_count": 1,
                        "upregulated_count": 1,
                        "downregulated_count": 0,
                    },
                    "top_genes": {
                        "upregulated": [
                            {
                                "gene": "ENSG_GRIK5.14",
                                "base_mean": 45.0,
                                "log2_fold_change": 3.8,
                                "padj": 7.04e-26,
                                "p_value": 1e-20,
                                "stat": 12.0,
                            }
                        ],
                        "downregulated": [],
                    },
                    "significant_gene_sets": {
                        "upregulated": ["ENSG_GRIK5.14"],
                        "downregulated": [],
                        "all_significant": ["ENSG_GRIK5.14"],
                    },
                    "ranked_gene_scores": [{"gene": "ENSG_GRIK5.14", "score": 12.0}],
                    "results": [
                        {
                            "gene": "ENSG_GRIK5.14",
                            "base_mean": 45.0,
                            "log2_fold_change": 3.8,
                            "padj": 7.04e-26,
                            "p_value": 1e-20,
                            "stat": 12.0,
                        },
                        {
                            "gene": "ENSG_OTHER.1",
                            "base_mean": 12.0,
                            "log2_fold_change": 0.4,
                            "padj": 0.2,
                            "p_value": 0.1,
                            "stat": 1.0,
                        },
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self.build_reference_catalog(root)
            service = self.build_service(root, deseq2_backend=_FocusedDESeq2Backend(), reference_catalog=catalog)
            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nENSG_GRIK5.14\t1\t2\n", encoding="utf-8")

            result = service.analyze_deseq2_counts(
                counts_path=str(counts_path),
                sample_metadata_text="sample\tcondition\ns1\tA\ns2\tB\n",
                comparison="B vs A",
                focus_genes=["GRIK5"],
            )

            focus_rows = result["structured_data"]["focus_rows"]
            self.assertEqual(len(focus_rows), 1)
            self.assertTrue(focus_rows[0]["matched"])
            self.assertEqual(focus_rows[0]["matched_identifier"], "ENSG_GRIK5.14")
            self.assertAlmostEqual(float(focus_rows[0]["key_metrics"]["padj"]), 7.04e-26, places=32)
            self.assertIn("Focus genes", result["summary"])

    def test_run_expression_pca_saves_artifact_and_uses_shared_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeExpressionMatrixBackend()
            service = self.build_service(root, expression_matrix_backend=backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Expression matrix session",
                description="Matrix analysis should be saved as artifacts.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t1\t2\ng2\t3\t4\n")

            result = service.run_expression_pca(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                label="My matrix",
                force=True,
            )
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "expression_matrix")
            self.assertEqual([call[0] for call in backend.calls], ["load_matrix", "run_pca"])
            self.assertTrue(bool(backend.calls[1][1].get("force")))
            self.assertEqual(artifacts["artifact_count"], 1)
            self.assertEqual(artifacts["artifacts"][0]["kind"], "expression_pca")
            self.assertEqual(artifacts["artifacts"][0]["title"], "PCA: My matrix")
            self.assertAlmostEqual(result["pc1_explained_variance_percent"], 100.0)
            self.assertAlmostEqual(result["question_ready"]["pc1_explained_variance_percent"], 100.0)

    def test_run_expression_hierarchical_clustering_emits_cluster_labels_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeExpressionMatrixBackend()
            service = self.build_service(root, expression_matrix_backend=backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Hierarchical clustering session",
                description="Cluster assignments should be persisted for follow-up tools.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t1\t2\ng2\t3\t4\n", encoding="utf-8")

            result = service.run_expression_hierarchical_clustering(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                label="Cluster matrix",
            )

            cluster_labels_path = result["derived_inputs"]["cluster_labels_path"]
            self.assertTrue(Path(cluster_labels_path).exists())
            self.assertEqual(result["question_ready"]["cluster_labels_path"], cluster_labels_path)
            label_rows = Path(cluster_labels_path).read_text(encoding="utf-8").splitlines()
            self.assertIn("observation_label\tcluster", label_rows[0])
            self.assertEqual([call[0] for call in backend.calls], ["load_matrix", "run_hierarchical_clustering"])

    def test_run_expression_hierarchical_clustering_compacts_input_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeExpressionMatrixBackend()
            service = self.build_service(root, expression_matrix_backend=backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Compact hierarchical clustering session",
                description="Compact payloads should not expose full matrix labels in model-facing responses.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t1\t2\ng2\t3\t4\n", encoding="utf-8")

            with patch.dict(os.environ, {"GLESKIT_COMPACT_TOOL_PAYLOADS": "1"}):
                result = service.run_expression_hierarchical_clustering(
                    matrix_path=str(matrix_path),
                    row_id_column="gene",
                    label="Compact cluster matrix",
                )

            compact_input = result["structured_data"]["input"]
            self.assertNotIn("row_labels", compact_input)
            self.assertNotIn("column_labels", compact_input)
            self.assertIn("row_labels_preview", compact_input)
            self.assertIn("column_labels_preview", compact_input)

    def test_run_log_transform_saves_artifact_and_uses_shared_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeExpressionMatrixBackend()
            service = self.build_service(root, expression_matrix_backend=backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Log transform session",
                description="Log transforms should be saved as artifacts.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t0\t3\ng2\t1\t7\n", encoding="utf-8")

            result = service.run_log_transform(matrix_path=str(matrix_path), row_id_column="gene", label="My matrix")
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "expression_matrix")
            self.assertEqual([call[0] for call in backend.calls], ["load_matrix", "run_log_transform"])
            self.assertNotIn("transformed_matrix", result)
            dump_json(result)
            self.assertTrue(Path(result["matrix_path"]).exists())
            self.assertEqual(result["structured_data"]["derived_inputs"]["matrix_path"], result["matrix_path"])
            self.assertEqual(artifacts["artifact_count"], 1)
            self.assertEqual(artifacts["artifacts"][0]["kind"], "log_transform")
            self.assertEqual(artifacts["artifacts"][0]["title"], "Log transform: My matrix")

    def test_cluster_marker_analysis_accepts_cluster_labels_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Cluster marker session",
                description="Cluster marker analysis should accept saved cluster labels.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\ts4\n"
                "G1\t10\t9\t1\t1\n"
                "G2\t1\t1\t10\t9\n",
                encoding="utf-8",
            )
            cluster_labels_path = root / "cluster_labels.tsv"
            cluster_labels_path.write_text(
                "observation_label\tcluster\n"
                "s1\t1\n"
                "s2\t1\n"
                "s3\t2\n"
                "s4\t2\n",
                encoding="utf-8",
            )

            result = service.cluster_marker_analysis(
                matrix_path=str(matrix_path),
                row_id_column="gene",
                cluster_labels_path=str(cluster_labels_path),
                top_n=2,
                label="Marker path test",
            )

            self.assertEqual(result["provider"], "single_cell")
            self.assertEqual(result["question_ready"]["cluster_count"], 2)
            self.assertTrue(Path(result["derived_inputs"]["marker_gene_sets_path"]).exists())
            self.assertEqual(result["structured_data"]["cluster_count"], 2)

    def test_cluster_marker_analysis_compacts_model_visible_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Compact cluster marker session",
                description="Compact payloads should hide bulky marker gene blocks from the model-facing response.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\ts4\n"
                "G1\t10\t9\t1\t1\n"
                "G2\t1\t1\t10\t9\n",
                encoding="utf-8",
            )
            cluster_labels_path = root / "cluster_labels.tsv"
            cluster_labels_path.write_text(
                "observation_label\tcluster\n"
                "s1\t1\n"
                "s2\t1\n"
                "s3\t2\n"
                "s4\t2\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"GLESKIT_COMPACT_TOOL_PAYLOADS": "1"}):
                result = service.cluster_marker_analysis(
                    matrix_path=str(matrix_path),
                    row_id_column="gene",
                    cluster_labels_path=str(cluster_labels_path),
                    top_n=2,
                    label="Compact marker path test",
                )

            structured = result["structured_data"]
            self.assertIn("question_ready", structured)
            self.assertIn("derived_inputs", structured)
            self.assertNotIn("marker_gene_sets", structured)
            self.assertIn("significant_marker_gene_sets_json_path", structured["derived_inputs"])

    def test_cluster_marker_analysis_exposes_reactome_question_ready_from_term_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Cluster enrichment summary session",
                description="Question-ready cluster enrichment should expose the top Reactome pathway.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\ts4\n"
                "G1\t10\t9\t1\t1\n"
                "G2\t1\t1\t10\t9\n"
                "G3\t8\t8\t2\t2\n",
                encoding="utf-8",
            )
            cluster_labels_path = root / "cluster_labels.tsv"
            cluster_labels_path.write_text(
                "observation_label\tcluster\n"
                "s1\t1\n"
                "s2\t1\n"
                "s3\t2\n"
                "s4\t2\n",
                encoding="utf-8",
            )

            def _fake_enrichment(genes, *, species, label, warnings, database="", background_genes=None, simplify_go_similarity=0.0):
                cluster_token = "1" if str(label).endswith("cluster 1") else "2"
                if cluster_token == "1":
                    payload = {
                        "top_terms": [
                            {
                                "term_name": "Synaptic vesicle cycle",
                                "source": "REAC",
                                "adjusted_p_value": 0.002,
                                "intersection_size": 4,
                            }
                        ],
                        "terms": [
                            {"term_name": "Synaptic vesicle cycle", "adjusted_p_value": 0.002},
                            {"term_name": "Neurotransmitter release", "adjusted_p_value": 0.01},
                        ],
                    }
                else:
                    payload = {
                        "top_terms": [
                            {
                                "term_name": "Interferon signaling",
                                "source": "REAC",
                                "adjusted_p_value": 0.02,
                                "intersection_size": 3,
                            }
                        ],
                        "terms": [
                            {"term_name": "Interferon signaling", "adjusted_p_value": 0.02},
                        ],
                    }
                return payload, "local", "ok"

            with patch.object(service, "_pathway_enrichment_context", side_effect=_fake_enrichment):
                result = service.cluster_marker_analysis(
                    matrix_path=str(matrix_path),
                    row_id_column="gene",
                    cluster_labels_path=str(cluster_labels_path),
                    top_n=2,
                    enrich_top_markers=True,
                    enrichment_database="REAC",
                    label="Cluster Reactome test",
                )

            question_ready = result["question_ready"]
            self.assertEqual(question_ready["cluster_with_most_significant_pathways"], "1")
            self.assertEqual(question_ready["max_cluster_significant_pathway_count"], 2)
            self.assertEqual(question_ready["top_reactome_pathway_for_max_cluster"], "Synaptic vesicle cycle")
            self.assertTrue(Path(result["derived_inputs"]["significant_marker_gene_sets_json_path"]).exists())

    def test_run_log_transform_artifact_is_reusable_for_pca(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeExpressionMatrixBackend()
            service = self.build_service(root, expression_matrix_backend=backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Artifact reuse session",
                description="Transformed matrix artifacts should be reusable by selector.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t0\t3\ng2\t1\t7\n", encoding="utf-8")

            transformed = service.run_log_transform(matrix_path=str(matrix_path), row_id_column="gene", label="Reusable matrix")
            artifact_id = transformed["artifacts"]["result"]["artifact_id"]

            result = service.run_expression_pca(
                matrix_path=str(artifact_id),
                row_id_column="gene",
                label="Reusable PCA",
                force=True,
            )

            self.assertEqual([call[0] for call in backend.calls], ["load_matrix", "run_log_transform", "load_matrix", "run_pca"])
            reused_path = str(backend.calls[2][1].get("matrix_path") or "")
            self.assertEqual(reused_path, transformed["matrix_path"])
            self.assertAlmostEqual(result["question_ready"]["pc1_explained_variance_percent"], 100.0)

    def test_run_pipeline_chains_matrix_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeExpressionMatrixBackend()
            service = self.build_service(root, expression_matrix_backend=backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Pipeline matrix session",
                description="Pipelines should chain matrix steps.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\ng1\t0\t3\ng2\t1\t7\n", encoding="utf-8")

            result = service.run_pipeline(
                source={
                    "kind": "matrix",
                    "matrix_path": str(matrix_path),
                    "row_id_column": "gene",
                    "analysis_axis": "columns",
                },
                steps=[
                    {"tool": "run_log_transform", "params": {"base": 2.0, "offset": 1.0}},
                    {"tool": "run_expression_pca", "params": {"analysis_axis": "columns", "n_components": 2, "force": True}},
                ],
                label="Matrix pipeline",
            )
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "gleskit_pipeline")
            self.assertEqual([call[0] for call in backend.calls], ["load_matrix", "run_log_transform", "run_pca"])
            self.assertEqual(result["workspace"]["step_count"], 2)
            self.assertEqual(artifacts["artifact_count"], 2)
            self.assertEqual([item["kind"] for item in artifacts["artifacts"]], ["log_transform", "expression_pca"])
            self.assertEqual(result["structured_data"]["steps"][0]["tool"], "run_log_transform")
            self.assertEqual(result["structured_data"]["steps"][1]["tool"], "run_expression_pca")
            self.assertTrue(bool(backend.calls[2][1].get("force")))

    def test_run_pipeline_chains_deseq2_and_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Pipeline DESeq2 session",
                description="Pipelines should chain DESeq2 and enrichment.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t11\nBRCA1\t5\t2\nGAPDH\t100\t101\n", encoding="utf-8")

            result = service.run_pipeline(
                source={
                    "kind": "raw_counts",
                    "counts_path": str(counts_path),
                    "sample_metadata_text": "sample\tcondition\ns1\tA\ns2\tB\n",
                    "sample_column": "sample",
                    "condition_column": "condition",
                    "condition_a": "A",
                    "condition_b": "B",
                    "design_formula": "~ condition",
                    "species": "human",
                },
                steps=[
                    {
                        "tool": "analyze_deseq2_counts",
                        "params": {
                            "sample_column": "sample",
                            "condition_column": "condition",
                            "condition_a": "A",
                            "condition_b": "B",
                            "design_formula": "~ condition",
                        },
                    },
                    {"tool": "enrich_gene_set", "params": {"gene_source": "all_significant"}},
                ],
                label="DESeq2 pipeline",
            )
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "gleskit_pipeline")
            self.assertEqual(result["workspace"]["step_count"], 2)
            self.assertEqual([call[0] for call in service.deseq2_backend.calls], ["analyze_counts"])
            self.assertEqual(artifacts["artifact_count"], 3)
            self.assertEqual([item["kind"] for item in artifacts["artifacts"]], ["deseq2_results", "deseq2_qc", "enrichment"])
            self.assertEqual(result["structured_data"]["steps"][0]["tool"], "analyze_deseq2_counts")
            self.assertEqual(result["structured_data"]["steps"][1]["tool"], "enrich_gene_set")

    def test_run_pipeline_normalizes_gene_literature_shorthand_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Pipeline literature session",
                description="Pipeline mode should accept shorthand literature steps.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            result = service.run_pipeline(
                source={
                    "genes": ["BRCA1", "BRCA2"],
                },
                steps=[
                    {"gene_summary": {"query": "BRCA1", "species": "human"}},
                    {"gene_summary": {"query": "BRCA2", "species": "human"}},
                    {"interaction_network": {"genes": ["BRCA1", "BRCA2"], "limit": 50, "species": "human"}},
                    {"pathway_context": {"genes": ["BRCA1", "BRCA2"], "limit": 10, "species": "human"}},
                    {"gene_disease_associations": {"gene": "BRCA1", "limit": 10, "species": "human"}},
                    {"gene_disease_associations": {"gene": "BRCA2", "limit": 10, "species": "human"}},
                    {
                        "search_papers": {
                            "limit": 20,
                            "open_access_only": False,
                            "query": "(BRCA1 OR BRCA2) AND (homologous recombination OR \"DNA repair\")",
                        }
                    },
                ],
                label="BRCA1_BRCA2_literature_and_context",
            )
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "gleskit_pipeline")
            self.assertEqual(result["workspace"]["step_count"], 7)
            self.assertEqual(result["structured_data"]["source"]["kind"], "genes")
            self.assertEqual(
                [item["tool"] for item in result["structured_data"]["steps"]],
                [
                    "gene_summary",
                    "gene_summary",
                    "interaction_network",
                    "pathway_context",
                    "gene_disease_associations",
                    "gene_disease_associations",
                    "search_papers",
                ],
            )
            self.assertEqual(artifacts["artifact_count"], 7)
            self.assertEqual(artifacts["artifacts"][0]["kind"], "gene_summary")
            self.assertEqual(artifacts["artifacts"][-1]["kind"], "paper_search")

    def test_run_pipeline_infers_source_kind_for_literature_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Pipeline literature inference session",
                description="Pipeline source kind should be inferred from the stage list.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            result = service.run_pipeline(
                source={},
                steps=[
                    {"tool": "gene_summary", "params": {"query": "BRCA1", "species": "human"}},
                    {"tool": "gene_summary", "params": {"query": "BRCA2", "species": "human"}},
                    {
                        "tool": "search_papers",
                        "params": {
                            "query": "(BRCA1 OR BRCA2) AND (homologous recombination OR \"DNA repair\")",
                            "limit": 10,
                        },
                    },
                ],
                label="BRCA1_BRCA2_literature_and_context",
            )
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "gleskit_pipeline")
            self.assertEqual(result["structured_data"]["source"]["kind"], "genes")
            self.assertEqual(result["workspace"]["step_count"], 3)
            self.assertEqual(
                [item["tool"] for item in result["structured_data"]["steps"]],
                ["gene_summary", "gene_summary", "search_papers"],
            )
            self.assertEqual(artifacts["artifact_count"], 3)
            self.assertEqual([item["kind"] for item in artifacts["artifacts"]], ["gene_summary", "gene_summary", "paper_search"])

    def test_run_expression_statistics_saves_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_matrix_backend = ExpressionMatrixAnalysisBackend()
            statistics_backend = ExpressionStatisticsBackend(real_matrix_backend)
            service = self.build_service(
                root,
                expression_matrix_backend=real_matrix_backend,  # type: ignore[arg-type]
                expression_statistics_backend=statistics_backend,
            )
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Statistics session",
                description="Statistics outputs should be captured as artifacts.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            table_path = root / "values.tsv"
            table_path.write_text("value\tgroup\n1\tA\n2\tA\n6\tB\n7\tB\n", encoding="utf-8")

            result = service.run_expression_statistics(
                analysis="group_test",
                source_path=str(table_path),
                value_column="value",
                group_column="group",
                test="welch",
                label="Group stats",
            )
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "expression_statistics")
            self.assertTrue(result["workspace"]["saved"])
            self.assertEqual(result["structured_data"]["analysis"], "group_test")
            self.assertEqual(artifacts["artifact_count"], 1)
            self.assertEqual(artifacts["artifacts"][0]["kind"], "expression_statistics_group_test")

    def test_compare_differential_expression_results_saves_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="DE comparison session",
                description="DE comparisons should be captured as artifacts.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            first = root / "first.tsv"
            second = root / "second.tsv"
            first.write_text(
                "gene\tlog2FoldChange\tpadj\nTP53\t2.0\t0.01\nBRCA1\t-1.6\t0.02\nGAPDH\t0.1\t0.9\n",
                encoding="utf-8",
            )
            second.write_text(
                "gene\tlog2FoldChange\tpadj\nTP53\t1.8\t0.03\nEGFR\t2.1\t0.01\nBRCA1\t-0.2\t0.8\n",
                encoding="utf-8",
            )

            result = service.compare_differential_expression_results(
                results=[
                    {"label": "contrast_a", "result_path": str(first)},
                    {"label": "contrast_b", "result_path": str(second)},
                ],
                gene_source="all_significant",
                label="Compare A vs B",
            )
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["union_gene_count"], 3)
            self.assertEqual(result["structured_data"]["shared_across_all_gene_count"], 1)
            self.assertEqual(result["structured_data"]["pairwise"][0]["shared_count"], 1)
            self.assertEqual(artifacts["artifact_count"], 1)
            self.assertEqual(artifacts["artifacts"][0]["kind"], "differential_expression_comparison")

    def test_compare_differential_expression_results_accepts_compact_gene_list_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="DE comparison session",
                description="Compact gene-list handoff should be accepted.",
                species="Pseudomonas aeruginosa",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            first = root / "first.tsv"
            second = root / "second.tsv"
            first.write_text("gene\nA\nB\nC\n", encoding="utf-8")
            second.write_text("gene\nB\nC\nD\n", encoding="utf-8")

            result = service.compare_differential_expression_results(
                results=[
                    {"label": "contrast_a", "all_significant_genes_path": str(first)},
                    {"label": "contrast_b", "all_significant_genes_path": str(second)},
                ],
                gene_source="all_significant",
                label="Compare compact gene lists",
            )

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["union_gene_count"], 4)
            self.assertEqual(result["structured_data"]["shared_across_all_gene_count"], 2)
            self.assertEqual(result["structured_data"]["pairwise"][0]["shared_count"], 2)

    def test_compare_differential_expression_results_accepts_manifest_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            first = root / "first.tsv"
            second = root / "second.tsv"
            first.write_text("gene\nA\nB\nC\n", encoding="utf-8")
            second.write_text("gene\nB\nC\nD\n", encoding="utf-8")
            first_manifest = root / "first_manifest.json"
            second_manifest = root / "second_manifest.json"
            first_manifest.write_text(json.dumps({"files": {"all_significant_genes_path": str(first)}}), encoding="utf-8")
            second_manifest.write_text(json.dumps({"files": {"all_significant_genes_path": str(second)}}), encoding="utf-8")

            result = service.compare_differential_expression_results(
                results=[
                    {"label": "contrast_a", "manifest_path": str(first_manifest)},
                    {"label": "contrast_b", "manifest_path": str(second_manifest)},
                ],
                gene_source="all_significant",
                label="Compare manifest handoff",
            )

            self.assertEqual(result["structured_data"]["union_gene_count"], 4)
            self.assertEqual(result["structured_data"]["shared_across_all_gene_count"], 2)

    def test_compare_differential_expression_results_rejects_ranked_scores_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            first = root / "first.tsv"
            scores = root / "scores.tsv"
            first.write_text("gene\nA\nB\nC\n", encoding="utf-8")
            scores.write_text("gene\tscore\nA\t1.2\nB\t0.8\n", encoding="utf-8")

            with self.assertRaisesRegex(ProviderError, "ranked_gene_scores_path"):
                service.compare_differential_expression_results(
                    results=[
                        {"label": "contrast_a", "all_significant_genes_path": str(first)},
                        {"label": "contrast_b", "ranked_gene_scores_path": str(scores)},
                    ],
                    gene_source="all_significant",
                    label="Reject ranked handoff",
                )

    def test_compare_differential_expression_results_accepts_rds_inputs_via_conversion(self) -> None:
        class _FakeRNASeqBackend:
            def __init__(self) -> None:
                self.result_paths: list[str] = []

            def analyze_result(self, *, result_path: str = "", **kwargs):
                self.result_paths.append(result_path)
                return {
                    "summary": "Loaded converted DE result.",
                    "provider": "rnaseq",
                    "input": {"result_path": result_path},
                    "significant_gene_sets": {
                        "upregulated": ["A"],
                        "downregulated": ["B"],
                        "all_significant": ["A", "B"],
                    },
                    "direction_map": {"A": "upregulated", "B": "downregulated"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            fake_backend = _FakeRNASeqBackend()
            service.rnaseq_backend = fake_backend

            def _fake_rjson_run(script_name, request, capability="", timeout_seconds=0):
                output_path = Path(str(request["output_path"]))
                output_path.write_text("gene\tlog2FoldChange\tpadj\nA\t2.1\t0.01\nB\t-1.8\t0.02\n", encoding="utf-8")
                return {
                    "output_path": str(output_path),
                    "row_count": 2,
                    "column_count": 3,
                    "column_names": ["gene", "log2FoldChange", "padj"],
                    "source_class": ["DESeqResults"],
                }

            service.rjson_runner.run = _fake_rjson_run  # type: ignore[assignment]

            first_rds = root / "first.rds"
            second_rds = root / "second.rds"
            first_rds.write_text("placeholder", encoding="utf-8")
            second_rds.write_text("placeholder", encoding="utf-8")

            result = service.compare_differential_expression_results(
                results=[
                    {"label": "contrast_a", "result_path": str(first_rds)},
                    {"label": "contrast_b", "result_path": str(second_rds)},
                ],
                gene_source="all_significant",
                label="Compare RDS results",
            )

            self.assertEqual(result["structured_data"]["shared_across_all_gene_count"], 2)
            self.assertEqual(len(fake_backend.result_paths), 2)
            self.assertTrue(all(path.endswith(".tsv") for path in fake_backend.result_paths))
            self.assertTrue(all(Path(path).exists() for path in fake_backend.result_paths))

    def test_rds_conversion_script_writes_table_to_requested_output_path(self) -> None:
        rscript = shutil.which("Rscript")
        if not rscript:
            self.skipTest("Rscript is not available.")

        jsonlite_probe = subprocess.run(
            [rscript, "-e", "quit(status=ifelse(requireNamespace('jsonlite', quietly=TRUE), 0, 1))"],
            capture_output=True,
            text=True,
        )
        if jsonlite_probe.returncode != 0:
            self.skipTest("R package 'jsonlite' is not available.")

        script_path = Path(__file__).resolve().parents[1] / "src" / "gleskit_mcp" / "r" / "gleskit_rds_table.R"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rds_path = root / "result.rds"
            tsv_path = root / "converted.tsv"
            input_path = root / "request.json"
            response_path = root / "response.json"

            create_rds = subprocess.run(
                [
                    rscript,
                    "-e",
                    (
                        "frame <- data.frame(log2FoldChange=c(2.1,-1.8), padj=c(0.01,0.02), "
                        "row.names=c('GeneA','GeneB')); "
                        f"saveRDS(frame, file='{rds_path.as_posix()}')"
                    ),
                ],
                capture_output=True,
                text=True,
            )
            if create_rds.returncode != 0:
                self.fail(create_rds.stderr or create_rds.stdout)

            input_path.write_text(
                json.dumps({"rds_path": str(rds_path), "output_path": str(tsv_path)}),
                encoding="utf-8",
            )

            run = subprocess.run(
                [rscript, str(script_path), "--input", str(input_path), "--output", str(response_path)],
                capture_output=True,
                text=True,
            )
            if run.returncode != 0:
                self.fail(run.stderr or run.stdout)

            self.assertTrue(tsv_path.exists())
            self.assertTrue(response_path.exists())
            response = json.loads(response_path.read_text(encoding="utf-8"))
            self.assertEqual(response["output_path"], str(tsv_path))
            converted = tsv_path.read_text(encoding="utf-8")
            self.assertIn("log2FoldChange", converted)
            self.assertIn("GeneA", converted)

    def test_run_pipeline_compares_named_deseq2_steps(self) -> None:
        class _ComparisonDESeq2Backend(_FakeDESeq2Backend):
            def analyze_counts(self, **kwargs):
                label = str(kwargs.get("label") or "")
                if "strain97" in label.lower():
                    return {
                        **super().analyze_counts(**kwargs),
                        "summary": "strain97",
                        "significant_gene_sets": {
                            "upregulated": ["GENE_A", "GENE_B"],
                            "downregulated": ["GENE_C"],
                            "all_significant": ["GENE_A", "GENE_B", "GENE_C"],
                        },
                    }
                return {
                    **super().analyze_counts(**kwargs),
                    "summary": "strain99",
                    "significant_gene_sets": {
                        "upregulated": ["GENE_A"],
                        "downregulated": ["GENE_D"],
                        "all_significant": ["GENE_A", "GENE_D"],
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root, deseq2_backend=_ComparisonDESeq2Backend())
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Pipeline comparison session",
                description="Pipelines should compare named DESeq2 outputs.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nGENE_A\t10\t11\n", encoding="utf-8")

            result = service.run_pipeline(
                source={
                    "kind": "raw_counts",
                    "counts_path": str(counts_path),
                    "sample_metadata_text": "sample\tcondition\ns1\tA\ns2\tB\n",
                    "species": "human",
                },
                steps=[
                    {"id": "jbx97", "tool": "analyze_deseq2_counts", "label": "strain97", "params": {"condition_a": "A", "condition_b": "B"}},
                    {"id": "jbx99", "tool": "analyze_deseq2_counts", "label": "strain99", "params": {"condition_a": "A", "condition_b": "B"}},
                    {"tool": "compare_differential_expression_results", "params": {"from_steps": ["jbx97", "jbx99"]}},
                ],
                label="Comparison pipeline",
            )

            self.assertEqual(result["provider"], "gleskit_pipeline")
            self.assertEqual(result["workspace"]["step_count"], 3)
            self.assertEqual(result["structured_data"]["steps"][0]["id"], "jbx97")
            self.assertEqual(result["structured_data"]["steps"][1]["id"], "jbx99")
            self.assertEqual(result["structured_data"]["steps"][2]["tool"], "compare_differential_expression_results")

    def test_run_pipeline_can_use_uploaded_counts_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Upload-backed pipeline session",
                description="Pipeline should accept upload ids as authoritative inputs.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t2\n", encoding="utf-8")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\ns1\tA\ns2\tB\n", encoding="utf-8")

            counts_upload = service.create_session_upload(
                created["session_id"],
                data_path=str(counts_path),
                source_mode="linked",
                file_name="counts.tsv",
                source_path=str(counts_path),
            )
            metadata_upload = service.create_session_upload(
                created["session_id"],
                data_path=str(metadata_path),
                source_mode="linked",
                file_name="metadata.tsv",
                source_path=str(metadata_path),
            )

            result = service.run_pipeline(
                source={
                    "kind": "raw_counts",
                    "counts_path": counts_upload["upload_id"],
                    "sample_metadata_path": metadata_upload["upload_id"],
                    "species": "human",
                    "sample_column": "sample",
                    "condition_column": "condition",
                    "condition_a": "A",
                    "condition_b": "B",
                },
                steps=[
                    {"tool": "analyze_deseq2_counts", "params": {"condition_a": "A", "condition_b": "B"}},
                    {"tool": "enrich_gene_set", "params": {"gene_source": "all_significant", "database": "GO_BP"}},
                ],
                label="Upload-backed pipeline",
            )

            self.assertEqual(result["provider"], "gleskit_pipeline")
            self.assertEqual(result["workspace"]["step_count"], 2)
            self.assertEqual(result["structured_data"]["steps"][0]["tool"], "analyze_deseq2_counts")
            self.assertEqual(result["structured_data"]["steps"][1]["tool"], "enrich_gene_set")

    def test_consensus_clustering_accepts_upload_selector_and_axis_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expression_backend = _FakeExpressionMatrixBackend()
            service = self.build_service(root, expression_matrix_backend=expression_backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Matrix selector contract",
                description="Consensus clustering should accept upload ids directly.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t2\n", encoding="utf-8")
            upload = service.create_session_upload(
                created["session_id"],
                data_path=str(matrix_path),
                source_mode="linked",
                file_name="matrix.tsv",
                source_path=str(matrix_path),
            )

            result = service.run_expression_consensus_clustering(
                matrix_path=upload["upload_id"],
                row_id_column="gene",
                analysis_axis="samples_in_columns",
                cluster_count=3,
            )

            self.assertEqual(result["provider"], "expression_matrix")
            self.assertEqual(expression_backend.calls[0][0], "load_matrix")
            self.assertEqual(expression_backend.calls[0][1]["matrix_path"], str(matrix_path.resolve()))
            self.assertEqual(expression_backend.calls[0][1]["analysis_axis"], "columns")
            self.assertEqual(expression_backend.calls[1][0], "run_consensus_clustering")
            self.assertEqual(expression_backend.calls[1][1]["analysis_axis"], "columns")

    def test_supervised_expression_model_accepts_upload_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supervised_backend = _FakeSupervisedExpressionBackend()
            service = self.build_service(
                root,
                expression_matrix_backend=_FakeExpressionMatrixBackend(),
                supervised_expression_backend=supervised_backend,
            )
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Supervised selector contract",
                description="Supervised modeling should accept upload ids directly.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t2\n", encoding="utf-8")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\ns1\tA\ns2\tB\n", encoding="utf-8")
            matrix_upload = service.create_session_upload(
                created["session_id"],
                data_path=str(matrix_path),
                source_mode="linked",
                file_name="matrix.tsv",
                source_path=str(matrix_path),
            )
            metadata_upload = service.create_session_upload(
                created["session_id"],
                data_path=str(metadata_path),
                source_mode="linked",
                file_name="metadata.tsv",
                source_path=str(metadata_path),
            )

            result = service.run_supervised_expression_model(
                matrix_path=matrix_upload["upload_id"],
                row_id_column="gene",
                analysis_axis="genes_in_rows",
                metadata_path=metadata_upload["upload_id"],
                sample_column="sample",
                target_column="condition",
                task="classification",
            )

            self.assertEqual(result["provider"], "supervised_expression")
            self.assertEqual(supervised_backend.calls[0][0], "run_supervised_model")
            self.assertEqual(supervised_backend.calls[0][1]["matrix_path"], str(matrix_path.resolve()))
            self.assertEqual(supervised_backend.calls[0][1]["metadata_path"], str(metadata_path.resolve()))
            self.assertEqual(supervised_backend.calls[0][1]["analysis_axis"], "columns")

    def test_cluster_agreement_accepts_upload_selector_and_axis_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supervised_backend = _FakeSupervisedExpressionBackend()
            service = self.build_service(
                root,
                expression_matrix_backend=_FakeExpressionMatrixBackend(),
                supervised_expression_backend=supervised_backend,
            )
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Agreement selector contract",
                description="Cluster agreement should accept upload ids directly.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t2\n", encoding="utf-8")
            upload = service.create_session_upload(
                created["session_id"],
                data_path=str(matrix_path),
                source_mode="linked",
                file_name="matrix.tsv",
                source_path=str(matrix_path),
            )

            result = service.evaluate_cluster_agreement(
                matrix_path=upload["upload_id"],
                row_id_column="gene",
                analysis_axis="samples_in_columns",
                metric="correlation",
            )

            self.assertEqual(result["provider"], "supervised_expression")
            self.assertEqual(supervised_backend.calls[0][0], "evaluate_cluster_agreement")
            self.assertEqual(supervised_backend.calls[0][1]["matrix_path"], str(matrix_path.resolve()))
            self.assertEqual(supervised_backend.calls[0][1]["analysis_axis"], "columns")
            self.assertEqual(result["structured_data"]["evaluation_scope"]["agreement_population"], "test_only")
            self.assertEqual(result["question_ready"]["agreement_count_test_only"], 2)
            self.assertEqual(result["agreement_count_test_only"], 2)
            self.assertEqual(result["agreement_rate_test_only"], 1.0)

    def test_evaluate_cluster_agreement_compacts_model_facing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GLESKIT_COMPACT_TOOL_PAYLOADS": "1"}):
            root = Path(tmp)
            supervised_backend = _FakeSupervisedExpressionBackend()
            service = self.build_service(root, supervised_expression_backend=supervised_backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Cluster agreement compact payload",
                description="Cluster agreement should expose answer-bearing counts directly.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            matrix_path = root / "matrix.tsv"
            matrix_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t2\n", encoding="utf-8")
            upload = service.create_session_upload(
                created["session_id"],
                data_path=str(matrix_path),
                source_mode="linked",
                file_name="matrix.tsv",
                source_path=str(matrix_path),
            )

            result = service.evaluate_cluster_agreement(
                matrix_path=upload["upload_id"],
                row_id_column="gene",
                analysis_axis="samples_in_columns",
                metric="correlation",
            )

            self.assertEqual(result["question_ready"]["agreement_count_test_only"], 2)
            self.assertEqual(result["structured_data"]["agreement"]["count_test_only"], 2)
            self.assertNotIn("predicted_labels", result["structured_data"].get("test", {}))
            self.assertNotIn("labels", result["structured_data"].get("train", {}))

    def test_analyze_deseq2_counts_accepts_upload_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deseq2_backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=deseq2_backend)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="DESeq2 selector contract",
                description="DESeq2 should accept upload ids directly.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t2\n", encoding="utf-8")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\ns1\tA\ns2\tB\n", encoding="utf-8")
            counts_upload = service.create_session_upload(
                created["session_id"],
                data_path=str(counts_path),
                source_mode="linked",
                file_name="counts.tsv",
                source_path=str(counts_path),
            )
            metadata_upload = service.create_session_upload(
                created["session_id"],
                data_path=str(metadata_path),
                source_mode="linked",
                file_name="metadata.tsv",
                source_path=str(metadata_path),
            )

            result = service.analyze_deseq2_counts(
                counts_path=counts_upload["upload_id"],
                sample_metadata_path=metadata_upload["upload_id"],
                sample_column="sample",
                condition_column="condition",
                condition_a="A",
                condition_b="B",
            )

            self.assertEqual(result["provider"], "deseq2")
            self.assertEqual(deseq2_backend.calls[0][0], "analyze_counts")
            self.assertEqual(deseq2_backend.calls[0][1]["counts_path"], str(counts_path.resolve()))
            self.assertEqual(deseq2_backend.calls[0][1]["sample_metadata_path"], str(metadata_path.resolve()))

    def test_analyze_rnaseq_result_accepts_upload_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="RNA-seq selector contract",
                description="RNA-seq interpretation should accept upload ids directly.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            result_path = root / "de_results.tsv"
            result_path.write_text(
                "\n".join(
                    [
                        "gene_symbol\tlog2FoldChange\tpadj\tpvalue\tbaseMean",
                        "TP53\t2.1\t0.001\t0.0001\t100",
                        "BRCA1\t1.4\t0.02\t0.001\t80",
                        "EGFR\t-1.8\t0.01\t0.002\t120",
                        "CLOCK\t-2.3\t0.03\t0.004\t60",
                    ]
                ),
                encoding="utf-8",
            )
            upload = service.create_session_upload(
                created["session_id"],
                data_path=str(result_path),
                source_mode="linked",
                file_name="de_results.tsv",
                source_path=str(result_path),
            )

            result = service.analyze_rnaseq_result(
                result_path=upload["upload_id"],
                comparison="tumor vs control",
                label="Tumor signature",
            )

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["summary_stats"]["significant_gene_count"], 4)

    def test_analyze_rnaseq_result_accepts_deseq2_handoff_manifest_and_focus_gene_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self.build_reference_catalog(root)
            service = self.build_service(root, reference_catalog=catalog)
            result_table_path = root / "deseq2_results.tsv"
            manifest_path = root / "handoff_manifest.json"
            result_table_path.write_text(
                "\n".join(
                    [
                        "gene\tbase_mean\tlog2_fold_change\tstat\tp_value\tpadj",
                        "ENSG_TP53\t100\t2.5\t5.0\t0.0001\t0.001",
                        "ENSG_CLOCK\t80\t-1.5\t-3.0\t0.002\t0.01",
                    ]
                ),
                encoding="utf-8",
            )
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "DESeq2 result",
                        "comparison": "treated vs control",
                        "files": {"result_table_path": str(result_table_path)},
                        "column_map": {
                            "gene": "gene",
                            "log2FoldChange": "log2_fold_change",
                            "padj": "padj",
                            "pvalue": "p_value",
                            "stat": "stat",
                            "baseMean": "base_mean",
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = service.analyze_rnaseq_result(
                result_path=str(manifest_path),
                focus_genes=["TP53"],
                label="TP53 focus",
            )

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(
                result["structured_data"]["input"]["handoff_manifest"]["result_table_path"],
                str(result_table_path.resolve()),
            )
            focus_rows = result["structured_data"]["focus_rows"]
            self.assertEqual(len(focus_rows), 1)
            self.assertTrue(focus_rows[0]["matched"])
            self.assertEqual(focus_rows[0]["matched_identifier"], "ENSG_TP53")
            self.assertAlmostEqual(float(focus_rows[0]["key_metrics"]["log2_fold_change"]), 2.5, places=6)

    def test_analyze_rnaseq_result_accepts_legacy_deseq2_handoff_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            handoff_root = root / "handoff"
            handoff_root.mkdir(parents=True, exist_ok=True)
            result_table_path = handoff_root / "deseq2_results.tsv"
            manifest_path = handoff_root / "handoff_manifest.json"
            result_table_path.write_text(
                "\n".join(
                    [
                        "gene\tbase_mean\tlog2_fold_change\tstat\tp_value\tpadj",
                        "TP53\t100\t2.5\t5.0\t0.0001\t0.001",
                        "CLOCK\t80\t-1.5\t-3.0\t0.002\t0.01",
                    ]
                ),
                encoding="utf-8",
            )
            manifest_path.write_text(
                json.dumps(
                    {
                        "label": "Legacy DESeq2 result",
                        "comparison": "treated vs control",
                        "handoff_root": str(handoff_root),
                        "files": {
                            "all_significant_genes_path": str(handoff_root / "all_significant_genes.tsv"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = service.analyze_rnaseq_result(
                result_path=str(manifest_path),
                label="Legacy handoff",
            )

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(
                result["structured_data"]["input"]["handoff_manifest"]["result_table_path"],
                str(result_table_path.resolve()),
            )
            self.assertEqual(
                result["structured_data"]["table"]["column_map"]["gene"],
                "gene",
            )

    def test_analyze_rnaseq_result_matches_focus_gene_against_versioned_ensembl_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self.build_reference_catalog(root)
            service = self.build_service(root, reference_catalog=catalog)
            result_table_path = root / "deseq2_results.tsv"
            result_table_path.write_text(
                "\n".join(
                    [
                        "gene\tbase_mean\tlog2_fold_change\tstat\tp_value\tpadj",
                        "ENSG_GRIK5.14\t45\t3.8\tNA\t1e-20\t7.04e-26",
                    ]
                ),
                encoding="utf-8",
            )

            result = service.analyze_rnaseq_result(
                result_path=str(result_table_path),
                focus_genes=["GRIK5"],
                label="GRIK5 focus",
            )

            focus_rows = result["structured_data"]["focus_rows"]
            self.assertEqual(len(focus_rows), 1)
            self.assertTrue(focus_rows[0]["matched"])
            self.assertEqual(focus_rows[0]["matched_identifier"], "ENSG_GRIK5.14")

    def test_materialize_rds_result_table_uses_runner_returned_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            rds_path = root / "result.rds"
            rds_path.write_text("placeholder", encoding="utf-8")
            alternate_output = root / "alternate.tsv"

            def _fake_rjson_run(script_name, request, capability="", timeout_seconds=0):
                alternate_output.write_text("gene\tlog2FoldChange\tpadj\nA\t2.1\t0.01\n", encoding="utf-8")
                return {
                    "output_path": str(alternate_output),
                    "row_count": 1,
                    "column_count": 3,
                    "column_names": ["gene", "log2FoldChange", "padj"],
                    "source_class": ["DESeqResults"],
                }

            service.rjson_runner.run = _fake_rjson_run  # type: ignore[assignment]

            materialized_path, metadata = service._materialize_rds_result_table(str(rds_path))

            self.assertEqual(materialized_path, str(alternate_output))
            self.assertEqual(metadata["output_path"], str(alternate_output))
            self.assertTrue(Path(materialized_path).exists())

    def test_compare_differential_expression_results_exposes_question_ready_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            result = service.compare_differential_expression_results(
                results=[
                    {
                        "label": "A",
                        "structured_data": {
                            "significant_gene_sets": {
                                "all_significant": ["G1", "G2", "G3"],
                                "upregulated": ["G1", "G2"],
                                "downregulated": ["G3"],
                            }
                        },
                    },
                    {
                        "label": "B",
                        "structured_data": {
                            "significant_gene_sets": {
                                "all_significant": ["G2", "G4"],
                                "upregulated": ["G2"],
                                "downregulated": ["G4"],
                            }
                        },
                    },
                ],
                gene_source="all_significant",
            )

            question_ready = result["structured_data"]["question_ready_counts"]
            self.assertEqual(question_ready["exclusive_by_label"]["A"], 2)
            self.assertEqual(question_ready["exclusive_by_label"]["B"], 1)
            self.assertEqual(question_ready["union"], 4)

    def test_inspect_tabular_data_reports_workbook_sheet_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            workbook_path = root / "rna_inputs.xlsx"

            import pandas as pd

            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    [
                        ["ignore", "ignore", "ignore", "ignore"],
                        ["gene", "Sample_A_1", "Sample_B_1", "Sample_B_2"],
                        ["Gene1", 10, 20, 30],
                        ["Gene2", 5, 7, 8],
                    ]
                ).to_excel(writer, sheet_name="counts", index=False, header=False)
                pd.DataFrame(
                    {
                        "sample_id": ["Sample_A_1", "Sample_B_1", "Sample_B_2"],
                        "condition": ["A", "B", "B"],
                        "batch": ["x", "x", "y"],
                    }
                ).to_excel(writer, sheet_name="metadata", index=False)

            result = service.inspect_tabular_data(path=str(workbook_path))

            structured = result["structured_data"]
            self.assertEqual(structured["detected_data_type"], "raw_counts")
            self.assertIn("prepare_rnaseq_inputs", structured["candidate_analysis_families"])
            self.assertIn("analyze_deseq2_counts", structured["candidate_analysis_families"])
            self.assertEqual(structured["header_row"], 2)
            self.assertIn(structured["header_confidence"], {"high", "medium"})
            self.assertGreaterEqual(len(structured["sheet_roles"]), 2)
            self.assertTrue(
                any(
                    "inspection_preview_rows=" in note
                    for row in structured["sheet_roles"]
                    for note in row.get("provenance_notes", [])
                )
            )
            metadata_rows = [row for row in structured["sheet_roles"] if row["detected_data_type"] == "metadata"]
            self.assertTrue(metadata_rows)
            self.assertEqual(metadata_rows[0]["candidate_sample_column"], "sample_id")
            self.assertIn("condition", metadata_rows[0]["candidate_group_columns"])
            self.assertEqual(metadata_rows[0]["candidate_group_levels"]["condition"], ["A", "B"])
            self.assertEqual(metadata_rows[0]["candidate_sample_preview"][:2], ["Sample_A_1", "Sample_B_1"])
            self.assertEqual(structured["matrix_sample_preview"][:3], ["Sample_A_1", "Sample_B_1", "Sample_B_2"])
            self.assertEqual(structured["agent_payload_mode"], "compact")
            self.assertLess(len(json.dumps(structured)), 6_000)

    def test_prepare_rnaseq_inputs_infers_multisheet_handoff_from_verified_sample_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            workbook_path = root / "normalized_counts.xlsx"

            import pandas as pd

            baseline_samples = [f"Control_baseline_blood{index}" for index in range(1, 7)]
            final_samples = [f"Control_final_blood{index}" for index in range(1, 7)]
            control_samples = baseline_samples + final_samples
            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    [
                        ["", "", "baseline blood"],
                        ["", "", ""],
                        ["", "", ""],
                        ["Index", "GeneID", *control_samples, "stored_padj"],
                        [1, "G1", *[10.4 + index for index in range(12)], "NA"],
                        [2, "G2", *[4.2 + index for index in range(12)], "NA"],
                    ]
                ).to_excel(writer, sheet_name="NormCount", index=False, header=False)
                pd.DataFrame(
                    [["Index", "Sample ID", "Response", "Tissue", "Code"]]
                    + [
                        [index, f"internal_{index}", "Control", "baseline_blood", sample]
                        for index, sample in enumerate(baseline_samples, start=1)
                    ]
                    + [
                        [index, f"internal_{index}", "Control", "final_blood", sample]
                        for index, sample in enumerate(final_samples, start=7)
                    ]
                    + [[13, "internal_13", "Other", "baseline_blood", "Other_baseline_blood1"]]
                ).to_excel(writer, sheet_name="Mapping_statistics", index=False, header=False)

            inspection = service.inspect_tabular_data(path=str(workbook_path))["structured_data"]
            recommended = inspection["recommended_arguments"]
            self.assertEqual(recommended["counts_sheet_name"], "NormCount")
            self.assertEqual(recommended["counts_header_row"], 4)
            self.assertEqual(recommended["metadata_sheet_name"], "Mapping_statistics")
            self.assertEqual(recommended["metadata_header_row"], 1)
            self.assertEqual(recommended["gene_column"], "GeneID")
            self.assertEqual(recommended["sample_key_column"], "Code")
            self.assertEqual(recommended["count_transform"], "round_int")
            self.assertEqual(recommended["candidate_condition_columns"][:2], ["Tissue", "Response"])

            prepared = service.prepare_rnaseq_inputs(
                counts_path=str(workbook_path),
                condition_column="Tissue",
                sample_filter_column="Response",
                sample_filter_values=["Control"],
                design_columns=["Tissue"],
                count_transform="round_int",
            )["structured_data"]
            self.assertEqual(prepared["sample_count"], 12)
            self.assertEqual(prepared["feature_count"], 2)
            self.assertEqual(prepared["resolved_columns"]["gene_column"], "GeneID")
            self.assertEqual(prepared["resolved_columns"]["sample_key_column"], "Code")
            self.assertEqual(prepared["resolved_columns"]["condition_column"], "Tissue")
            sanitized_counts = pd.read_csv(prepared["sanitized_counts_path"], sep="\t")
            self.assertEqual(
                list(sanitized_counts.columns),
                ["GeneID", *control_samples],
            )
            self.assertEqual(sanitized_counts.iloc[0, 1], 10)
            self.assertEqual(sanitized_counts.iloc[0, 7], 16)

    def test_inspect_tabular_data_normalizes_blank_first_header_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            counts_path = root / "counts.csv"
            counts_path.write_text(
                ",s1,s2\n"
                "G1,10,20\n"
                "G2,30,40\n",
                encoding="utf-8",
            )

            result = service.inspect_tabular_data(path=str(counts_path), role_hint="counts")

            structured = result["structured_data"]
            self.assertEqual(structured["row_identifier_column"], "column_1")
            self.assertEqual(structured["candidate_gene_id_columns"], ["column_1"])
            self.assertEqual(structured["matrix_sample_preview"][:2], ["s1", "s2"])

    def test_inspect_tabular_data_parses_whitespace_delimited_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            counts_path = root / "feature_counts.txt"
            counts_path.write_text(
                "Geneid    sample_a    sample_b\n"
                "G1        10          20\n"
                "G2        30          40\n",
                encoding="utf-8",
            )

            result = service.inspect_tabular_data(path=str(counts_path))

            structured = result["structured_data"]
            self.assertEqual(structured["detected_data_type"], "raw_counts")
            self.assertEqual(structured["matrix_sample_preview"], ["sample_a", "sample_b"])

    def test_inspect_tabular_data_prefers_semantic_metadata_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            workbook_path = root / "metadata.xlsx"

            import pandas as pd

            pd.DataFrame(
                [
                    ["sample", "condition", "sex", "age"],
                    ["S1", "case", "F", 40],
                    ["S2", "control", "M", 42],
                ]
            ).to_excel(workbook_path, index=False, header=False)

            result = service.inspect_tabular_data(path=str(workbook_path))

            structured = result["structured_data"]
            self.assertEqual(structured["header_row"], 1)
            self.assertEqual(structured["candidate_sample_column"], "sample")
            self.assertIn("condition", structured["candidate_group_columns"])

    def test_inspect_tabular_data_routes_qpcr_matrix_to_continuous_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            matrix_path = root / "qpcr_values.tsv"
            matrix_path.write_text(
                "feature\tP_1\tP_2\tC_1\tC_2\n"
                "miR-1\t2.1\t2.2\t7.9\t8.1\n"
                "miR-2\t4.0\t4.1\t4.0\t4.1\n",
                encoding="utf-8",
            )

            result = service.inspect_tabular_data(path=str(matrix_path))
            structured = result["structured_data"]

            self.assertEqual(structured["detected_data_type"], "continuous_matrix")
            self.assertIn("run_expression_statistics", structured["candidate_analysis_families"])
            self.assertEqual(structured["candidate_sample_group_map"]["P_1"], "P")
            self.assertEqual(structured["matrix_orientation"], "genes_rows_samples_columns")
            self.assertIn("matrix_featurewise_group_test", " ".join(structured["next_step_hints"]))
            self.assertEqual(structured["recommended_arguments"]["analysis"], "matrix_featurewise_group_test")
            self.assertEqual(structured["recommended_arguments"]["analysis_axis"], "columns")
            self.assertEqual(structured["recommended_arguments"]["sample_group_map"]["C_1"], "C")

    def test_inspect_tabular_data_routes_rds_to_result_analysis_without_text_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            rds_path = root / "comparison.rds"
            rds_path.write_bytes(b"\x1f\x8bnot-a-text-table")

            result = service.inspect_tabular_data(path=str(rds_path))
            structured = result["structured_data"]

            self.assertEqual(structured["detected_data_type"], "deseq2_results")
            self.assertEqual(structured["file_format"], "rds")
            self.assertEqual(structured["candidate_analysis_families"], ["analyze_rnaseq_result"])
            self.assertIn("Do not parse it as text", " ".join(structured["next_step_hints"]))

    def test_analyze_stratified_deseq2_filters_and_compares_lfc_distributions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            sample_ids = [f"{stratum}_{sex}_{index}" for stratum in ("CD4", "CD8", "CD14") for sex in ("M", "F") for index in (1, 2)]
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\t" + "\t".join(sample_ids) + "\n"
                + "TP53\t" + "\t".join(["100"] * len(sample_ids)) + "\n"
                + "BRCA1\t" + "\t".join(["80"] * len(sample_ids)) + "\n"
                + "GAPDH\t" + "\t".join(["300"] * len(sample_ids)) + "\n",
                encoding="utf-8",
            )
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tcelltype\tsex\n"
                + "\n".join(f"{sample}\t{sample.split('_')[0]}\t{sample.split('_')[1]}" for sample in sample_ids)
                + "\n",
                encoding="utf-8",
            )
            annotation_path = root / "annotation.tsv"
            annotation_path.write_text(
                "gene\tgene_biotype\nTP53\tprotein_coding\nBRCA1\tprotein_coding\nGAPDH\tlncRNA\n",
                encoding="utf-8",
            )

            result = service.analyze_stratified_deseq2(
                counts_path=str(counts_path),
                sample_metadata_path=str(metadata_path),
                gene_column="gene",
                sample_key_column="sample",
                stratum_column="celltype",
                sex_column="sex",
                target_strata=["CD4", "CD8"],
                reference_strata=["CD14"],
                feature_annotation_path=str(annotation_path),
                feature_annotation_key_column="gene",
                feature_annotation_group_column="gene_biotype",
                base_mean_threshold=10.0,
                absolute_log2_fold_change_threshold=0.5,
                lfc_shrinkage="apeglm",
            )

            self.assertEqual(result["provider"], "gleskit_stratified_deseq2")
            self.assertEqual(result["question_ready"]["target_lfc_count"], 4)
            self.assertEqual(result["question_ready"]["reference_lfc_count"], 2)
            self.assertEqual(len(result["structured_data"]["per_stratum"]), 3)
            self.assertEqual(len(backend.calls), 3)
            self.assertTrue(all(call[1]["lfc_shrinkage"] == "apeglm" for call in backend.calls))

    def test_stratified_deseq2_focus_gene_endpoint_runs_one_pooled_contrast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            sample_ids = [
                f"{stratum}_{sex}_{index}"
                for stratum in ("CD4", "CD8", "CD14")
                for sex in ("M", "F")
                for index in (1, 2)
            ]
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\t" + "\t".join(sample_ids) + "\n"
                + "TP53\t" + "\t".join(["100"] * len(sample_ids)) + "\n"
                + "BRCA1\t" + "\t".join(["80"] * len(sample_ids)) + "\n",
                encoding="utf-8",
            )
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tcelltype\tsex\n"
                + "\n".join(f"{sample}\t{sample.split('_')[0]}\t{sample.split('_')[1]}" for sample in sample_ids)
                + "\n",
                encoding="utf-8",
            )

            result = service.analyze_stratified_deseq2(
                counts_path=str(counts_path),
                sample_metadata_path=str(metadata_path),
                gene_column="gene",
                sample_key_column="sample",
                stratum_column="celltype",
                sex_column="sex",
                analysis="focus_gene_lfc",
                focus_genes=["TP53"],
                lfc_shrinkage="apeglm",
            )

            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(result["question_ready"]["focus_rows"][0]["matched_identifier"], "TP53")
            self.assertEqual(result["question_ready"]["focus_rows"][0]["log2_fold_change"], 2.1)
            self.assertEqual(
                result["structured_data"]["input"]["target_strata"],
                ["CD4", "CD8", "CD14"],
            )

    def test_stratified_deseq2_count_endpoint_uses_padj_and_target_strata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            sample_ids = [
                f"{stratum}_{sex}_{index}"
                for stratum in ("CD4", "CD8", "CD14")
                for sex in ("M", "F")
                for index in (1, 2)
            ]
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\t" + "\t".join(sample_ids) + "\n"
                + "TP53\t" + "\t".join(["100"] * len(sample_ids)) + "\n"
                + "BRCA1\t" + "\t".join(["80"] * len(sample_ids)) + "\n",
                encoding="utf-8",
            )
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tcelltype\tsex\n"
                + "\n".join(f"{sample}\t{sample.split('_')[0]}\t{sample.split('_')[1]}" for sample in sample_ids)
                + "\n",
                encoding="utf-8",
            )

            result = service.analyze_stratified_deseq2(
                counts_path=str(counts_path),
                sample_metadata_path=str(metadata_path),
                gene_column="gene",
                sample_key_column="sample",
                stratum_column="celltype",
                sex_column="sex",
                target_strata=["CD4", "CD8"],
                reference_strata=["CD14"],
                analysis="significant_gene_count",
                padj_threshold=0.05,
                base_mean_threshold=10.0,
                absolute_log2_fold_change_threshold=0.5,
                lfc_shrinkage="apeglm",
            )

            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(result["question_ready"]["significant_gene_count"], 2)
            self.assertEqual(result["question_ready"]["significance_mode"], "padj")
            self.assertEqual(result["structured_data"]["input"]["reference_strata_ignored"], ["CD14"])

    def test_stratified_deseq2_annotation_group_endpoint_compares_annotated_lfcs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _AnnotationGroupFakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            sample_ids = [
                f"{stratum}_{sex}_{index}"
                for stratum in ("CD4", "CD8")
                for sex in ("M", "F")
                for index in (1, 2)
            ]
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\t" + "\t".join(sample_ids) + "\n"
                + "TP53\t" + "\t".join(["100"] * len(sample_ids)) + "\n"
                + "BRCA1\t" + "\t".join(["80"] * len(sample_ids)) + "\n"
                + "GAPDH\t" + "\t".join(["300"] * len(sample_ids)) + "\n"
                + "ACTB\t" + "\t".join(["240"] * len(sample_ids)) + "\n",
                encoding="utf-8",
            )
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tcelltype\tsex\n"
                + "\n".join(f"{sample}\t{sample.split('_')[0]}\t{sample.split('_')[1]}" for sample in sample_ids)
                + "\n",
                encoding="utf-8",
            )
            annotation_path = root / "annotation.tsv"
            annotation_path.write_text(
                "gene\tgene_biotype\n"
                "TP53\tprotein_coding\n"
                "BRCA1\tprotein_coding\n"
                "GAPDH\tlncRNA\n"
                "ACTB\tlncRNA\n",
                encoding="utf-8",
            )

            result = service.analyze_stratified_deseq2(
                counts_path=str(counts_path),
                sample_metadata_path=str(metadata_path),
                gene_column="gene",
                sample_key_column="sample",
                stratum_column="celltype",
                sex_column="sex",
                target_strata=["CD4", "CD8"],
                analysis="annotation_group_lfc_test",
                feature_annotation_path=str(annotation_path),
                feature_annotation_key_column="gene",
                feature_annotation_group_column="gene_biotype",
                feature_annotation_group_value="protein_coding",
                lfc_shrinkage="apeglm",
                test="welch",
            )

            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(result["question_ready"]["selected_lfc_count"], 2)
            self.assertEqual(result["question_ready"]["other_annotated_lfc_count"], 2)
            self.assertEqual(result["question_ready"]["test"], "welch")

    def test_stratified_deseq2_shares_the_evaluation_budget_across_strata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            sample_ids = [
                f"{stratum}_{sex}_{index}"
                for stratum in ("CD4", "CD8", "CD14")
                for sex in ("M", "F")
                for index in (1, 2)
            ]
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\t" + "\t".join(sample_ids) + "\n"
                + "TP53\t" + "\t".join(["100"] * len(sample_ids)) + "\n"
                + "BRCA1\t" + "\t".join(["80"] * len(sample_ids)) + "\n",
                encoding="utf-8",
            )
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text(
                "sample\tcelltype\tsex\n"
                + "\n".join(f"{sample}\t{sample.split('_')[0]}\t{sample.split('_')[1]}" for sample in sample_ids)
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"GLESKIT_EVAL_TOOL_TIMEOUT_SECONDS": "60"}):
                result = service.analyze_stratified_deseq2(
                    counts_path=str(counts_path),
                    sample_metadata_path=str(metadata_path),
                    gene_column="gene",
                    sample_key_column="sample",
                    stratum_column="celltype",
                    sex_column="sex",
                    target_strata=["CD4", "CD8"],
                    reference_strata=["CD14"],
                )

            timeouts = [call[1].get("timeout_seconds") for call in backend.calls]
            self.assertEqual(result["structured_data"]["input"]["execution_budget_seconds"], 60)
            self.assertEqual(len(timeouts), 3)
            self.assertTrue(all(isinstance(timeout, int) and timeout > 0 for timeout in timeouts))

    def test_pathway_species_detects_pa14_locus_tags_without_a_question_specific_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))

            self.assertEqual(
                service._pathway_species_for_genes(
                    ["PA14_33530", "PA14_33610"],
                    species="Pseudomonas aeruginosa",
                ),
                "pseudomonas_aeruginosa_ucbpp_pa14",
            )

    def test_eval_path_policy_rejects_external_and_symlinked_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            visible_root = root / "visible"
            visible_root.mkdir()
            allowed_path = visible_root / "counts.tsv"
            outside_path = root / "private.tsv"
            allowed_path.write_text("gene\ts1\nG1\t1\n", encoding="utf-8")
            outside_path.write_text("gene\ts1\nSECRET\t99\n", encoding="utf-8")
            symlink_path = visible_root / "link.tsv"
            try:
                symlink_path.symlink_to(outside_path)
            except OSError:
                symlink_path = None

            service = self.build_service(root)
            policy = {
                "GLESKIT_EVAL_PATH_POLICY": "1",
                "GLESKIT_EVAL_ALLOWED_INPUT_PATHS": str(allowed_path),
                "GLESKIT_EVAL_ALLOWED_INPUT_ROOTS": str(visible_root),
            }
            with patch.dict(os.environ, policy, clear=False):
                self.assertEqual(service.inspect_tabular_data(path=str(allowed_path))["provider"], "gleskit_inspection")
                with self.assertRaises(ProviderError):
                    service.inspect_tabular_data(path=str(outside_path))
                with self.assertRaises(ProviderError):
                    service.enrich_gene_set(genes_path=str(outside_path))
                with self.assertRaises(ProviderError):
                    service._load_cluster_labels_payload(cluster_labels_path=str(outside_path))
                with self.assertRaises(ProviderError):
                    service.inspect_tabular_data(path="/etc/passwd")
                with self.assertRaises(ProviderError):
                    service.inspect_tabular_data(path=str(visible_root / ".." / "private.tsv"))
                if symlink_path is not None:
                    with self.assertRaises(ProviderError):
                        service.inspect_tabular_data(path=str(symlink_path))

    def test_inspect_tabular_data_recognizes_excel_style_deg_workbook_as_result_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            workbook_path = root / "deg_by_cell_type.xlsx"

            import pandas as pd

            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    {
                        "Unnamed: 0": ["TP53", "BRCA1"],
                        "p_val": [0.0001, 0.003],
                        "avg_log2FC": [2.1, -1.5],
                        "pct.1": [0.8, 0.2],
                        "pct.2": [0.1, 0.7],
                        "p_val_adj": [0.001, 0.02],
                    }
                ).to_excel(writer, sheet_name="CD14 Mono", index=False)
                pd.DataFrame(
                    {
                        "Unnamed: 0": ["LYZ"],
                        "p_val": [0.01],
                        "avg_log2FC": [1.2],
                        "pct.1": [0.4],
                        "pct.2": [0.2],
                        "p_val_adj": [0.03],
                    }
                ).to_excel(writer, sheet_name="NK", index=False)

            result = service.inspect_tabular_data(path=str(workbook_path))

            structured = result["structured_data"]
            self.assertEqual(structured["detected_data_type"], "deseq2_results")
            self.assertIn("analyze_rnaseq_result", structured["candidate_analysis_families"])
            self.assertTrue(all(row["detected_data_type"] == "deseq2_results" for row in structured["sheet_roles"]))

    def test_analyze_single_cell_rnaseq_bubbles_top_cell_type_question_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            workbook_path = root / "deg_by_cell_type.xlsx"

            import pandas as pd

            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame(
                    {
                        "Unnamed: 0": ["TP53", "BRCA1"],
                        "p_val": [0.0001, 0.003],
                        "avg_log2FC": [2.1, -1.5],
                        "pct.1": [0.8, 0.2],
                        "pct.2": [0.1, 0.7],
                        "p_val_adj": [0.001, 0.02],
                    }
                ).to_excel(writer, sheet_name="CD14 Mono", index=False)
                pd.DataFrame(
                    {
                        "Unnamed: 0": ["LYZ"],
                        "p_val": [0.01],
                        "avg_log2FC": [1.2],
                        "pct.1": [0.4],
                        "pct.2": [0.2],
                        "p_val_adj": [0.03],
                    }
                ).to_excel(writer, sheet_name="NK", index=False)

            result = service.analyze_single_cell_rnaseq(
                analysis="cell_type_deg_counts",
                deg_table_path=str(workbook_path),
            )

            self.assertEqual(result["question_ready"]["top_cell_type"], "CD14 Mono")
            self.assertEqual(result["question_ready"]["top_significant_gene_count"], 2)
            self.assertEqual(result["structured_data"]["question_ready"]["ranked_cell_type_count"], 2)
            self.assertIn("Top cell type: CD14 Mono (2 significant genes).", result["summary"])

    def test_prepare_rnaseq_inputs_writes_manifest_and_sanitized_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            counts_path = root / "counts.tsv"
            metadata_path = root / "metadata.tsv"
            counts_path.write_text(
                "gene\ts1\ts2\ts3\n"
                "G1\t10.2\t20.8\t30.0\n"
                "G2\t5.0\t7.0\t8.0\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tcondition\tkeep\n"
                "s1\tA\tyes\n"
                "s2\tB\tyes\n"
                "s3\tB\tno\n",
                encoding="utf-8",
            )

            result = service.prepare_rnaseq_inputs(
                counts_path=str(counts_path),
                metadata_path=str(metadata_path),
                gene_column="gene",
                sample_key_column="sample",
                condition_column="condition",
                sample_filter_column="keep",
                sample_filter_values=["yes"],
                count_transform="round_int",
                label="prep test",
            )

            structured = result["structured_data"]
            manifest_path = Path(structured["prepared_manifest_path"])
            sanitized_counts_path = Path(structured["sanitized_counts_path"])
            sanitized_metadata_path = Path(structured["sanitized_metadata_path"])
            self.assertTrue(manifest_path.exists())
            self.assertTrue(sanitized_counts_path.exists())
            self.assertTrue(sanitized_metadata_path.exists())
            self.assertEqual(structured["sample_count"], 2)
            self.assertEqual(structured["feature_count"], 2)
            self.assertEqual(structured["resolved_columns"]["sample_key_column"], "sample")
            self.assertEqual(structured["applied_transform"]["count_transform"], "round_int")
            self.assertEqual(structured["applied_filters"]["sample_filter_column"], "keep")

    def test_analyze_deseq2_counts_accepts_prepared_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            counts_path = root / "counts.tsv"
            metadata_path = root / "metadata.tsv"
            counts_path.write_text(
                "gene\ts1\ts2\n"
                "G1\t10\t20\n"
                "G2\t5\t7\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tcondition\n"
                "s1\tA\n"
                "s2\tB\n",
                encoding="utf-8",
            )

            prepared = service.prepare_rnaseq_inputs(
                counts_path=str(counts_path),
                metadata_path=str(metadata_path),
                gene_column="gene",
                sample_key_column="sample",
                condition_column="condition",
                count_transform="none",
            )
            manifest_path = prepared["structured_data"]["prepared_manifest_path"]
            result = service.analyze_deseq2_counts(
                prepared_manifest_path=manifest_path,
                comparison="B vs A",
                label="Prepared DESeq2",
            )

            call_name, call_payload = backend.calls[-1]
            self.assertEqual(call_name, "analyze_counts")
            self.assertTrue(str(call_payload["counts_path"]).endswith("sanitized_counts.tsv"))
            self.assertTrue(str(call_payload["sample_metadata_path"]).endswith("sanitized_metadata.tsv"))
            self.assertEqual(call_payload["sample_column"], "sample")
            self.assertEqual(call_payload["condition_column"], "condition")
            self.assertEqual(result["structured_data"]["analysis_route"], "prepared_manifest_deseq2")
            self.assertEqual(
                result["structured_data"]["preparation_summary"]["prepared_manifest_path"],
                manifest_path,
            )
            self.assertIn("tested_gene_count", result["structured_data"]["question_ready_counts"])

    def test_prepared_manifest_infers_design_from_explicit_contrast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = _FakeDESeq2Backend()
            service = self.build_service(root, deseq2_backend=backend)
            counts_path = root / "counts.tsv"
            metadata_path = root / "metadata.tsv"
            counts_path.write_text(
                "gene\tbaseline_1\tfinal_1\nG1\t10\t20\nG2\t30\t40\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tTissue\nbaseline_1\tbaseline_blood\nfinal_1\tfinal_blood\n",
                encoding="utf-8",
            )

            prepared = service.prepare_rnaseq_inputs(
                counts_path=str(counts_path),
                metadata_path=str(metadata_path),
                gene_column="gene",
                sample_key_column="sample",
            )
            manifest_path = prepared["structured_data"]["prepared_manifest_path"]
            self.assertEqual(prepared["structured_data"]["recommended_design_formula"], "~ 1")

            result = service.analyze_deseq2_counts(
                prepared_manifest_path=manifest_path,
                condition_column="Tissue",
                condition_a="final_blood",
                condition_b="baseline_blood",
            )

            self.assertEqual(backend.calls[-1][1]["design_formula"], "~ Tissue")
            self.assertEqual(
                result["structured_data"]["preparation_summary"]["auto_selected_design_formula"],
                "~ Tissue",
            )

    def test_run_expression_statistics_adds_route_warning_for_deg_table_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(
                root,
                expression_matrix_backend=ExpressionMatrixAnalysisBackend(),
                expression_statistics_backend=ExpressionStatisticsBackend(),
            )
            result_path = root / "deg.tsv"
            result_path.write_text(
                "gene\tlog2FoldChange\tpadj\tpvalue\tbaseMean\n"
                "TP53\t2.1\t0.01\t0.001\t100\n"
                "BRCA1\t-1.2\t0.02\t0.003\t80\n",
                encoding="utf-8",
            )

            result = service.run_expression_statistics(
                analysis="distribution_summary",
                source_path=str(result_path),
                value_column="log2FoldChange",
                label="Route warning",
            )

            self.assertTrue(result["warnings"])
            self.assertIn("DEG/result table", result["warnings"][0])
            self.assertIn("route_warnings", result["structured_data"])

    def test_path_aware_tools_reject_upload_selector_without_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))

            with self.assertRaises(ValueError) as context:
                service.run_expression_pca(
                    matrix_path="upload_missing",
                    row_id_column="gene",
                )

            self.assertIn("looks like a GLESKit upload/artifact id", str(context.exception))
            self.assertIn("workspace is unavailable", str(context.exception))

    def test_uploads_use_public_upload_id_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Upload contract",
                description="Uploads should expose upload_id to callers.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            source_file = root / "counts.tsv"
            source_file.write_text("gene\tcount\nTP53\t10\n")

            upload = service.create_session_upload(
                created["session_id"],
                data_path=str(source_file),
                source_mode="linked",
                file_name="counts.tsv",
                source_path=str(source_file),
            )
            uploads = service.list_uploads(created["session_id"])
            fetched = service.get_upload(created["session_id"], upload["upload_id"])

            self.assertIn("upload_id", upload)
            self.assertNotIn("artifact_id", upload)
            self.assertEqual(upload["upload_id"], uploads["uploads"][0]["upload_id"])
            self.assertEqual(fetched["upload_id"], upload["upload_id"])
            self.assertNotIn("artifact_id", fetched)
            self.assertEqual(upload["summary_block"]["data_type"], "raw_counts")
            self.assertEqual(upload["summary_block"]["sample_count"], 1)
            self.assertEqual(upload["technical"]["path"], str(source_file.resolve()))
            self.assertEqual(fetched["summary_block"]["data_type"], "raw_counts")
            self.assertEqual(fetched["summary_block"]["sample_count"], 1)

    def test_legacy_upload_payload_is_hydrated_from_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Legacy upload hydration",
                description="Uploads created before structured summaries should still inspect cleanly.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t7\n")
            _, workspace = service.workspace_gateway.require_workspace()
            legacy = workspace.capture_artifact(
                created["session_id"],
                kind="upload",
                title="Legacy counts",
                summary="Legacy counts upload",
                provider="local_upload",
                payload={
                    "data_path": str(counts_path),
                    "source_mode": "linked",
                    "file_name": "counts.tsv",
                    "mime_type": "text/tab-separated-values",
                },
                provenance={"cohort_role": "counts"},
            )

            uploads = service.list_uploads(created["session_id"])
            fetched = service.get_upload(created["session_id"], legacy.artifact_id)

            self.assertEqual(uploads["upload_count"], 1)
            self.assertEqual(uploads["uploads"][0]["summary_block"]["data_type"], "raw_counts")
            self.assertEqual(uploads["uploads"][0]["summary_block"]["sample_count"], 2)
            self.assertEqual(fetched["summary_block"]["data_type"], "raw_counts")
            self.assertEqual(fetched["summary_block"]["sample_count"], 2)
            self.assertEqual(Path(fetched["technical"]["path"]).resolve(), counts_path.resolve())

    def test_upload_previews_respect_configured_line_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "gleskit.defaults.json").write_text(
                json.dumps(
                    {
                        "runtime": {
                            "upload_preview_max_lines": 2,
                            "artifact_preview_max_lines": 4,
                        }
                    }
                ),
                encoding="utf-8",
            )
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Upload preview limit",
                description="Upload previews should be bounded by the upload-specific limit.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            source_file = root / "long_counts.tsv"
            source_file.write_text(
                "\n".join(
                    [
                        "gene\tcount",
                        "TP53\t" + ("A" * 3000),
                        "BRCA1\t20",
                        "EGFR\t30",
                    ]
                ),
                encoding="utf-8",
            )

            upload = service.create_session_upload(
                created["session_id"],
                data_path=str(source_file),
                source_mode="linked",
                file_name="long_counts.tsv",
                source_path=str(source_file),
            )
            fetched = service.get_upload(created["session_id"], upload["upload_id"])

            self.assertEqual(len(upload["payload"]["preview_lines"]), 2)
            self.assertEqual(len(fetched["payload"]["preview_lines"]), 2)
            self.assertLessEqual(len(upload["payload"]["preview_lines"][1]), 1000)
            self.assertLessEqual(len(fetched["payload"]["preview_lines"][1]), 1000)

    def test_create_cohort_and_create_session_from_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t5\t7\n")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\ns1\tA\ns2\tB\n")

            cohort = service.create_cohort(
                title="Service cohort",
                counts_path=str(counts_path),
                metadata_path=str(metadata_path),
                provider_id="tcga",
                source_kind="downloaded",
                species="human",
            )
            loaded_cohort = service.get_cohort(cohort["cohort_id"])
            session = service.create_session_from_cohort(
                cohort_id=cohort["cohort_id"],
                title="Cohort-backed session",
                description="The session should link cohort inputs.",
                species="human",
            )

            uploads = service.list_uploads(session["session_id"])
            self.assertTrue(cohort["workspace"]["available"])
            self.assertEqual(cohort["profile"]["recommended_group_column"], "condition")
            self.assertEqual(cohort["inspection"]["counts"]["data_type"], "raw_counts")
            self.assertEqual(cohort["inspection"]["metadata"]["data_type"], "metadata")
            self.assertEqual(loaded_cohort["summary_block"]["sample_count"], 2)
            self.assertEqual(loaded_cohort["summary_block"]["raw_gene_count"], 2)
            self.assertEqual(loaded_cohort["summary_block"]["filtered_gene_count"], 1)
            self.assertEqual(loaded_cohort["technical"]["counts_path"], cohort["counts_path"])
            self.assertEqual(session["cohort_id"], cohort["cohort_id"])
            self.assertEqual(uploads["upload_count"], 2)
            self.assertEqual(session["linked_upload_ids"], [item["upload_id"] for item in uploads["uploads"]])
            self.assertTrue(all(item["summary_block"]["data_type"] in {"raw_counts", "metadata"} for item in uploads["uploads"]))

    def test_get_cohort_summary_payload_exposes_raw_and_filtered_gene_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            counts_path = root / "counts.tsv"
            counts_path.write_text("gene\ts1\ts2\nTP53\t10\t20\nBRCA1\t0\t0\n")
            metadata_path = root / "metadata.tsv"
            metadata_path.write_text("sample\tcondition\ns1\tA\ns2\tB\n")

            created = service.create_cohort(
                title="Summary payload cohort",
                counts_path=str(counts_path),
                metadata_path=str(metadata_path),
                provider_id="local_import",
                source_kind="imported",
                species="human",
            )
            payload = service.get_cohort(created["cohort_id"])

            self.assertEqual(payload["summary_block"]["sample_count"], 2)
            self.assertEqual(payload["summary_block"]["raw_gene_count"], 2)
            self.assertEqual(payload["summary_block"]["filtered_gene_count"], 1)
            self.assertEqual(payload["gene_count"], 1)
            self.assertEqual(payload["technical"]["counts_path"], created["counts_path"])

    def test_resolve_gene_set_capture_saves_gene_set_resolution_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)

            created = service.create_session(
                title="Gene resolution audit",
                description="Saved batch normalization workspace.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            result = service.resolve_gene_set(genes=["TP53", "CLOCK"])

            self.assertTrue(result["workspace"]["available"])
            self.assertTrue(result["workspace"]["saved"])
            artifacts = service.list_artifacts(created["session_id"])
            self.assertEqual(artifacts["artifact_count"], 1)
            self.assertEqual(artifacts["artifacts"][0]["kind"], "gene_set_resolution")

    def test_gene_summary_combines_mygene_ensembl_and_uniprot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.gene_summary(query="TP53")

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["gene"]["symbol"], "TP53")
            self.assertEqual(result["structured_data"]["ensembl"]["display_name"], "TP53")
            self.assertEqual(result["structured_data"]["protein"]["accession"], "P_TP53")
            self.assertEqual(result["structured_data"]["go_annotations"]["terms"][0]["go_id"], "GO:0006977")

    def test_protein_summary_adds_linked_gene_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.protein_summary(query="TP53")

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["protein"]["gene_primary"], "TP53")
            self.assertEqual(result["structured_data"]["linked_gene"]["symbol"], "TP53")
            self.assertEqual(result["structured_data"]["go_annotations"]["terms"][0]["go_name"], "DNA damage response")
            self.assertEqual(result["structured_data"]["drug_target_context"]["drug_count"], 1)

    def test_drug_target_context_returns_chembl_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.drug_target_context(gene="TP53")

            self.assertEqual(result["provider"], "chembl")
            self.assertEqual(result["structured_data"]["target"]["target_chembl_id"], "CHEMBL123")
            self.assertEqual(result["structured_data"]["drugs"][0]["molecule_name"], "Nutlin-3")

    def test_variant_trait_associations_returns_gwas_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.variant_trait_associations(gene="TP53")

            self.assertEqual(result["provider"], "gwas_catalog")
            self.assertEqual(result["structured_data"]["associations"][0]["variant_id"], "rs78378222-T")
            self.assertEqual(result["structured_data"]["normalized_gene"]["symbol"], "TP53")

    def test_interaction_network_returns_string_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.interaction_network(genes=["TP53", "MDM2"])

            self.assertEqual(result["provider"], "string")
            self.assertEqual(result["structured_data"]["edge_count"], 2)
            self.assertEqual(result["structured_data"]["edges"][0]["protein_b"], "MDM2")

    def test_pathway_context_returns_reactome_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.pathway_context(genes=["TP53", "CDK2"])

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["pathways"][0]["pathway_id"], "R-HSA-1")
            self.assertEqual(result["structured_data"]["pathways"][0]["display_name"], "Cell Cycle Checkpoints")

    def test_expression_context_combines_gtex_and_expression_atlas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.expression_context(gene="TP53")

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["normalized_gene"]["symbol"], "TP53")
            self.assertEqual(result["structured_data"]["gtex"]["reference_gene"]["gencode_id"], "ENSG00000141510.16")
            self.assertEqual(result["structured_data"]["expression_atlas"]["reference_experiment"]["accession"], "E-MTAB-2836")
            self.assertEqual(result["structured_data"]["expression_atlas"]["query_gene_profile"]["gene_name"], "TP53")

    def test_analyze_rnaseq_result_builds_multisource_interpretation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            result_path = root / "de_results.tsv"
            result_path.write_text(
                "\n".join(
                    [
                        "gene_symbol\tlog2FoldChange\tpadj\tpvalue\tbaseMean",
                        "TP53\t2.1\t0.001\t0.0001\t100",
                        "BRCA1\t1.4\t0.02\t0.001\t80",
                        "EGFR\t-1.8\t0.01\t0.002\t120",
                        "CLOCK\t-2.3\t0.03\t0.004\t60",
                    ]
                )
            )

            result = service.analyze_rnaseq_result(
                result_path=str(result_path),
                comparison="tumor vs control",
                label="Tumor signature",
            )

            self.assertEqual(result["provider"], "gleskit_aggregate")
            self.assertEqual(result["structured_data"]["summary_stats"]["significant_gene_count"], 4)
            self.assertEqual(result["structured_data"]["resolved_gene_sets"]["upregulated"]["resolved_genes"][:2], ["TP53", "BRCA1"])
            self.assertEqual(result["structured_data"]["resolved_gene_sets"]["upregulated"]["resolution_scope"], "lead_subset")
            self.assertEqual(result["structured_data"]["enrichment"]["upregulated"]["term_count"], 2)
            self.assertEqual(result["structured_data"]["ranked_gsea"]["term_count"], 2)
            self.assertEqual(result["structured_data"]["gene_score_count"], 4)
            self.assertEqual(len(result["structured_data"]["ranked_gene_scores"]), 4)
            self.assertEqual(result["structured_data"]["ranked_gsea"]["gene_score_count"], 4)
            self.assertEqual(len(result["structured_data"]["ranked_gsea"]["ranked_gene_scores"]), 4)
            self.assertEqual(result["structured_data"]["disease_enrichment"]["term_count"], 2)
            self.assertEqual(result["structured_data"]["papers"]["paper_count"], 1)
            self.assertIn("evidence_bundle", result["structured_data"])
            self.assertIn("derived_inputs", result)
            self.assertTrue(Path(result["derived_inputs"]["all_significant_genes_path"]).exists())
            self.assertTrue(Path(result["derived_inputs"]["ranked_gene_scores_path"]).exists())

    def test_analyze_rnaseq_result_compacts_agent_payload_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            result_path = root / "de_results.tsv"
            result_path.write_text(
                "\n".join(
                    [
                        "gene_symbol\tlog2FoldChange\tpadj\tpvalue\tbaseMean",
                        "TP53\t2.1\t0.001\t0.0001\t100",
                        "BRCA1\t1.9\t0.002\t0.0002\t80",
                        "EGFR\t-1.8\t0.01\t0.002\t120",
                        "CLOCK\t-2.3\t0.03\t0.004\t60",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"GLESKIT_COMPACT_TOOL_PAYLOADS": "1"}):
                result = service.analyze_rnaseq_result(
                    result_path=str(result_path),
                    comparison="tumor vs control",
                    label="Tumor signature",
                )

            structured = result["structured_data"]
            self.assertEqual(structured["agent_payload_mode"], "compact")
            self.assertIn("derived_inputs", structured)
            self.assertIn("significant_gene_set_preview", structured)
            self.assertNotIn("significant_gene_sets", structured)
            self.assertNotIn("ranked_gene_scores", structured)
            self.assertEqual(structured["ranked_gsea"]["gene_score_count"], 4)
            self.assertTrue(Path(structured["result_table_path"]).exists())
            self.assertTrue(Path(structured["all_significant_genes_path"]).exists())

    def test_analyze_rnaseq_result_persists_ranked_signature_when_ranked_gsea_fails(self) -> None:
        class _RankedGSEAFailingYuLabBackend(_FakeYuLabBackend):
            def ranked_pathway_gsea(
                self,
                *,
                ranked_gene_scores: list[dict[str, object]],
                sources: list[str] | None = None,
                limit: int = 15,
            ) -> ProviderResponse:
                raise ProviderError("Local ranked GSEA backend failed.")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root, yulab_backend=_RankedGSEAFailingYuLabBackend())
            result_path = root / "de_results.tsv"
            result_path.write_text(
                "\n".join(
                    [
                        "gene_symbol\tlog2FoldChange\tpadj\tpvalue\tbaseMean",
                        "TP53\t2.1\t0.001\t0.0001\t100",
                        "BRCA1\t1.4\t0.02\t0.001\t80",
                        "EGFR\t-1.8\t0.01\t0.002\t120",
                        "CLOCK\t-2.3\t0.03\t0.004\t60",
                    ]
                )
            )

            result = service.analyze_rnaseq_result(
                result_path=str(result_path),
                comparison="tumor vs control",
                label="Tumor signature",
            )

            self.assertEqual(result["structured_data"]["ranked_gsea"]["term_count"], 0)
            self.assertEqual(result["structured_data"]["gene_score_count"], 4)
            self.assertEqual(len(result["structured_data"]["ranked_gene_scores"]), 4)
            self.assertEqual(result["structured_data"]["ranked_gene_scores"][0]["gene"], "TP53")
            self.assertEqual(result["structured_data"]["ranked_gsea"]["gene_score_count"], 4)
            self.assertEqual(len(result["structured_data"]["ranked_gsea"]["ranked_gene_scores"]), 4)
            self.assertTrue(any("Local ranked GSEA backend failed." in warning for warning in result["warnings"]))

    def test_analyze_rnaseq_result_keeps_enrichment_when_mygene_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(root)
            service = GLESKitService(
                settings=settings,
                mygene=_FailingMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                expression_atlas=_FakeExpressionAtlasProvider(),
                gtex=_FakeGTExProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FakeOpenTargetsProvider(),
                workspace_gateway=WorkspaceGateway(settings),
            )
            result_path = root / "de_results.tsv"
            result_path.write_text(
                "\n".join(
                    [
                        "gene_symbol\tlog2FoldChange\tpadj\tpvalue\tbaseMean",
                        "TP53\t2.1\t0.001\t0.0001\t100",
                        "BRCA1\t1.4\t0.02\t0.001\t80",
                        "EGFR\t-1.8\t0.01\t0.002\t120",
                        "CLOCK\t-2.3\t0.03\t0.004\t60",
                    ]
                )
            )

            result = service.analyze_rnaseq_result(
                result_path=str(result_path),
                comparison="tumor vs control",
                label="Tumor signature",
            )

            self.assertEqual(result["structured_data"]["enrichment"]["upregulated"]["genes"], ["TP53", "BRCA1"])
            self.assertEqual(result["structured_data"]["enrichment"]["upregulated"]["term_count"], 2)
            self.assertEqual(result["structured_data"]["resolved_gene_sets"]["upregulated"]["resolved_genes"], [])
            self.assertTrue(any("MyGene network error" in warning for warning in result["warnings"]))

    def test_enrich_gene_set_adds_reactome_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.enrich_gene_set(genes=["TP53", "CDK2"])

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["analysis_mode"], "ora")
            self.assertEqual(result["structured_data"]["backend"], "clusterProfiler")
            self.assertEqual(result["structured_data"]["species"], "human")
            self.assertEqual(result["structured_data"]["query_size"], 2)
            self.assertGreaterEqual(result["structured_data"]["background_size"], 0)
            self.assertEqual(result["structured_data"]["terms"][0]["source"], "GO:BP")
            self.assertIn("query_size", result["structured_data"]["terms"][0])
            self.assertIn("intersection_size", result["structured_data"]["terms"][0])
            self.assertEqual(result["structured_data"]["reactome_context"][0]["pathway_id"], "R-HSA-69620")

    def test_enrich_gene_set_accepts_gene_list_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            genes_path = root / "genes.tsv"
            genes_path.write_text("gene\nTP53\nCDK2\n", encoding="utf-8")

            result = service.enrich_gene_set(genes_path=str(genes_path))

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["input_gene_count"], 2)
            self.assertEqual(result["structured_data"]["terms"][0]["matched_genes"], ["TP53", "CDK2"])
            self.assertEqual(result["structured_data"]["top_terms"][0]["term_name"], "DNA repair")
            self.assertIn("Top enriched terms", result["summary"])

    def test_enrich_gene_set_returns_bounded_focus_term_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.enrich_gene_set(
                genes=["TP53", "CDK2"],
                focus_terms=["GO:0050863"],
            )

            structured = result["structured_data"]
            self.assertEqual(structured["focus_terms_requested"], ["GO:0050863"])
            self.assertEqual(structured["focus_terms_found"], ["GO:0050863"])
            self.assertEqual(structured["focused_terms"][0]["term_name"], "regulation of T cell activation")
            self.assertEqual(structured["focused_terms"][0]["adjusted_p_value"], 0.0002)

    def test_enrich_gene_set_compacts_focus_miss_for_model(self) -> None:
        class _NoFocusYuLabBackend(_FakeYuLabBackend):
            def pathway_enrichment(self, **kwargs) -> ProviderResponse:
                result = super().pathway_enrichment(**kwargs)
                payload = dict(result.payload)
                payload["focus_terms_found"] = []
                payload["focused_terms"] = []
                return ProviderResponse(provider=result.provider, summary=result.summary, payload=payload)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"GLESKIT_COMPACT_TOOL_PAYLOADS": "1"},
        ):
            service = self.build_service(Path(tmp), yulab_backend=_NoFocusYuLabBackend())
            result = service.enrich_gene_set(
                genes=["TP53", "CDK2"],
                focus_terms=["neutrophil activation"],
            )

            structured = result["structured_data"]
            self.assertEqual(structured["focus_status"], "not_found")
            self.assertEqual(structured["focus_finalization"]["recommended_action"], "finalize")
            self.assertEqual(structured["focus_terms_requested"], ["neutrophil activation"])
            self.assertEqual(structured["focused_terms"], [])
            self.assertEqual(structured["top_term"]["term_name"], "DNA repair")
            self.assertNotIn("terms", structured)
            self.assertNotIn("genes", structured)

    def test_enrich_gene_set_accepts_ensembl_ids_and_normalizes_internally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self.build_reference_catalog(root)
            service = self.build_service(root, reference_catalog=catalog)

            result = service.enrich_gene_set(genes=["ENSG_BGN", "ENSG_TP53"])

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["input_genes"], ["ENSG_BGN", "ENSG_TP53"])
            self.assertEqual(result["structured_data"]["genes"], ["BGN", "TP53"])
            self.assertTrue(result["structured_data"]["normalized_gene_set"]["resolution_used"])
            self.assertEqual(result["structured_data"]["normalized_gene_set"]["analysis_genes"], ["BGN", "TP53"])

    def test_enrich_gene_set_resolves_homo_sapiens_ensembl_ids_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self.build_reference_catalog(root)
            service = self.build_service(root, reference_catalog=catalog)
            service.mygene = _FailingMyGeneProvider()

            result = service.enrich_gene_set(
                genes=["ENSG_BGN", "ENSG_TP53"],
                species="Homo sapiens",
            )

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["genes"], ["BGN", "TP53"])
            self.assertTrue(result["structured_data"]["normalized_gene_set"]["resolution_used"])
            self.assertEqual(result["structured_data"]["normalized_gene_set"]["analysis_genes"], ["BGN", "TP53"])
            self.assertEqual(
                result["structured_data"]["normalized_gene_set"]["provider_usage"],
                {"local_reference": 2},
            )

    def test_enrich_gene_set_strips_ensembl_versions_before_local_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root, reference_catalog=self.build_reference_catalog(root))
            result = service.enrich_gene_set(genes=["ENSG_BGN.14", "ENSG_TP53.2"], species="human")

            self.assertEqual(result["structured_data"]["genes"], ["BGN", "TP53"])
            self.assertEqual(result["structured_data"]["normalized_gene_set"]["input_genes"], ["ENSG_BGN", "ENSG_TP53"])

    def test_enrich_gene_set_passes_background_gene_path_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            background_path = root / "tested_genes.tsv"
            background_path.write_text("gene\nTP53\nCDK2\nBRCA1\n", encoding="utf-8")

            result = service.enrich_gene_set(
                genes=["TP53", "CDK2"],
                background_genes_path=str(background_path),
            )

            self.assertEqual(result["structured_data"]["background_genes"], ["TP53", "CDK2", "BRCA1"])
            self.assertEqual(result["structured_data"]["background_resolution"]["analysis_gene_count"], 3)
            self.assertEqual(result["structured_data"]["background_genes_path"], str(background_path.resolve()))

    def test_enrich_gene_set_rejects_zero_identifier_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            empty_resolution = ProviderResponse(
                provider="ensembl",
                summary="No matches",
                payload={"matches": []},
            )
            with patch.object(service, "_resolve_gene_provider_result", return_value=empty_resolution):
                with self.assertRaisesRegex(ProviderError, "could not map any input identifiers"):
                    service.enrich_gene_set(genes=["ENSG000001.14", "ENSG000002.3"])

    def test_enrich_gene_set_falls_back_to_gprofiler_when_local_backend_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp), yulab_backend=_FailingYuLabBackend())
            result = service.enrich_gene_set(genes=["TP53", "CDK2"])

            self.assertEqual(result["provider"], "gprofiler")
            self.assertEqual(result["structured_data"]["backend"], "gprofiler")
            self.assertEqual(result["structured_data"]["terms"][0]["term_name"], "DNA repair")
            self.assertIn("local_backend_warning", result["structured_data"])

    def test_enrich_gene_set_explicit_clusterprofiler_never_silently_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp), yulab_backend=_FailingYuLabBackend())

            with self.assertRaisesRegex(ProviderError, "requested clusterProfiler enrichment failed"):
                service.enrich_gene_set(
                    genes=["TP53", "CDK2"],
                    database="GO_BP",
                    backend="clusterprofiler",
                )

    def test_enrich_gene_set_retries_transient_local_provider_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = _TransientYuLabBackend()
            service = self.build_service(Path(tmp), yulab_backend=backend)

            result = service.enrich_gene_set(genes=["TP53", "CDK2"], database="GO_BP")

            self.assertEqual(backend.calls, 2)
            self.assertEqual(result["provider"], "yulab_clusterprofiler")

    def test_yulab_provider_timeout_has_structured_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp), yulab_backend=_TimeoutYuLabBackend())

            with self.assertRaisesRegex(ProviderError, r"error_code=provider_timeout"):
                service._call_yulab_backend("pathway_enrichment", genes=["TP53", "CDK2"])

    def test_gene_set_disease_context_adds_term_level_disease_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.gene_set_disease_context(genes=["TP53", "BRCA1"], label="DNA damage set")

            self.assertEqual(result["provider"], "yulab_dose")
            self.assertEqual(result["structured_data"]["database"], "DO")
            self.assertEqual(result["structured_data"]["term_count"], 2)
            self.assertEqual(result["structured_data"]["terms"][0]["term_name"], "cancer")
            self.assertEqual(result["structured_data"]["terms"][0]["matched_genes"], ["TP53", "BRCA1"])
            self.assertEqual(result["structured_data"]["normalized_gene_set"]["resolved_with_entrez_count"], 2)

    def test_compare_gene_programs_returns_grouped_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.compare_gene_programs(
                programs={
                    "Clock core": ["CLOCK", "ARNTL", "PER1"],
                    "Lipid metabolism": ["PPARA", "PPARG", "FASN"],
                },
                label="Clock vs lipid",
            )

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["backend"], "clusterProfiler")
            self.assertEqual(result["structured_data"]["species"], "human")
            self.assertEqual(result["structured_data"]["program_count"], 2)
            self.assertEqual(len(result["structured_data"]["comparison_terms"]), 2)
            self.assertEqual(result["structured_data"]["comparison_terms"][0]["program_label"], "Clock core")

    def test_compare_gene_programs_falls_back_to_gprofiler_for_nonhuman_kegg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.compare_gene_programs(
                programs={
                    "Medium A": ["gene1", "gene2", "gene3"],
                    "Medium B": ["gene4", "gene5", "gene6"],
                },
                species="mouse",
                database="KEGG",
                label="Shared KEGG pathways",
            )

            self.assertEqual(result["provider"], "gprofiler")
            self.assertEqual(result["structured_data"]["backend"], "gprofiler")
            self.assertEqual(result["structured_data"]["database"], "KEGG")
            self.assertEqual(result["structured_data"]["program_count"], 2)
            self.assertGreaterEqual(len(result["structured_data"]["shared_terms"]), 1)
            self.assertEqual(
                result["structured_data"]["shared_terms"][0]["program_labels"],
                ["Medium A", "Medium B"],
            )

    def test_ranked_gene_set_gsea_returns_local_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.ranked_gene_set_gsea(
                gene_scores={
                    "CLOCK": 2.5,
                    "ARNTL": 2.2,
                    "PER1": 1.9,
                    "CRY1": 1.7,
                    "CCND1": -1.6,
                    "MYC": -1.8,
                },
                label="Clock ranked signature",
            )

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["analysis_mode"], "gsea")
            self.assertEqual(result["structured_data"]["backend"], "clusterProfiler")
            self.assertEqual(result["structured_data"]["species"], "human")
            self.assertEqual(result["structured_data"]["query_size"], 6)
            self.assertEqual(result["structured_data"]["background_size"], 6)
            self.assertEqual(result["structured_data"]["term_count"], 2)
            self.assertEqual(result["structured_data"]["positive_terms"][0]["term_name"], "circadian rhythm")
            self.assertEqual(result["structured_data"]["gene_score_count"], 6)
            self.assertIn("term_size", result["structured_data"]["positive_terms"][0])
            self.assertIn("precision", result["structured_data"]["positive_terms"][0])

    def test_ranked_gene_set_gsea_accepts_gene_score_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            gene_scores_path = root / "scores.tsv"
            gene_scores_path.write_text(
                "gene\tscore\nCLOCK\t2.5\nARNTL\t2.2\nPER1\t1.9\nMYC\t-1.8\n",
                encoding="utf-8",
            )

            result = service.ranked_gene_set_gsea(gene_scores_path=str(gene_scores_path))

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["analysis_mode"], "gsea")
            self.assertEqual(result["structured_data"]["gene_score_count"], 4)
            self.assertEqual(result["structured_data"]["positive_terms"][0]["term_name"], "circadian rhythm")
            self.assertEqual(result["structured_data"]["top_terms"][0]["term_name"], "circadian rhythm")
            self.assertIn("Top enriched terms", result["summary"])

    def test_run_expression_statistics_compacts_correlation_payload_and_applies_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(
                root,
                expression_matrix_backend=ExpressionMatrixAnalysisBackend(),
                expression_statistics_backend=ExpressionStatisticsBackend(),
            )
            matrix_path = root / "matrix.tsv"
            annotation_path = root / "annotation.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\n"
                "G1\t10\t10\n"
                "G2\t20\t20\n"
                "G3\t30\t30\n"
                "G4\t999\t999\n",
                encoding="utf-8",
            )
            annotation_path.write_text(
                "gene\tlength\tbiotype\n"
                "G1\t10\tprotein_coding\n"
                "G2\t20\tprotein_coding\n"
                "G3\t30\tprotein_coding\n"
                "G4\t40\tlncRNA\n",
                encoding="utf-8",
            )

            result = service.run_expression_statistics(
                analysis="matrix_annotation_correlation",
                source_path=str(matrix_path),
                row_id_column="gene",
                analysis_axis="columns",
                target_axis="features",
                summary_stat="mean",
                annotation_path=str(annotation_path),
                annotation_key_column="gene",
                annotation_value_column="length",
                annotation_group_column="biotype",
                annotation_group_value="protein_coding",
                method="pearson",
                label="Protein-coding correlation",
            )

            structured = result["structured_data"]
            self.assertEqual(structured["analysis"], "correlation")
            self.assertNotIn("aligned_rows", structured)
            self.assertEqual(structured["aligned_rows_count"], 3)
            self.assertEqual(structured["result"]["label_count"], 3)
            self.assertEqual(structured["correlation_summary"]["annotation_group_value"], "protein_coding")
            self.assertIn("correlation=", result["summary"])

    def test_run_expression_statistics_compacts_matrix_featurewise_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(
                root,
                expression_matrix_backend=ExpressionMatrixAnalysisBackend(),
                expression_statistics_backend=ExpressionStatisticsBackend(),
            )
            matrix_path = root / "matrix.tsv"
            metadata_path = root / "metadata.tsv"
            matrix_path.write_text(
                "gene\ts1\ts2\ts3\ts4\n"
                "G1\t1\t1\t10\t10\n"
                "G2\t2\t2\t20\t20\n"
                "G3\t5\t5\t5\t5\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                "sample\tgroup\n"
                "s1\tA\n"
                "s2\tA\n"
                "s3\tB\n"
                "s4\tB\n",
                encoding="utf-8",
            )

            result = service.run_expression_statistics(
                analysis="matrix_featurewise_group_test",
                source_path=str(matrix_path),
                row_id_column="gene",
                metadata_path=str(metadata_path),
                metadata_key_column="sample",
                metadata_group_column="group",
                group_a="A",
                group_b="B",
                test="welch",
                alpha=0.1,
                label="Featurewise compact payload",
            )

            structured = result["structured_data"]
            self.assertEqual(structured["analysis"], "matrix_featurewise_group_test")
            self.assertNotIn("significant_features", structured)
            self.assertIn("significant_features_preview", structured)
            self.assertIn("significant_features_counts", structured)
            self.assertEqual(
                structured["significant_features_counts"]["bh"],
                structured["question_ready_counts"]["significant_feature_count_bh"],
            )
            self.assertEqual(
                structured["significant_features_counts"]["raw"],
                structured["question_ready_counts"]["significant_feature_count_raw"],
            )
            self.assertIn("significant_feature_percentage_raw", structured["question_ready_counts"])
            self.assertEqual(
                structured["significant_features_counts"]["all_corrections"],
                structured["question_ready_counts"]["significant_feature_count_all_corrections"],
            )

    def test_ranked_gene_set_gsea_accepts_ensembl_ids_and_normalizes_internally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = self.build_reference_catalog(root)
            service = self.build_service(root, reference_catalog=catalog)

            result = service.ranked_gene_set_gsea(
                gene_scores={
                    "ENSG_CLOCK": 2.5,
                    "ENSG_ARNTL": 2.2,
                    "ENSG_TP53": 1.9,
                    "ENSG_BGN": -1.6,
                },
                label="Ensembl ranked signature",
            )

            self.assertEqual(result["provider"], "yulab_clusterprofiler")
            self.assertEqual(result["structured_data"]["input_gene_scores"][0]["gene"], "ENSG_CLOCK")
            self.assertEqual([row["gene"] for row in result["structured_data"]["ranked_gene_scores"]][:4], ["CLOCK", "ARNTL", "TP53", "BGN"])
            self.assertTrue(result["structured_data"]["normalized_gene_set"]["resolution_used"])
            self.assertEqual(result["structured_data"]["normalized_gene_set"]["analysis_gene_scores"][0]["gene"], "CLOCK")

    def test_session_notes_are_saved_as_first_class_markdown_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Clock note workspace",
                description="Workspace for note testing.",
                species="human",
            )

            note = service.add_session_note(
                created["session_id"],
                title="Hypothesis note",
                body_markdown="## Idea\n\nCLOCK may shape DNA repair timing.",
                author="assistant",
            )
            notes = service.list_session_notes(created["session_id"])
            loaded = service.get_session_note(created["session_id"], note["note_id"])

            self.assertEqual(notes["note_count"], 1)
            self.assertEqual(notes["notes"][0]["title"], "Hypothesis note")
            self.assertIn("DNA repair timing", loaded["body_markdown"])
            self.assertTrue(loaded["path"].endswith(f"{note['note_id']}.md"))

    def test_session_title_and_description_can_be_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Provisional session",
                description="Temporary framing for the study.",
                species="human",
            )

            updated = service.update_session(
                created["session_id"],
                title="Refined circadian-metabolism session",
                description="Focused study of circadian regulation, lipid metabolism, and disease context.",
            )
            summary = service.get_session_summary(created["session_id"])

            self.assertEqual(updated["title"], "Refined circadian-metabolism session")
            self.assertEqual(
                updated["description"],
                "Focused study of circadian regulation, lipid metabolism, and disease context.",
            )
            self.assertEqual(summary["title"], "Refined circadian-metabolism session")
            self.assertEqual(
                summary["description"],
                "Focused study of circadian regulation, lipid metabolism, and disease context.",
            )

    def test_delete_session_artifacts_removes_multiple_saved_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Artifact delete workspace",
                description="Workspace for artifact deletion testing.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            first = service.resolve_gene(query="TP53")
            second = service.search_papers(query="TP53 DNA repair", limit=1)
            first_id = first["workspace"]["artifact_id"]
            second_id = second["workspace"]["artifact_id"]

            deleted = service.delete_session_artifacts(created["session_id"], [first_id, second_id])
            artifacts = service.list_artifacts(created["session_id"])

            self.assertEqual(deleted["deleted_count"], 2)
            self.assertEqual(deleted["deleted_artifact_ids"], [first_id, second_id])
            self.assertEqual(artifacts["artifact_count"], 0)

    def test_get_artifact_can_resolve_active_session_when_session_id_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Artifact lookup workspace",
                description="Workspace for artifact lookup testing.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            resolved = service.resolve_gene(query="TP53")
            artifact_id = resolved["workspace"]["artifact_id"]

            loaded = service.get_artifact(None, artifact_id)

            self.assertEqual(loaded["artifact_id"], artifact_id)
            self.assertEqual(loaded["session_id"], created["session_id"])
            self.assertEqual(loaded["kind"], "gene_resolution")

    def test_get_artifact_returns_helpful_error_when_lookup_cannot_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Artifact error workspace",
                description="Workspace for artifact error messaging.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            with self.assertRaises(FileNotFoundError) as caught:
                service.get_artifact(None, "artifact_missing123")

            message = str(caught.exception)
            self.assertIn("Could not resolve artifact_id 'artifact_missing123'", message)
            self.assertIn("get_artifact(session_id='session_x', artifact_id='artifact_y')", message)

    def test_report_templates_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))

            templates = service.list_report_templates()
            mechanism = service.get_report_template("mechanism")
            disease_context = service.get_report_template("disease_context")

            self.assertGreaterEqual(templates["template_count"], 7)
            self.assertTrue(any(item["id"] == "custom" for item in templates["templates"]))
            self.assertTrue(any(item["id"] == "disease_context" for item in templates["templates"]))
            self.assertEqual(mechanism["id"], "mechanism")
            self.assertGreaterEqual(len(mechanism["sections"]), 1)
            self.assertEqual(disease_context["id"], "disease_context")
            self.assertGreaterEqual(len(disease_context["sections"]), 1)

    def test_multiple_session_reports_are_authored_and_editable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Report workspace",
                description="Workspace for report version testing.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )
            resolved = service.resolve_gene(query="TP53")
            artifact_id = resolved["workspace"]["artifact_id"]

            first = service.create_session_report(
                created["session_id"],
                title="Mechanistic report",
                body_markdown="# Mechanistic report\n\nInitial mechanistic draft.",
                focus="mechanistic",
                template_id="mechanism",
                linked_artifact_ids=[artifact_id],
            )
            second = service.create_session_report(
                created["session_id"],
                title="Biomarker report",
                body_markdown="# Biomarker report\n\nMarker-oriented draft.",
                focus="biomarker",
                template_id="biomarker",
            )
            updated = service.update_session_report(
                created["session_id"],
                first["report_id"],
                body_markdown="# Mechanistic report\n\nUpdated mechanistic framing.",
                focus="mechanistic",
                template_id="mechanism",
            )
            reports = service.list_session_reports(created["session_id"])
            loaded = service.get_session_report(created["session_id"], first["report_id"])
            deleted = service.delete_session_report(created["session_id"], second["report_id"])
            reports_after_delete = service.list_session_reports(created["session_id"])

            self.assertEqual(reports["report_count"], 2)
            self.assertEqual({item["title"] for item in reports["reports"]}, {"Mechanistic report", "Biomarker report"})
            self.assertEqual(updated["template_id"], "mechanism")
            self.assertEqual(loaded["title"], "Mechanistic report")
            self.assertEqual(loaded["focus"], "mechanistic")
            self.assertEqual(loaded["template_id"], "mechanism")
            self.assertEqual(loaded["linked_artifact_ids"], [artifact_id])
            self.assertIn("Updated mechanistic framing", loaded["body_markdown"])
            self.assertEqual(deleted["deleted_report_id"], second["report_id"])
            self.assertEqual(reports_after_delete["report_count"], 1)

    def test_saved_reports_append_compact_evidence_links_from_linked_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Evidence report workspace",
                description="Workspace for report provenance testing.",
                species="human",
            )
            workspace = service.workspace_gateway.require_workspace()[1]
            artifact_one = workspace.store.create_artifact(
                created["session_id"],
                kind="pathway_context",
                title="Pathway context",
                summary="Built pathway context for 7 genes.",
                provider="gleskit_aggregate",
                payload={},
            )
            artifact_two = workspace.store.create_artifact(
                created["session_id"],
                kind="paper_search",
                title="Literature bundle",
                summary="Retrieved 2 supporting papers.",
                provider="europe_pmc",
                payload={
                    "papers": [
                        {"title": "DNA repair in cancer", "url": "https://example.org/paper-1"},
                        {"title": "Cell-cycle regulation overview", "url": "https://example.org/paper-2"},
                    ]
                },
            )

            created_report = service.create_session_report(
                created["session_id"],
                title="Mechanistic report",
                body_markdown="# Mechanistic report\n\nGrounded narrative.",
                focus="mechanistic",
                template_id="mechanism",
                linked_artifact_ids=[artifact_one.artifact_id, artifact_two.artifact_id],
            )
            updated_report = service.update_session_report(
                created["session_id"],
                created_report["report_id"],
                body_markdown="# Mechanistic report\n\nGrounded narrative revised.",
                focus="mechanistic",
                template_id="mechanism",
                linked_artifact_ids=[artifact_one.artifact_id, artifact_two.artifact_id],
            )

            body = str(updated_report["body_markdown"])
            self.assertIn("## Evidence links", body)
            self.assertIn(
                f"../../sessions/{created['session_id']}/artifacts/{artifact_one.artifact_id}.json",
                body,
            )
            self.assertIn("[DNA repair in cancer](https://example.org/paper-1)", body)
            self.assertEqual(body.count("## Evidence links"), 1)
            self.assertEqual(len(updated_report["linked_artifacts"]), 2)
            self.assertEqual(updated_report["citation_audit"]["issue_count"], 0)

    def test_session_report_payload_exposes_citation_audit_with_exact_title_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Citation audit workspace",
                description="Workspace for citation audit testing.",
                species="human",
            )
            workspace = service.workspace_gateway.require_workspace()[1]
            paper_artifact = workspace.store.create_artifact(
                created["session_id"],
                kind="paper_search",
                title="Literature bundle",
                summary="Retrieved 1 supporting paper.",
                provider="europe_pmc",
                payload={
                    "papers": [
                        {
                            "title": "Decoding the Therapeutic Effects of Acupuncture in Hemorrhagic Stroke Using Single-Cell RNA Sequencing.",
                            "url": "https://europepmc.org/article/MED/41363028",
                            "pmid": "41363028",
                            "doi": "10.1002/cns.70689",
                            "first_author_surname": "Ruan",
                            "year": "2025",
                        }
                    ]
                },
            )

            report = service.create_session_report(
                created["session_id"],
                title="Disease context report",
                body_markdown=(
                    "# Disease context report\n\n"
                    'Sources: Ruan et al. 2025 "APOE-TREM2 signaling axis" '
                    "[Europe PMC](https://europepmc.org/article/MED/41363028).\n"
                ),
                focus="disease_context",
                template_id="disease_context",
                linked_artifact_ids=[paper_artifact.artifact_id],
            )
            loaded = service.get_session_report(created["session_id"], report["report_id"])

            self.assertIn("citation_title_mismatch", report["citation_audit"]["issue_codes"])
            self.assertEqual(report["citation_audit"]["major_issue_count"], 1)
            self.assertEqual(
                report["citation_audit"]["issues"][0]["suggested_exact_link"],
                "[Decoding the Therapeutic Effects of Acupuncture in Hemorrhagic Stroke Using Single-Cell RNA Sequencing.](https://europepmc.org/article/MED/41363028)",
            )
            self.assertEqual(loaded["citation_audit"]["issue_codes"], report["citation_audit"]["issue_codes"])

    def test_session_report_citation_audit_accepts_clickable_author_year_citations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Author-year citation workspace",
                description="Workspace for author-year citation testing.",
                species="human",
            )
            workspace = service.workspace_gateway.require_workspace()[1]
            paper_artifact = workspace.store.create_artifact(
                created["session_id"],
                kind="paper_search",
                title="Literature bundle",
                summary="Retrieved 1 supporting paper.",
                provider="europe_pmc",
                payload={
                    "papers": [
                        {
                            "title": "Decoding the Therapeutic Effects of Acupuncture in Hemorrhagic Stroke Using Single-Cell RNA Sequencing.",
                            "url": "https://europepmc.org/article/MED/41363028",
                            "pmid": "41363028",
                            "doi": "10.1002/cns.70689",
                            "first_author_surname": "Ruan",
                            "year": "2025",
                        }
                    ]
                },
            )

            report = service.create_session_report(
                created["session_id"],
                title="Disease context report",
                body_markdown=(
                    "# Disease context report\n\n"
                    "Sources: Europe PMC; [(Ruan et al., 2025)](https://europepmc.org/article/MED/41363028).\n"
                ),
                focus="disease_context",
                template_id="disease_context",
                linked_artifact_ids=[paper_artifact.artifact_id],
            )

            self.assertEqual(report["citation_audit"]["issue_count"], 0)
            self.assertEqual(report["citation_audit"]["paper_checks"][0]["citation_style"], "author_year")

    def test_custom_reports_require_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Custom report workspace",
                description="Workspace for custom report validation.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            with self.assertRaises(ValueError):
                service.create_session_report(
                    created["session_id"],
                    title="Custom report",
                    body_markdown="# Custom report\n\nNeeds structure.",
                    template_id="custom",
                )

    def test_new_reports_require_explicit_template_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="Template gate workspace",
                description="Workspace for report-type gating.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            with self.assertRaises(ValueError):
                service.create_session_report(
                    created["session_id"],
                    title="Untyped report",
                    body_markdown="# Untyped report\n\nShould not save without a chosen type.",
                    template_id="",
                )

    def test_investigate_gene_set_captures_multiple_artifacts_when_workspace_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.build_service(root)
            settings = load_settings(root)
            write_runtime_manifest(settings.runtime_manifest_path, workspace_root=settings.workspace_default_root)
            created = service.create_session(
                title="DNA damage set",
                description="Workspace for DNA repair testing.",
                species="human",
            )
            write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=settings.workspace_default_root,
                active_session_id=created["session_id"],
            )

            result = service.investigate_gene_set(
                genes=["TP53", "BRCA1"],
                question="What pathway and disease story is supported?",
                label="DNA damage set",
            )

            self.assertTrue(result["workspace"]["saved"])
            self.assertEqual(result["structured_data"]["disease_enrichment"]["term_count"], 2)
            self.assertGreaterEqual(len(result["workspace"]["child_artifact_ids"]), 5)
            summary = service.get_session_summary(created["session_id"])
            self.assertGreaterEqual(summary["artifact_count"], 6)

    def test_workspace_management_requires_runtime_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            with self.assertRaises(WorkspaceUnavailableError):
                service.list_sessions()

    def test_variant_trait_associations_require_human_species(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            with self.assertRaises(ProviderError):
                service.variant_trait_associations(gene="Trp53", species="mouse")

    def test_gene_disease_associations_add_gwas_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.build_service(Path(tmp))
            result = service.gene_disease_associations(gene="TP53")

            self.assertIn("variant_trait_context", result["structured_data"])
            self.assertEqual(result["structured_data"]["variant_trait_context"]["association_count"], 1)

    def test_gene_disease_associations_raise_diagnostic_error_when_open_targets_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(
                settings=settings,
                mygene=_FakeMyGeneProvider(),
                ensembl=_FakeEnsemblProvider(),
                uniprot=_FakeUniProtProvider(),
                quickgo=_FakeQuickGOProvider(),
                reactome=_FakeReactomeProvider(),
                stringdb=_FakeStringDBProvider(),
                gwas_catalog=_FakeGWASCatalogProvider(),
                chembl=_FakeChEMBLProvider(),
                gprofiler=_FakeGProfilerProvider(),
                europepmc=_FakeEuropePMCProvider(),
                opentargets=_FailingOpenTargetsProvider(),
                workspace_gateway=WorkspaceGateway(settings),
            )

            with self.assertRaises(ProviderError) as caught:
                service.gene_disease_associations(gene="BGN")

            message = str(caught.exception)
            self.assertIn("resolved_symbol='BGN'", message)
            self.assertIn("ensembl_gene_id='ENSG_BGN'", message)
            self.assertIn("timed out", message.lower())


if __name__ == "__main__":
    unittest.main()
