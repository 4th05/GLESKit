from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gleskit_mcp.config import load_settings
from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.store import WorkspaceStore
from gleskit_mcp.workspace import WorkspaceUnavailableError, clear_runtime_manifest, write_runtime_manifest
from gleskit_mcp.workspace import WorkspaceService


def build_default_service(project_root: Path):
    # Analysis dependencies are unnecessary for help and direct workspace reads.
    from gleskit_mcp.services import build_default_service as build
    return build(project_root)


class _LazyService:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self._service = None

    def __getattr__(self, name):
        if self._service is None:
            self._service = build_default_service(self.project_root)
        return getattr(self._service, name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GLESKit local development CLI.")
    parser.add_argument("--project-root", default=".", help="Project root used for local runtime storage.")
    parser.add_argument(
        "--workspace-root",
        default="",
        help="Direct workspace root for local UI commands. When set, workspace commands bypass the desktop runtime gate.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("workspace-status", help="Show whether the local workspace bridge is available.")
    subparsers.add_parser("reference-status", help="Show whether local reference datasets are installed.")
    subparsers.add_parser("update-references", help="Download or refresh the local reference datasets.")

    create = subparsers.add_parser("create-session", help="Create a new saved workspace session.")
    create.add_argument("--title", required=True)
    create.add_argument("--description", required=False)
    create.add_argument("--question", dest="description", help=argparse.SUPPRESS)
    create.add_argument("--species", default="")
    create.add_argument("--tags", nargs="*", default=[])

    update_session = subparsers.add_parser("update-session", help="Update the title and/or description of a saved session.")
    update_session.add_argument("--session-id", required=True)
    update_session.add_argument("--title")
    update_session.add_argument("--description")

    note = subparsers.add_parser("add-note", help="Persist a note into a saved session.")
    note.add_argument("--session-id", required=True)
    note.add_argument("--title", default="")
    note.add_argument("--note", dest="body_markdown", default="", help=argparse.SUPPRESS)
    note.add_argument("--body-markdown", default="")
    note.add_argument("--author", default="assistant")
    note.add_argument("--linked-artifact-ids", nargs="*", default=[])
    note.add_argument("--tags", nargs="*", default=[])

    list_notes = subparsers.add_parser("list-notes", help="List saved markdown notes for a session.")
    list_notes.add_argument("--session-id", required=True)

    get_note = subparsers.add_parser("note", help="Show one saved session note.")
    get_note.add_argument("--session-id", required=True)
    get_note.add_argument("--note-id", required=True)

    list_report_templates = subparsers.add_parser(
        "list-report-templates",
        help="List authored report templates.",
    )

    get_report_template = subparsers.add_parser(
        "report-template",
        help="Show one authored report template.",
    )
    get_report_template.add_argument("--template-id", required=True)

    create_report = subparsers.add_parser("create-report", help="Persist an authored report into a saved session.")
    create_report.add_argument("--session-id", required=True)
    create_report.add_argument("--title", default="")
    create_report.add_argument("--body-markdown", required=True)
    create_report.add_argument("--template-id", required=True)
    create_report.add_argument("--author", default="assistant")
    create_report.add_argument("--focus", default="")
    create_report.add_argument("--custom-guidance", default="")
    create_report.add_argument("--linked-artifact-ids", nargs="*", default=[])
    create_report.add_argument("--tags", nargs="*", default=[])

    update_report = subparsers.add_parser("update-report", help="Update one saved session report.")
    update_report.add_argument("--session-id", required=True)
    update_report.add_argument("--report-id", required=True)
    update_report.add_argument("--body-markdown", required=True)
    update_report.add_argument("--title")
    update_report.add_argument("--author")
    update_report.add_argument("--focus")
    update_report.add_argument("--template-id")
    update_report.add_argument("--custom-guidance")
    update_report.add_argument("--linked-artifact-ids", nargs="*", default=None)
    update_report.add_argument("--tags", nargs="*", default=None)

    delete_report = subparsers.add_parser("delete-report", help="Delete one saved session report.")
    delete_report.add_argument("--session-id", required=True)
    delete_report.add_argument("--report-id", required=True)

    delete_reports = subparsers.add_parser("delete-reports", help="Delete multiple saved session reports.")
    delete_reports.add_argument("--session-id", required=True)
    delete_reports.add_argument("--report-ids", nargs="+", required=True)

    update_note = subparsers.add_parser("update-note", help="Update one saved session note.")
    update_note.add_argument("--session-id", required=True)
    update_note.add_argument("--note-id", required=True)
    update_note.add_argument("--title", required=True)
    update_note.add_argument("--body-markdown", required=True)
    update_note.add_argument("--author", default="assistant")
    update_note.add_argument("--linked-artifact-ids", nargs="*", default=[])
    update_note.add_argument("--tags", nargs="*", default=[])

    delete_note = subparsers.add_parser("delete-note", help="Delete one saved session note.")
    delete_note.add_argument("--session-id", required=True)
    delete_note.add_argument("--note-id", required=True)

    delete_notes = subparsers.add_parser("delete-notes", help="Delete multiple saved session notes.")
    delete_notes.add_argument("--session-id", required=True)
    delete_notes.add_argument("--note-ids", nargs="+", required=True)

    sessions = subparsers.add_parser("list-sessions", help="List saved workspace sessions.")
    sessions.add_argument("--query", default="")
    sessions.add_argument("--status", default="")

    summary = subparsers.add_parser("session-summary", help="Show a saved session summary.")
    summary.add_argument("--session-id", required=True)

    snapshot = subparsers.add_parser(
        "session-snapshot",
        help="Return one session summary plus its artifacts and uploads in a single call.",
    )
    snapshot.add_argument("--session-id", required=True)

    cohorts = subparsers.add_parser("list-cohorts", help="List saved cohort libraries.")

    cohort_providers = subparsers.add_parser("list-cohort-providers", help="List available cohort providers.")

    cohort_catalog = subparsers.add_parser("cohort-catalog", help="List datasets for one cohort provider.")
    cohort_catalog.add_argument("--provider-id", required=True)

    get_cohort = subparsers.add_parser("cohort", help="Show one saved cohort.")
    get_cohort.add_argument("--cohort-id", required=True)

    download_cohort = subparsers.add_parser("download-cohort", help="Download a public cohort into the local cohort library.")
    download_cohort.add_argument("--title", required=True)
    download_cohort.add_argument("--provider-id", required=True)
    download_cohort.add_argument("--dataset-id", required=True)
    download_cohort.add_argument("--tissue", default="")
    download_cohort.add_argument("--species", default="")
    download_cohort.add_argument("--sample-limit", type=int, default=0)
    download_cohort.add_argument("--tags", nargs="*", default=[])

    create_cohort = subparsers.add_parser("create-cohort", help="Import a local cohort into the local cohort library.")
    create_cohort.add_argument("--title", required=True)
    create_cohort.add_argument("--provider-id", default="local_import")
    create_cohort.add_argument("--source-kind", default="imported")
    create_cohort.add_argument("--species", default="human")
    create_cohort.add_argument("--counts-path", required=False, default="")
    create_cohort.add_argument("--metadata-path", required=False, default="")
    create_cohort.add_argument("--transpose", action="store_true")
    create_cohort.add_argument("--sample-column", default="")
    create_cohort.add_argument("--group-column", default="")
    create_cohort.add_argument("--min-count", type=int, default=10)
    create_cohort.add_argument("--sample-limit", type=int, default=0)
    create_cohort.add_argument("--source-counts-path", default="")
    create_cohort.add_argument("--source-metadata-path", default="")
    create_cohort.add_argument("--tags", nargs="*", default=[])

    update_cohort = subparsers.add_parser("update-cohort", help="Update one saved cohort.")
    update_cohort.add_argument("--cohort-id", required=True)
    update_cohort.add_argument("--title", required=True)

    delete_cohort = subparsers.add_parser("delete-cohort", help="Delete one saved cohort.")
    delete_cohort.add_argument("--cohort-id", required=True)

    create_session_from_cohort = subparsers.add_parser(
        "create-session-from-cohort",
        help="Create a new session and link the selected cohort files as uploads.",
    )
    create_session_from_cohort.add_argument("--cohort-id", required=True)
    create_session_from_cohort.add_argument("--title", required=True)
    create_session_from_cohort.add_argument("--description", required=True)
    create_session_from_cohort.add_argument("--species", default="")
    create_session_from_cohort.add_argument("--tags", nargs="*", default=[])

    artifacts = subparsers.add_parser("artifacts", help="List stored artifacts for a session.")
    artifacts.add_argument("--session-id", required=True)
    artifacts.add_argument("--kind", default="")

    uploads = subparsers.add_parser("uploads", help="List stored uploads for a session.")
    uploads.add_argument("--session-id", required=True)

    artifact = subparsers.add_parser("artifact", help="Show one stored artifact.")
    artifact.add_argument("--session-id", required=True)
    artifact.add_argument("--artifact-id", required=True)

    delete_artifacts = subparsers.add_parser("delete-artifacts", help="Delete stored artifacts from a session.")
    delete_artifacts.add_argument("--session-id", required=True)
    delete_artifacts.add_argument("--artifact-ids", nargs="+", required=True)

    upload = subparsers.add_parser("upload", help="Show one stored upload.")
    upload.add_argument("--session-id", required=True)
    upload.add_argument("--upload-id", required=True)

    delete_uploads = subparsers.add_parser("delete-uploads", help="Delete stored uploads from a session.")
    delete_uploads.add_argument("--session-id", required=True)
    delete_uploads.add_argument("--upload-ids", nargs="+", required=True)

    create_upload = subparsers.add_parser("create-upload", help="Persist a linked or copied upload into a saved session.")
    create_upload.add_argument("--session-id", required=True)
    create_upload.add_argument("--data-path", required=True)
    create_upload.add_argument("--source-mode", required=True)
    create_upload.add_argument("--title", default="")
    create_upload.add_argument("--source-path", default="")
    create_upload.add_argument("--stored-path", default="")
    create_upload.add_argument("--file-name", default="")
    create_upload.add_argument("--mime-type", default="")

    create_figure = subparsers.add_parser("create-figure", help="Generate and save a figure artifact from a saved artifact.")
    create_figure.add_argument("--session-id", required=True)
    create_figure.add_argument("--artifact-id", required=True)
    create_figure.add_argument("--figure-type", required=True)
    create_figure.add_argument("--title", default="")
    create_figure.add_argument("--top-n", type=int, default=10)

    archive = subparsers.add_parser("archive-session", help="Archive a saved session.")
    archive.add_argument("--session-id", required=True)

    plan_delete = subparsers.add_parser("plan-delete-sessions", help="Preview which sessions would be deleted.")
    plan_delete.add_argument("--session-ids", nargs="+", required=True)

    delete = subparsers.add_parser("delete-sessions", help="Delete saved sessions.")
    delete.add_argument("--session-ids", nargs="+", required=True)

    copy = subparsers.add_parser("create-session-from-artifacts", help="Create a new session by copying stored artifacts.")
    copy.add_argument("--title", required=True)
    copy.add_argument("--description", required=True)
    copy.add_argument("--species", default="")
    copy.add_argument("--tags", nargs="*", default=[])
    copy.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help="Artifact sources in the form session_id:artifact_id",
    )

    resolve = subparsers.add_parser("resolve-gene", help="Resolve a gene with the local reference bundle first, then MyGene.")
    resolve.add_argument("--query", required=True)
    resolve.add_argument("--species", default="")

    resolve_gene_set = subparsers.add_parser(
        "resolve-gene-set",
        help="Resolve a whole gene list in one call. Prefer set-level tools for ordinary analysis because they normalize internally.",
    )
    resolve_gene_set.add_argument("--genes", nargs="+", required=True)
    resolve_gene_set.add_argument("--species", default="")
    resolve_gene_set.add_argument("--limit", type=int, default=300)

    gene_summary = subparsers.add_parser("gene-summary", help="Build a gene summary from MyGene, Ensembl, and UniProt.")
    gene_summary.add_argument("--query", required=True)
    gene_summary.add_argument("--species", default="")

    resolve_protein = subparsers.add_parser("resolve-protein", help="Resolve a protein using UniProt.")
    resolve_protein.add_argument("--query", required=True)
    resolve_protein.add_argument("--species", default="")

    protein_summary = subparsers.add_parser("protein-summary", help="Build a protein summary from UniProt and linked gene context.")
    protein_summary.add_argument("--query", required=True)
    protein_summary.add_argument("--species", default="")

    drug_target = subparsers.add_parser("drug-target", help="Retrieve ChEMBL drug-target context for one gene.")
    drug_target.add_argument("--gene", required=True)
    drug_target.add_argument("--species", default="")
    drug_target.add_argument("--limit", type=int, default=10)

    variant_traits = subparsers.add_parser("variant-traits", help="Retrieve GWAS Catalog variant-trait associations for one gene.")
    variant_traits.add_argument("--gene", required=True)
    variant_traits.add_argument("--species", default="")
    variant_traits.add_argument("--limit", type=int, default=10)

    interaction_network = subparsers.add_parser("interaction-network", help="Retrieve STRING interaction-network context for a gene set.")
    interaction_network.add_argument("--genes", nargs="+", required=True)
    interaction_network.add_argument("--species", default="")
    interaction_network.add_argument("--limit", type=int, default=25)

    pathway_context = subparsers.add_parser("pathway-context", help="Build Reactome-focused pathway context for a gene set.")
    pathway_context.add_argument("--genes", nargs="+", required=True)
    pathway_context.add_argument("--species", default="")
    pathway_context.add_argument("--limit", type=int, default=5)

    expression_context = subparsers.add_parser("expression-context", help="Build reference transcriptomics context from GTEx and Expression Atlas.")
    expression_context.add_argument("--gene", required=True)
    expression_context.add_argument("--species", default="")
    expression_context.add_argument("--limit", type=int, default=10)

    rnaseq = subparsers.add_parser("analyze-rnaseq", help="Interpret a differential-expression table from RNA-seq results.")
    rnaseq.add_argument("--result-path", default="")
    rnaseq.add_argument("--table-text", default="")
    rnaseq.add_argument("--sheet-name", default="")
    rnaseq.add_argument("--header-row", type=int, default=1)
    rnaseq.add_argument("--skip-rows", type=int, default=0)
    rnaseq.add_argument("--species", default="")
    rnaseq.add_argument("--label", default="")
    rnaseq.add_argument("--comparison", default="")
    rnaseq.add_argument("--gene-column", default="")
    rnaseq.add_argument("--log2-fold-change-column", default="")
    rnaseq.add_argument("--padj-column", default="")
    rnaseq.add_argument("--p-value-column", default="")
    rnaseq.add_argument("--base-mean-column", default="")
    rnaseq.add_argument("--dispersion-column", default="")
    rnaseq.add_argument("--stat-column", default="")
    rnaseq.add_argument("--padj-threshold", type=float, default=0.05)
    rnaseq.add_argument("--p-value-threshold", type=float, default=0.05)
    rnaseq.add_argument("--base-mean-threshold", type=float, default=0.0)
    rnaseq.add_argument("--log2-fold-change-threshold", type=float, default=0.0)
    rnaseq.add_argument("--top-n", type=int, default=25)

    differential = subparsers.add_parser("analyze-differential-result", help="Analyze a precomputed transcriptomics or proteomics differential-result table.")
    differential.add_argument("--modality", required=True, choices=["transcriptomics", "proteomics"])
    differential.add_argument("--positive-condition", required=True)
    differential.add_argument("--reference-condition", required=True)
    differential.add_argument("--result-scope", required=True, choices=["full_results", "filtered_results"])
    differential.add_argument("--result-path", default="")
    differential.add_argument("--table-text", default="")
    differential.add_argument("--sheet-name", default="")
    differential.add_argument("--header-row", type=int, default=1)
    differential.add_argument("--skip-rows", type=int, default=0)
    differential.add_argument("--species", default="")
    differential.add_argument("--label", default="")
    differential.add_argument("--identifier-column", default="")
    differential.add_argument("--effect-column", default="")
    differential.add_argument("--padj-column", default="")
    differential.add_argument("--p-value-column", default="")
    differential.add_argument("--stat-column", default="")
    differential.add_argument("--significance-mode", required=True, choices=["padj", "p_value"])
    differential.add_argument("--padj-threshold", type=float, default=0.05)
    differential.add_argument("--p-value-threshold", type=float, default=0.05)
    differential.add_argument("--log2-fold-change-threshold", type=float, default=0.0)
    differential.add_argument("--background-gene", action="append", default=[])
    differential.add_argument("--identifier-mapping", default="", help="JSON object mapping input identifiers to one or more gene symbols.")
    differential.add_argument("--identifier-mapping-path", default="")
    differential.add_argument("--mapping-identifier-column", default="")
    differential.add_argument("--mapping-gene-column", default="")

    deseq2 = subparsers.add_parser("analyze-deseq2", help="Run DESeq2 on uploaded raw counts and save the outputs as artifacts.")
    deseq2.add_argument("--counts-path", required=True)
    deseq2.add_argument("--counts-sheet-name", default="")
    deseq2.add_argument("--counts-header-row", type=int, default=1)
    deseq2.add_argument("--counts-skip-rows", type=int, default=0)
    deseq2.add_argument("--sample-metadata-path", default="")
    deseq2.add_argument("--sample-metadata-sheet-name", default="")
    deseq2.add_argument("--sample-metadata-header-row", type=int, default=1)
    deseq2.add_argument("--sample-metadata-skip-rows", type=int, default=0)
    deseq2.add_argument("--sample-metadata-text", default="")
    deseq2.add_argument(
        "--sample-group-map",
        default="",
        help="JSON object mapping every count sample column to its condition when no metadata table is available.",
    )
    deseq2.add_argument("--species", default="")
    deseq2.add_argument("--label", default="")
    deseq2.add_argument("--comparison", default="")
    deseq2.add_argument("--gene-column", default="")
    deseq2.add_argument("--sample-column", default="sample")
    deseq2.add_argument("--condition-column", default="condition")
    deseq2.add_argument("--condition-a", default="")
    deseq2.add_argument("--condition-b", default="")
    deseq2.add_argument("--design-formula", default="")
    deseq2.add_argument("--min-count", type=int, default=10)
    deseq2.add_argument("--padj-threshold", type=float, default=0.05)
    deseq2.add_argument("--p-value-threshold", type=float, default=0.05)
    deseq2.add_argument("--base-mean-threshold", type=float, default=0.0)
    deseq2.add_argument("--significance-mode", choices=["padj", "p_value"], default="padj")
    deseq2.add_argument("--log2-fold-change-threshold", type=float, default=0.0)
    deseq2.add_argument("--top-n", type=int, default=25)
    deseq2.add_argument("--lfc-shrinkage", default="none")

    enrich = subparsers.add_parser(
        "enrich-genes",
        help="Run local pathway enrichment for human gene sets, with g:Profiler fallback when needed.",
    )
    enrich.add_argument("--genes", nargs="+", required=True)
    enrich.add_argument("--species", default="")
    enrich.add_argument("--label", default="")
    enrich.add_argument("--database", default="GO_BP")
    enrich.add_argument("--library", default="")
    enrich.add_argument("--backend", default="auto")
    enrich.add_argument("--background-genes", nargs="*", default=[])
    enrich.add_argument("--simplify-go-similarity", type=float, default=0.0)

    disease_enrichment = subparsers.add_parser(
        "gene-set-disease",
        help="Run local disease enrichment for a gene set using YuLab disease knowledge bases.",
    )
    disease_enrichment.add_argument("--genes", nargs="+", required=True)
    disease_enrichment.add_argument("--species", default="")
    disease_enrichment.add_argument("--label", default="")
    disease_enrichment.add_argument("--database", default="DO")
    disease_enrichment.add_argument("--limit", type=int, default=15)

    compare_programs = subparsers.add_parser(
        "compare-programs",
        help="Compare enrichment signals across multiple named gene programs using the local YuLab backend.",
    )
    compare_programs.add_argument("--program", action="append", required=True)
    compare_programs.add_argument("--species", default="")
    compare_programs.add_argument("--label", default="")
    compare_programs.add_argument("--database", default="GO_BP")
    compare_programs.add_argument("--limit", type=int, default=10)

    ranked_gsea = subparsers.add_parser(
        "ranked-gsea",
        help="Run local ranked GSEA on a preranked gene signature using the YuLab backend.",
    )
    ranked_gsea.add_argument("--gene-score", action="append", required=True)
    ranked_gsea.add_argument("--species", default="")
    ranked_gsea.add_argument("--label", default="")
    ranked_gsea.add_argument("--limit", type=int, default=15)
    ranked_gsea.add_argument("--database", default="GO_BP")

    expr_stats = subparsers.add_parser(
        "run-expression-statistics",
        help="Run one expression-statistics or RNA/QC calculation.",
    )
    expr_stats.add_argument("--analysis", default="distribution_summary")
    expr_stats.add_argument("--source-path", default="")
    expr_stats.add_argument("--table-text", default="")
    expr_stats.add_argument("--row-id-column", default="")
    expr_stats.add_argument("--delimiter", default="")
    expr_stats.add_argument("--sheet-name", default="")
    expr_stats.add_argument("--header-row", type=int, default=1)
    expr_stats.add_argument("--skip-rows", type=int, default=0)
    expr_stats.add_argument("--transpose", action="store_true")
    expr_stats.add_argument("--analysis-axis", default="columns")
    expr_stats.add_argument("--target-axis", default="features")
    expr_stats.add_argument("--summary-stat", default="mean")
    expr_stats.add_argument("--annotation-path", default="")
    expr_stats.add_argument("--annotation-table-text", default="")
    expr_stats.add_argument("--annotation-delimiter", default="")
    expr_stats.add_argument("--annotation-sheet-name", default="")
    expr_stats.add_argument("--annotation-header-row", type=int, default=1)
    expr_stats.add_argument("--annotation-skip-rows", type=int, default=0)
    expr_stats.add_argument("--annotation-key-column", default="")
    expr_stats.add_argument("--annotation-value-column", default="")
    expr_stats.add_argument("--method", default="pearson")
    expr_stats.add_argument("--value-column", default="")
    expr_stats.add_argument("--scope", default="all_values")
    expr_stats.add_argument("--group-column", default="")
    expr_stats.add_argument("--key-column", default="")
    expr_stats.add_argument("--pair-column", default="")
    expr_stats.add_argument("--annotation-group-column", default="")
    expr_stats.add_argument("--test", default="welch")
    expr_stats.add_argument("--observation-unit", default="flattened_values")
    expr_stats.add_argument("--value-transform", default="none")
    expr_stats.add_argument("--pseudocount", type=float, default=1.0)
    expr_stats.add_argument("--success-count", type=int, default=0)
    expr_stats.add_argument("--trial-count", type=int, default=0)
    expr_stats.add_argument("--null-probability", type=float, default=0.5)
    expr_stats.add_argument("--alpha", type=float, default=0.05)
    expr_stats.add_argument("--correction-method", action="append", default=[])
    expr_stats.add_argument("--label", default="")

    single_cell = subparsers.add_parser(
        "analyze-single-cell",
        help="Run one single-cell RNA analysis task.",
    )
    single_cell.add_argument("--analysis", default="summary")
    single_cell.add_argument("--h5ad-path", default="")
    single_cell.add_argument("--deg-table-path", default="")
    single_cell.add_argument("--gene", default="")
    single_cell.add_argument("--padj-threshold", type=float, default=0.05)
    single_cell.add_argument("--gene-column", default="")
    single_cell.add_argument("--cell-type-column", default="")
    single_cell.add_argument("--padj-column", default="")
    single_cell.add_argument("--p-value-column", default="")
    single_cell.add_argument("--log2-fold-change-column", default="")
    single_cell.add_argument("--sheet-name", action="append", default=[])
    single_cell.add_argument("--sample-column", default="")
    single_cell.add_argument("--condition-column", default="")
    single_cell.add_argument("--cell-type-value", action="append", default=[])
    single_cell.add_argument("--layer", default="")
    single_cell.add_argument("--use-raw", action="store_true")
    single_cell.add_argument("--obs-preview-limit", type=int, default=10)
    single_cell.add_argument("--var-preview-limit", type=int, default=10)
    single_cell.add_argument("--label", default="")

    supervised = subparsers.add_parser(
        "run-supervised-expression",
        help="Run generic supervised modeling on a gene-level matrix.",
    )
    supervised.add_argument("--matrix-path", default="")
    supervised.add_argument("--table-text", default="")
    supervised.add_argument("--row-id-column", default="")
    supervised.add_argument("--delimiter", default="")
    supervised.add_argument("--sheet-name", default="")
    supervised.add_argument("--header-row", type=int, default=1)
    supervised.add_argument("--skip-rows", type=int, default=0)
    supervised.add_argument("--transpose", action="store_true")
    supervised.add_argument("--analysis-axis", default="columns")
    supervised.add_argument("--metadata-path", required=True)
    supervised.add_argument("--metadata-table-text", default="")
    supervised.add_argument("--metadata-delimiter", default="")
    supervised.add_argument("--metadata-sheet-name", default="")
    supervised.add_argument("--metadata-header-row", type=int, default=1)
    supervised.add_argument("--metadata-skip-rows", type=int, default=0)
    supervised.add_argument("--sample-column", default="sample")
    supervised.add_argument("--target-column", required=True)
    supervised.add_argument("--task", default="classification")
    supervised.add_argument("--model-family", default="logistic_regression")
    supervised.add_argument("--split-strategy", default="train_test")
    supervised.add_argument("--group-column", default="")
    supervised.add_argument("--test-size", type=float, default=0.3)
    supervised.add_argument("--random-seed", type=int, default=0)
    supervised.add_argument("--top-n-features", type=int, default=25)
    supervised.add_argument("--label", default="")

    cluster_agreement = subparsers.add_parser(
        "evaluate-cluster-agreement",
        help="Measure held-out cluster agreement across train/test splits.",
    )
    cluster_agreement.add_argument("--matrix-path", default="")
    cluster_agreement.add_argument("--table-text", default="")
    cluster_agreement.add_argument("--row-id-column", default="")
    cluster_agreement.add_argument("--delimiter", default="")
    cluster_agreement.add_argument("--sheet-name", default="")
    cluster_agreement.add_argument("--header-row", type=int, default=1)
    cluster_agreement.add_argument("--skip-rows", type=int, default=0)
    cluster_agreement.add_argument("--transpose", action="store_true")
    cluster_agreement.add_argument("--analysis-axis", default="columns")
    cluster_agreement.add_argument("--cluster-count", type=int, default=3)
    cluster_agreement.add_argument("--iterations", type=int, default=50)
    cluster_agreement.add_argument("--train-fraction", type=float, default=0.7)
    cluster_agreement.add_argument("--metric", default="correlation")
    cluster_agreement.add_argument("--linkage", default="average")
    cluster_agreement.add_argument("--random-seed", type=int, default=0)
    cluster_agreement.add_argument("--label", default="")

    compare_de = subparsers.add_parser(
        "compare-de-results",
        help="Compare two or more differential-expression result specifications.",
    )
    compare_de.add_argument("--result-spec", action="append", required=True)
    compare_de.add_argument("--gene-source", default="all_significant")
    compare_de.add_argument("--label", default="")

    integrate = subparsers.add_parser("integrate-gene-level-results", help="Integrate canonical differential-result JSON specifications.")
    integrate.add_argument("--result-spec", action="append", required=True)
    integrate.add_argument("--label", default="")

    papers = subparsers.add_parser("search-papers", help="Run a Europe PMC search.")
    papers.add_argument("--query", required=True)
    papers.add_argument("--limit", type=int, default=5)
    papers.add_argument("--open-access-only", action="store_true")

    disease = subparsers.add_parser("gene-disease", help="Retrieve Open Targets disease associations for one gene.")
    disease.add_argument("--gene", required=True)
    disease.add_argument("--species", default="")
    disease.add_argument("--limit", type=int, default=10)

    workflow = subparsers.add_parser("investigate-gene-set", help="Run the gene set investigation workflow.")
    workflow.add_argument("--genes", nargs="+", required=True)
    workflow.add_argument("--question", default="")
    workflow.add_argument("--label", default="")
    workflow.add_argument("--species", default="")

    report_show = subparsers.add_parser("session-report", help="Show one saved markdown report for a session if one exists.")
    report_show.add_argument("--session-id", required=True)
    report_show.add_argument("--report-id", default="")

    report_list = subparsers.add_parser("list-reports", help="List saved markdown reports for a session.")
    report_list.add_argument("--session-id", required=True)

    runtime_write = subparsers.add_parser("runtime-write", help="Write the desktop runtime manifest for local testing.")
    runtime_write.add_argument("--workspace-root", default="")
    runtime_write.add_argument("--active-session-id", default="")
    runtime_write.add_argument("--disable-capture", action="store_true")
    runtime_write.add_argument("--ui-name", default="GLESKit Desktop")

    subparsers.add_parser("runtime-clear", help="Clear the desktop runtime manifest.")

    serve = subparsers.add_parser("serve", help="Run the MCP server if the Python MCP package is installed.")
    serve.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    serve.add_argument("--host", default="", help="Host interface for HTTP transports.")
    serve.add_argument("--port", type=int, default=None, help="Port for HTTP transports.")
    serve.add_argument("--streamable-http-path", default="", help="Path for streamable HTTP endpoint.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(Path(args.project_root))
    service = _LazyService(Path(args.project_root))
    direct_workspace = (
        WorkspaceService(WorkspaceStore(Path(args.workspace_root)), settings=settings)
        if args.workspace_root
        else None
    )

    try:
        if args.command == "workspace-status":
            payload = (
                {
                    "available": True,
                    "workspace_root": str(Path(args.workspace_root).resolve()),
                    "active_session_id": None,
                    "capture_enabled": False,
                    "mode": "direct",
                }
                if direct_workspace
                else service.workspace_status()
            )
        elif args.command == "reference-status":
            from gleskit_mcp.reference_data import ReferenceCatalog
            payload = ReferenceCatalog(settings).status()
        elif args.command == "update-references":
            payload = service.update_references()
        elif args.command == "create-session":
            if direct_workspace:
                payload = direct_workspace.create_session(
                    title=args.title,
                    description=_required_text(args.description, "description"),
                    species=args.species or settings.default_species,
                    tags=args.tags,
                )
            else:
                payload = service.create_session(
                    title=args.title,
                    description=_required_text(args.description, "description"),
                    species=args.species or None,
                    tags=args.tags,
                )
        elif args.command == "update-session":
            title = _optional_text(args.title)
            description = _optional_text(args.description)
            if title is None and description is None:
                raise SystemExit("Provide --title, --description, or both.\n")
            payload = (
                direct_workspace.update_session(
                    args.session_id,
                    title=title,
                    description=description,
                )
                if direct_workspace
                else service.update_session(
                    args.session_id,
                    title=title,
                    description=description,
                )
            )
        elif args.command == "add-note":
            body_markdown = _required_text(args.body_markdown, "body_markdown")
            title = _clean_note_title(args.title, body_markdown)
            payload = (
                direct_workspace.add_note(
                    args.session_id,
                    title=title,
                    body_markdown=body_markdown,
                    author=args.author,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
                if direct_workspace
                else service.add_session_note(
                    args.session_id,
                    title=title,
                    body_markdown=body_markdown,
                    author=args.author,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
            )
        elif args.command == "list-notes":
            payload = (
                direct_workspace.list_notes(args.session_id)
                if direct_workspace
                else service.list_session_notes(args.session_id)
            )
        elif args.command == "note":
            payload = (
                direct_workspace.get_note(args.session_id, args.note_id)
                if direct_workspace
                else service.get_session_note(args.session_id, args.note_id)
            )
        elif args.command == "list-report-templates":
            payload = service.list_report_templates()
        elif args.command == "report-template":
            payload = service.get_report_template(args.template_id)
        elif args.command == "create-report":
            payload = (
                direct_workspace.create_report(
                    args.session_id,
                    body_markdown=_required_text(args.body_markdown, "body_markdown"),
                    template_id=_required_text(args.template_id, "template_id"),
                    title=args.title,
                    author=args.author,
                    focus=args.focus,
                    custom_guidance=args.custom_guidance,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
                if direct_workspace
                else service.create_session_report(
                    args.session_id,
                    body_markdown=_required_text(args.body_markdown, "body_markdown"),
                    template_id=_required_text(args.template_id, "template_id"),
                    title=args.title,
                    author=args.author,
                    focus=args.focus,
                    custom_guidance=args.custom_guidance,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
            )
        elif args.command == "update-report":
            payload = (
                direct_workspace.update_report(
                    args.session_id,
                    args.report_id,
                    title=args.title,
                    body_markdown=_required_text(args.body_markdown, "body_markdown"),
                    author=args.author,
                    focus=args.focus,
                    template_id=args.template_id,
                    custom_guidance=args.custom_guidance,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
                if direct_workspace
                else service.update_session_report(
                    args.session_id,
                    args.report_id,
                    title=args.title,
                    body_markdown=_required_text(args.body_markdown, "body_markdown"),
                    author=args.author,
                    focus=args.focus,
                    template_id=args.template_id,
                    custom_guidance=args.custom_guidance,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
            )
        elif args.command == "delete-report":
            payload = (
                direct_workspace.delete_report(args.session_id, args.report_id)
                if direct_workspace
                else service.delete_session_report(args.session_id, args.report_id)
            )
        elif args.command == "delete-reports":
            payload = (
                direct_workspace.delete_reports(args.session_id, args.report_ids)
                if direct_workspace
                else service.delete_session_reports(args.session_id, args.report_ids)
            )
        elif args.command == "update-note":
            payload = (
                direct_workspace.update_note(
                    args.session_id,
                    args.note_id,
                    title=args.title,
                    body_markdown=args.body_markdown,
                    author=args.author,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
                if direct_workspace
                else service.update_session_note(
                    args.session_id,
                    args.note_id,
                    title=args.title,
                    body_markdown=args.body_markdown,
                    author=args.author,
                    linked_artifact_ids=args.linked_artifact_ids,
                    tags=args.tags,
                )
            )
        elif args.command == "delete-note":
            payload = (
                direct_workspace.delete_note(args.session_id, args.note_id)
                if direct_workspace
                else service.delete_session_note(args.session_id, args.note_id)
            )
        elif args.command == "delete-notes":
            payload = (
                direct_workspace.delete_notes(args.session_id, args.note_ids)
                if direct_workspace
                else service.delete_session_notes(args.session_id, args.note_ids)
            )
        elif args.command == "list-sessions":
            payload = (
                direct_workspace.list_sessions(query=args.query, status=args.status or None)
                if direct_workspace
                else service.list_sessions(query=args.query, status=args.status)
            )
        elif args.command == "session-summary":
            payload = (
                direct_workspace.get_session_summary(args.session_id)
                if direct_workspace
                else service.get_session_summary(args.session_id)
            )
        elif args.command == "session-snapshot":
            # One process, one payload: the desktop UI needs summary, artifacts,
            # and uploads together for a single session selection.
            workspace = direct_workspace
            if workspace is not None:
                payload = {
                    "session_id": args.session_id,
                    "summary": workspace.get_session_summary(args.session_id),
                    "artifacts": workspace.list_artifacts(args.session_id),
                    "uploads": workspace.list_uploads(args.session_id),
                }
            else:
                payload = {
                    "session_id": args.session_id,
                    "summary": service.get_session_summary(args.session_id),
                    "artifacts": service.list_artifacts(args.session_id),
                    "uploads": service.list_uploads(args.session_id),
                }
        elif args.command == "list-cohorts":
            payload = (
                direct_workspace.list_cohorts()
                if direct_workspace
                else service.list_cohorts()
            )
        elif args.command == "list-cohort-providers":
            payload = (
                direct_workspace.list_cohort_providers()
                if direct_workspace
                else service.list_cohort_providers()
            )
        elif args.command == "cohort-catalog":
            payload = (
                direct_workspace.list_cohort_catalog(args.provider_id)
                if direct_workspace
                else service.list_cohort_catalog(args.provider_id)
            )
        elif args.command == "cohort":
            payload = (
                direct_workspace.get_cohort(args.cohort_id)
                if direct_workspace
                else service.get_cohort(args.cohort_id)
            )
        elif args.command == "download-cohort":
            payload = (
                direct_workspace.download_cohort(
                    title=args.title,
                    provider_id=args.provider_id,
                    dataset_id=args.dataset_id,
                    tissue=args.tissue,
                    species=args.species or settings.default_species,
                    sample_limit=args.sample_limit,
                    tags=args.tags,
                )
                if direct_workspace
                else service.download_cohort(
                    title=args.title,
                    provider_id=args.provider_id,
                    dataset_id=args.dataset_id,
                    tissue=args.tissue,
                    species=args.species or settings.default_species,
                    sample_limit=args.sample_limit,
                    tags=args.tags,
                )
            )
        elif args.command == "create-cohort":
            counts_path = _optional_text(args.counts_path)
            metadata_path = _optional_text(args.metadata_path)
            if not counts_path or not metadata_path:
                raise SystemExit("Provide counts and metadata paths.\n")
            payload = (
                direct_workspace.create_cohort(
                    title=args.title,
                    counts_path=counts_path,
                    metadata_path=metadata_path,
                    provider_id=args.provider_id,
                    source_kind=args.source_kind,
                    species=args.species or settings.default_species,
                    transpose=args.transpose,
                    sample_column=args.sample_column,
                    group_column=args.group_column,
                    min_count=args.min_count,
                    sample_limit=args.sample_limit,
                    source_counts_path=args.source_counts_path,
                    source_metadata_path=args.source_metadata_path,
                    tags=args.tags,
                )
                if direct_workspace
                else service.create_cohort(
                    title=args.title,
                    counts_path=counts_path,
                    metadata_path=metadata_path,
                    provider_id=args.provider_id,
                    source_kind=args.source_kind,
                    species=args.species or None,
                    transpose=args.transpose,
                    sample_column=args.sample_column,
                    group_column=args.group_column,
                    min_count=args.min_count,
                    sample_limit=args.sample_limit,
                    source_counts_path=args.source_counts_path,
                    source_metadata_path=args.source_metadata_path,
                    tags=args.tags,
                )
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
        elif args.command == "update-cohort":
            payload = (
                direct_workspace.update_cohort(args.cohort_id, title=args.title)
                if direct_workspace
                else service.update_cohort(args.cohort_id, title=args.title)
            )
        elif args.command == "delete-cohort":
            payload = (
                direct_workspace.delete_cohort(args.cohort_id)
                if direct_workspace
                else service.delete_cohort(args.cohort_id)
            )
        elif args.command == "create-session-from-cohort":
            payload = (
                direct_workspace.create_session_from_cohort(
                    cohort_id=args.cohort_id,
                    title=args.title,
                    description=args.description,
                    species=args.species or settings.default_species,
                    tags=args.tags,
                )
                if direct_workspace
                else service.create_session_from_cohort(
                    cohort_id=args.cohort_id,
                    title=args.title,
                    description=args.description,
                    species=args.species or None,
                    tags=args.tags,
                )
            )
        elif args.command == "artifacts":
            payload = (
                direct_workspace.list_artifacts(args.session_id, kind=args.kind or None)
                if direct_workspace
                else service.list_artifacts(args.session_id, kind=args.kind or None)
            )
        elif args.command == "uploads":
            payload = (
                direct_workspace.list_uploads(args.session_id)
                if direct_workspace
                else service.list_uploads(args.session_id)
            )
        elif args.command == "artifact":
            payload = (
                direct_workspace.get_artifact(args.session_id, args.artifact_id)
                if direct_workspace
                else service.get_artifact(args.session_id, args.artifact_id)
            )
        elif args.command == "delete-artifacts":
            payload = (
                direct_workspace.delete_artifacts(args.session_id, args.artifact_ids)
                if direct_workspace
                else service.delete_session_artifacts(args.session_id, args.artifact_ids)
            )
        elif args.command == "upload":
            payload = (
                direct_workspace.get_upload(args.session_id, args.upload_id)
                if direct_workspace
                else service.get_upload(args.session_id, args.upload_id)
            )
        elif args.command == "delete-uploads":
            payload = (
                direct_workspace.delete_uploads(args.session_id, args.upload_ids)
                if direct_workspace
                else service.delete_session_uploads(args.session_id, args.upload_ids)
            )
        elif args.command == "create-upload":
            payload = (
                direct_workspace.create_upload(
                    args.session_id,
                    data_path=args.data_path,
                    source_mode=args.source_mode,
                    title=args.title,
                    source_path=args.source_path,
                    stored_path=args.stored_path,
                    file_name=args.file_name,
                    mime_type=args.mime_type,
                )
                if direct_workspace
                else service.create_session_upload(
                    args.session_id,
                    data_path=args.data_path,
                    source_mode=args.source_mode,
                    title=args.title,
                    source_path=args.source_path,
                    stored_path=args.stored_path,
                    file_name=args.file_name,
                    mime_type=args.mime_type,
                )
            )
        elif args.command == "create-figure":
            payload = (
                direct_workspace.create_figure(
                    args.session_id,
                    args.artifact_id,
                    figure_type=args.figure_type,
                    title=args.title,
                    top_n=args.top_n,
                )
                if direct_workspace
                else service.create_figure(
                    args.session_id,
                    args.artifact_id,
                    figure_type=args.figure_type,
                    title=args.title,
                    top_n=args.top_n,
                )
            )
        elif args.command == "archive-session":
            payload = (
                direct_workspace.archive_session(args.session_id)
                if direct_workspace
                else service.archive_session(args.session_id)
            )
        elif args.command == "plan-delete-sessions":
            payload = (
                direct_workspace.plan_delete_sessions(args.session_ids)
                if direct_workspace
                else service.plan_delete_sessions(args.session_ids)
            )
        elif args.command == "delete-sessions":
            payload = (
                direct_workspace.delete_sessions(args.session_ids)
                if direct_workspace
                else service.delete_sessions(args.session_ids)
            )
        elif args.command == "create-session-from-artifacts":
            if direct_workspace:
                payload = direct_workspace.create_session_from_artifacts(
                    title=args.title,
                    description=args.description,
                    species=args.species or settings.default_species,
                    tags=args.tags,
                    sources=_parse_sources(args.sources),
                )
            else:
                payload = service.create_session_from_artifacts(
                    title=args.title,
                    description=args.description,
                    species=args.species or None,
                    tags=args.tags,
                    sources=_parse_sources(args.sources),
                )
        elif args.command == "resolve-gene":
            payload = service.resolve_gene(query=args.query, species=args.species or None)
        elif args.command == "resolve-gene-set":
            payload = service.resolve_gene_set(genes=args.genes, species=args.species or None, limit=args.limit)
        elif args.command == "gene-summary":
            payload = service.gene_summary(query=args.query, species=args.species or None)
        elif args.command == "resolve-protein":
            payload = service.resolve_protein(query=args.query, species=args.species or None)
        elif args.command == "protein-summary":
            payload = service.protein_summary(query=args.query, species=args.species or None)
        elif args.command == "drug-target":
            payload = service.drug_target_context(
                gene=args.gene,
                species=args.species or None,
                limit=args.limit,
            )
        elif args.command == "variant-traits":
            payload = service.variant_trait_associations(
                gene=args.gene,
                species=args.species or None,
                limit=args.limit,
            )
        elif args.command == "interaction-network":
            payload = service.interaction_network(genes=args.genes, species=args.species or None, limit=args.limit)
        elif args.command == "pathway-context":
            payload = service.pathway_context(genes=args.genes, species=args.species or None, limit=args.limit)
        elif args.command == "expression-context":
            payload = service.expression_context(gene=args.gene, species=args.species or None, limit=args.limit)
        elif args.command == "analyze-rnaseq":
            payload = service.analyze_rnaseq_result(
                result_path=args.result_path,
                table_text=args.table_text,
                sheet_name=args.sheet_name,
                header_row=args.header_row,
                skip_rows=args.skip_rows,
                species=args.species or None,
                label=args.label,
                comparison=args.comparison,
                gene_column=args.gene_column,
                log2_fold_change_column=args.log2_fold_change_column,
                padj_column=args.padj_column,
                p_value_column=args.p_value_column,
                base_mean_column=args.base_mean_column,
                dispersion_column=args.dispersion_column,
                stat_column=args.stat_column,
                padj_threshold=args.padj_threshold,
                p_value_threshold=args.p_value_threshold,
                base_mean_threshold=args.base_mean_threshold,
                log2_fold_change_threshold=args.log2_fold_change_threshold,
                top_n=args.top_n,
            )
        elif args.command == "analyze-differential-result":
            payload = service.analyze_differential_result(
                modality=args.modality,
                positive_condition=args.positive_condition,
                reference_condition=args.reference_condition,
                result_scope=args.result_scope,
                result_path=args.result_path,
                table_text=args.table_text,
                sheet_name=args.sheet_name,
                header_row=args.header_row,
                skip_rows=args.skip_rows,
                species=args.species or None,
                label=args.label,
                identifier_column=args.identifier_column,
                effect_column=args.effect_column,
                padj_column=args.padj_column,
                p_value_column=args.p_value_column,
                stat_column=args.stat_column,
                significance_mode=args.significance_mode,
                padj_threshold=args.padj_threshold,
                p_value_threshold=args.p_value_threshold,
                log2_fold_change_threshold=args.log2_fold_change_threshold,
                background_genes=args.background_gene,
                identifier_mapping=_parse_identifier_mapping(args.identifier_mapping),
                identifier_mapping_path=args.identifier_mapping_path,
                mapping_identifier_column=args.mapping_identifier_column,
                mapping_gene_column=args.mapping_gene_column,
            )
        elif args.command == "analyze-deseq2":
            payload = service.analyze_deseq2_counts(
                counts_path=args.counts_path,
                counts_sheet_name=args.counts_sheet_name,
                counts_header_row=args.counts_header_row,
                counts_skip_rows=args.counts_skip_rows,
                sample_metadata_path=args.sample_metadata_path,
                sample_metadata_sheet_name=args.sample_metadata_sheet_name,
                sample_metadata_header_row=args.sample_metadata_header_row,
                sample_metadata_skip_rows=args.sample_metadata_skip_rows,
                sample_metadata_text=args.sample_metadata_text,
                sample_group_map=args.sample_group_map or None,
                species=args.species or None,
                label=args.label,
                comparison=args.comparison,
                gene_column=args.gene_column,
                sample_column=args.sample_column,
                condition_column=args.condition_column,
                condition_a=args.condition_a,
                condition_b=args.condition_b,
                design_formula=args.design_formula,
                min_count=args.min_count,
                padj_threshold=args.padj_threshold,
                p_value_threshold=args.p_value_threshold,
                base_mean_threshold=args.base_mean_threshold,
                significance_mode=args.significance_mode,
                log2_fold_change_threshold=args.log2_fold_change_threshold,
                top_n=args.top_n,
                lfc_shrinkage=args.lfc_shrinkage,
            )
        elif args.command == "enrich-genes":
            payload = service.enrich_gene_set(
                genes=args.genes,
                species=args.species or None,
                label=args.label,
                database=args.database,
                library=args.library,
                backend=args.backend,
                background_genes=args.background_genes,
                simplify_go_similarity=args.simplify_go_similarity,
            )
        elif args.command == "gene-set-disease":
            payload = service.gene_set_disease_context(
                genes=args.genes,
                species=args.species or None,
                label=args.label,
                database=args.database,
                limit=args.limit,
            )
        elif args.command == "compare-programs":
            payload = service.compare_gene_programs(
                programs=_parse_program_arguments(args.program),
                species=args.species or None,
                label=args.label,
                database=args.database,
                limit=args.limit,
            )
        elif args.command == "ranked-gsea":
            payload = service.ranked_gene_set_gsea(
                gene_scores=_parse_gene_score_arguments(args.gene_score),
                species=args.species or None,
                label=args.label,
                limit=args.limit,
                database=args.database,
            )
        elif args.command == "run-expression-statistics":
            payload = service.run_expression_statistics(
                analysis=args.analysis,
                source_path=args.source_path,
                table_text=args.table_text,
                row_id_column=args.row_id_column,
                delimiter=args.delimiter,
                sheet_name=args.sheet_name,
                header_row=args.header_row,
                skip_rows=args.skip_rows,
                transpose=args.transpose,
                analysis_axis=args.analysis_axis,
                target_axis=args.target_axis,
                summary_stat=args.summary_stat,
                annotation_path=args.annotation_path,
                annotation_table_text=args.annotation_table_text,
                annotation_delimiter=args.annotation_delimiter,
                annotation_sheet_name=args.annotation_sheet_name,
                annotation_header_row=args.annotation_header_row,
                annotation_skip_rows=args.annotation_skip_rows,
                annotation_key_column=args.annotation_key_column,
                annotation_value_column=args.annotation_value_column,
                method=args.method,
                value_column=args.value_column,
                scope=args.scope,
                group_column=args.group_column,
                key_column=args.key_column,
                pair_column=args.pair_column,
                annotation_group_column=args.annotation_group_column,
                test=args.test,
                observation_unit=args.observation_unit,
                value_transform=args.value_transform,
                pseudocount=args.pseudocount,
                success_count=args.success_count,
                trial_count=args.trial_count,
                null_probability=args.null_probability,
                alpha=args.alpha,
                correction_methods=args.correction_method,
                label=args.label,
            )
        elif args.command == "analyze-single-cell":
            payload = service.analyze_single_cell_rnaseq(
                analysis=args.analysis,
                h5ad_path=args.h5ad_path,
                deg_table_path=args.deg_table_path,
                gene=args.gene,
                padj_threshold=args.padj_threshold,
                gene_column=args.gene_column,
                cell_type_column=args.cell_type_column,
                padj_column=args.padj_column,
                p_value_column=args.p_value_column,
                log2_fold_change_column=args.log2_fold_change_column,
                sheet_names=args.sheet_name or None,
                sample_column=args.sample_column,
                condition_column=args.condition_column,
                cell_type_values=args.cell_type_value or None,
                layer=args.layer,
                use_raw=args.use_raw,
                obs_preview_limit=args.obs_preview_limit,
                var_preview_limit=args.var_preview_limit,
                label=args.label,
            )
        elif args.command == "run-supervised-expression":
            payload = service.run_supervised_expression_model(
                matrix_path=args.matrix_path,
                table_text=args.table_text,
                row_id_column=args.row_id_column,
                delimiter=args.delimiter,
                sheet_name=args.sheet_name,
                header_row=args.header_row,
                skip_rows=args.skip_rows,
                transpose=args.transpose,
                analysis_axis=args.analysis_axis,
                metadata_path=args.metadata_path,
                metadata_table_text=args.metadata_table_text,
                metadata_delimiter=args.metadata_delimiter,
                metadata_sheet_name=args.metadata_sheet_name,
                metadata_header_row=args.metadata_header_row,
                metadata_skip_rows=args.metadata_skip_rows,
                sample_column=args.sample_column,
                target_column=args.target_column,
                task=args.task,
                model_family=args.model_family,
                split_strategy=args.split_strategy,
                group_column=args.group_column,
                test_size=args.test_size,
                random_seed=args.random_seed,
                top_n_features=args.top_n_features,
                label=args.label,
            )
        elif args.command == "evaluate-cluster-agreement":
            payload = service.evaluate_cluster_agreement(
                matrix_path=args.matrix_path,
                table_text=args.table_text,
                row_id_column=args.row_id_column,
                delimiter=args.delimiter,
                sheet_name=args.sheet_name,
                header_row=args.header_row,
                skip_rows=args.skip_rows,
                transpose=args.transpose,
                analysis_axis=args.analysis_axis,
                cluster_count=args.cluster_count,
                iterations=args.iterations,
                train_fraction=args.train_fraction,
                metric=args.metric,
                linkage=args.linkage,
                random_seed=args.random_seed,
                label=args.label,
            )
        elif args.command == "compare-de-results":
            payload = service.compare_differential_expression_results(
                results=_parse_result_spec_arguments(args.result_spec),
                gene_source=args.gene_source,
                label=args.label,
            )
        elif args.command == "integrate-gene-level-results":
            payload = service.integrate_gene_level_results(
                results=_parse_result_spec_arguments(args.result_spec),
                label=args.label,
            )
        elif args.command == "search-papers":
            payload = service.search_papers(
                query=args.query,
                limit=args.limit,
                open_access_only=args.open_access_only,
            )
        elif args.command == "gene-disease":
            payload = service.gene_disease_associations(
                gene=args.gene,
                species=args.species or None,
                limit=args.limit,
            )
        elif args.command == "investigate-gene-set":
            payload = service.investigate_gene_set(
                genes=args.genes,
                question=args.question,
                label=args.label,
                species=args.species or None,
            )
        elif args.command == "session-report":
            payload = (
                direct_workspace.get_session_report(args.session_id, args.report_id or None)
                if direct_workspace
                else service.get_session_report(args.session_id, args.report_id or None)
            )
        elif args.command == "list-reports":
            payload = (
                direct_workspace.list_reports(args.session_id)
                if direct_workspace
                else service.list_session_reports(args.session_id)
            )
        elif args.command == "runtime-write":
            runtime = write_runtime_manifest(
                settings.runtime_manifest_path,
                workspace_root=Path(args.workspace_root).resolve() if args.workspace_root else settings.workspace_default_root,
                active_session_id=args.active_session_id or None,
                capture_enabled=not args.disable_capture,
                ui_name=args.ui_name,
            )
            payload = {
                "runtime_manifest_path": str(settings.runtime_manifest_path),
                "workspace_root": str(runtime.workspace_root),
                "active_session_id": runtime.active_session_id,
                "capture_enabled": runtime.capture_enabled,
                "updated_at": runtime.updated_at,
            }
        elif args.command == "runtime-clear":
            clear_runtime_manifest(settings.runtime_manifest_path)
            payload = {"runtime_manifest_path": str(settings.runtime_manifest_path), "cleared": True}
        elif args.command == "serve":
            from gleskit_mcp.mcp_server import main as serve_main

            return serve_main(
                transport=args.transport,
                project_root=args.project_root,
                host=args.host or None,
                port=args.port,
                streamable_http_path=args.streamable_http_path or None,
            )
        else:  # pragma: no cover
            parser.error(f"Unsupported command: {args.command}")
            return 2
    except (ProviderError, WorkspaceUnavailableError, ValueError) as exc:
        parser.exit(status=1, message=f"{exc}\n")
    except FileNotFoundError as exc:
        parser.exit(status=1, message=f"{exc}\n")

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _required_text(value: str | None, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise SystemExit(f"{field_name} must not be empty.\n")
    return cleaned


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _download_url_to_file(url: str, destination: Path) -> Path:
    requested = str(url or "").strip()
    if not requested.startswith(("http://", "https://")):
        raise SystemExit("Cohort URL downloads support only http:// and https:// URLs.\n")
    request = Request(requested, headers={"User-Agent": "GLESKit/0.2"})
    try:
        with urlopen(request, timeout=60) as response:
            data = response.read()
    except HTTPError as exc:
        raise SystemExit(f"Cohort download HTTP error: {exc.code}\n") from exc
    except URLError as exc:
        raise SystemExit(f"Cohort download network error: {exc.reason}\n") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


def _parse_program_arguments(values: list[str]) -> dict[str, list[str]]:
    programs: dict[str, list[str]] = {}
    for raw in values:
        token = str(raw or "").strip()
        if ":" not in token:
            raise ValueError("Each --program value must use the form label:GENE1,GENE2,...")
        label, gene_text = token.split(":", 1)
        cleaned_label = label.strip()
        genes = [item.strip() for item in gene_text.split(",") if item.strip()]
        if not cleaned_label or not genes:
            raise ValueError("Each --program value must include both a label and at least one gene.")
        programs[cleaned_label] = genes
    return programs


def _parse_gene_score_arguments(values: list[str]) -> dict[str, float]:
    gene_scores: dict[str, float] = {}
    for raw in values:
        token = str(raw or "").strip()
        if "=" not in token:
            raise ValueError("Each --gene-score value must use the form GENE=SCORE.")
        gene, score_text = token.split("=", 1)
        cleaned_gene = gene.strip()
        if not cleaned_gene:
            raise ValueError("Each --gene-score value must include a gene symbol before '='.")
        try:
            score = float(score_text.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid score '{score_text.strip()}' for gene '{cleaned_gene}'.") from exc
        gene_scores[cleaned_gene] = score
    return gene_scores


def _parse_result_spec_arguments(values: list[str]) -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    for raw in values:
        try:
            parsed = json.loads(str(raw or "").strip())
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Each --result-spec value must be a JSON object such as "
                '\'{"label":"A","result_path":"/path/to/results.tsv"}\'.'
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("Each --result-spec value must decode to a JSON object.")
        specs.append(parsed)
    return specs


def _parse_identifier_mapping(value: str) -> dict[str, list[str]] | None:
    if not str(value or "").strip():
        return None
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("--identifier-mapping must be a JSON object mapping identifiers to gene symbols or lists of symbols.") from exc
    if not isinstance(raw, dict):
        raise ValueError("--identifier-mapping must decode to a JSON object.")
    parsed: dict[str, list[str]] = {}
    for identifier, genes in raw.items():
        values = genes if isinstance(genes, list) else [genes]
        parsed[str(identifier)] = [str(gene) for gene in values if str(gene).strip()]
    return parsed


def _parse_sources(tokens: list[str]) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for token in tokens:
        source = str(token).strip()
        if ":" not in source:
            raise SystemExit(f"Invalid source '{source}'. Expected session_id:artifact_id.\n")
        session_id, artifact_id = source.split(":", 1)
        session_id = session_id.strip()
        artifact_id = artifact_id.strip()
        if not session_id or not artifact_id:
            raise SystemExit(f"Invalid source '{source}'. Expected session_id:artifact_id.\n")
        parsed.append({"session_id": session_id, "artifact_id": artifact_id})
    return parsed


def _clean_note_title(title: str | None, body_markdown: str) -> str:
    cleaned = str(title or "").strip()
    if cleaned:
        return cleaned
    for line in str(body_markdown or "").splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            return candidate[:80]
    return "Untitled note"
