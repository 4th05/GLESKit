from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from urllib.request import urlopen
from unittest.mock import patch

from mcp.types import CallToolResult

from gleskit_mcp.analysis.r_runner import RJsonScriptRunner
from gleskit_mcp.config import load_settings
from gleskit_mcp.figures import FigureCatalog
from gleskit_mcp.mcp_server import (
    _FigureDownloadHttpServer,
    _FigureDownloadRegistry,
    _auto_figure_tool_result,
    _figure_tool_result,
    _normalize_public_base,
    _public_base_from_request,
)
from gleskit_mcp.models import Artifact, utc_now_iso
from gleskit_mcp.services import GLESKitService
from gleskit_mcp.store import WorkspaceStore
from gleskit_mcp.workspace import WorkspaceService


class FigureWorkflowTests(unittest.TestCase):
    def test_normalize_public_base_strips_mcp_path(self) -> None:
        self.assertEqual(
            _normalize_public_base("https://example.org/mcp", streamable_http_path="/mcp"),
            "https://example.org",
        )
        self.assertEqual(
            _normalize_public_base("https://example.org/base/mcp", streamable_http_path="/mcp"),
            "https://example.org/base",
        )

    def test_normalize_public_base_strips_download_path(self) -> None:
        self.assertEqual(
            _normalize_public_base("https://example.org/figure-download", streamable_http_path="/figure-download"),
            "https://example.org",
        )

    def test_public_base_from_request_falls_back_to_request_url_netloc(self) -> None:
        class _URL:
            scheme = "https"
            netloc = "abc123.ngrok-free.app"
            path = "/mcp"

        class _Request:
            url = _URL()
            headers = None

        self.assertEqual(
            _public_base_from_request(_Request(), normalized_stream_path="/mcp"),
            "https://abc123.ngrok-free.app",
        )

    def test_public_base_from_request_prefers_forwarded_headers(self) -> None:
        class _URL:
            scheme = "http"
            netloc = "127.0.0.1:8000"
            path = "/mcp"

        class _Request:
            url = _URL()
            headers = {
                "x-forwarded-proto": "https",
                "x-forwarded-host": "public.example.org",
            }

        self.assertEqual(
            _public_base_from_request(_Request(), normalized_stream_path="/mcp"),
            "https://public.example.org",
        )

    def test_public_base_from_request_uses_origin_header(self) -> None:
        class _URL:
            scheme = ""
            netloc = ""
            path = "/mcp"

        class _Request:
            url = _URL()
            headers = {
                "origin": "https://origin.example.org",
            }

        self.assertEqual(
            _public_base_from_request(_Request(), normalized_stream_path="/mcp"),
            "https://origin.example.org",
        )

    def test_public_base_from_request_uses_referer_header(self) -> None:
        class _URL:
            scheme = ""
            netloc = ""
            path = "/mcp"

        class _Request:
            url = _URL()
            headers = {
                "referer": "https://refer.example.org/mcp",
            }

        self.assertEqual(
            _public_base_from_request(_Request(), normalized_stream_path="/mcp"),
            "https://refer.example.org",
        )

    def test_public_base_from_request_uses_base_url(self) -> None:
        class _URL:
            scheme = ""
            netloc = ""
            path = "/mcp"

        class _Request:
            url = _URL()
            base_url = "https://base.example.org/mcp"
            headers = {}

        self.assertEqual(
            _public_base_from_request(_Request(), normalized_stream_path="/mcp"),
            "https://base.example.org",
        )

    def test_companion_download_server_serves_registered_bytes(self) -> None:
        registry = _FigureDownloadRegistry(ttl_seconds=600, max_entries=32)
        server = _FigureDownloadHttpServer(
            registry,
            bind_host="127.0.0.1",
            port=0,
            path_prefix="/figure-download",
        )
        self.addCleanup(server.stop)

        token, uri = registry.register(
            data=b"png-bytes",
            mime_type="image/png",
            filename="test-figure.png",
        )

        self.assertTrue(uri.startswith("gleskit://figure-download/"))
        download_url = server.url_for_token(token)
        self.assertTrue(download_url.startswith("http://127.0.0.1:"))
        with urlopen(download_url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.read(), b"png-bytes")

    def test_companion_download_server_uses_public_base_override(self) -> None:
        registry = _FigureDownloadRegistry(ttl_seconds=600, max_entries=32)
        server = _FigureDownloadHttpServer(
            registry,
            bind_host="127.0.0.1",
            port=0,
            public_base_url="https://abc123.ngrok-free.app/figure-download",
            path_prefix="/figure-download",
        )
        self.addCleanup(server.stop)

        token, _uri = registry.register(
            data=b"png-bytes",
            mime_type="image/png",
            filename="test-figure.png",
        )
        self.assertEqual(
            server.url_for_token(token),
            f"https://abc123.ngrok-free.app/figure-download/{token}",
        )

    def test_enrichment_artifact_exposes_enrichplot_options(self) -> None:
        catalog = FigureCatalog()
        artifact = Artifact(
            artifact_id="artifact_test",
            session_id="session_test",
            kind="enrichment",
            title="Clock enrichment",
            summary="Clock terms",
            created_at=utc_now_iso(),
            provider="yulab_clusterprofiler",
            payload={
                "genes": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                "sources": ["GO:BP", "REAC"],
                "terms": [
                    {"term_id": "GO:0007623", "term_name": "circadian rhythm", "source": "GO:BP", "p_value": 1e-8, "intersection_size": 4},
                    {"term_id": "R-HSA-1368108", "term_name": "Circadian Clock", "source": "REAC", "p_value": 2e-6, "intersection_size": 3},
                ],
            },
        )

        figure_types = [row["figure_type"] for row in catalog.figure_options(artifact)]
        self.assertIn("enrichment_barplot", figure_types)
        self.assertIn("enrichment_emapplot", figure_types)
        self.assertIn("enrichment_cnetplot", figure_types)
        self.assertIn("enrichment_ppi_network", figure_types)

    def test_disease_enrichment_artifact_exposes_disease_graph_options(self) -> None:
        catalog = FigureCatalog()
        artifact = Artifact(
            artifact_id="artifact_disease",
            session_id="session_test",
            kind="disease_enrichment",
            title="Disease context",
            summary="Disease terms",
            created_at=utc_now_iso(),
            provider="yulab_dose",
            payload={
                "input_genes": ["TP53", "BRCA1", "EGFR"],
                "database": "DO",
                "terms": [
                    {
                        "term_id": "DOID:162",
                        "term_name": "cancer",
                        "source": "DO",
                        "p_value": 1e-8,
                        "intersection_size": 3,
                        "matched_genes": ["TP53", "BRCA1", "EGFR"],
                    },
                ],
            },
        )

        figure_types = [row["figure_type"] for row in catalog.figure_options(artifact)]
        self.assertIn("enrichment_barplot", figure_types)
        self.assertIn("disease_emapplot", figure_types)
        self.assertIn("disease_cnetplot", figure_types)
        self.assertIn("disease_ppi_network", figure_types)

    def test_program_comparison_artifact_exposes_comparecluster_map_option(self) -> None:
        catalog = FigureCatalog()
        artifact = Artifact(
            artifact_id="artifact_compare",
            session_id="session_test",
            kind="program_comparison",
            title="Clock vs metabolism",
            summary="Program comparison",
            created_at=utc_now_iso(),
            provider="yulab_clusterprofiler",
            payload={
                "database": "GO_BP",
                "program_inputs": {
                    "Clock core": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                    "Lipid metabolism": ["PPARA", "PPARG", "FASN", "SREBF1"],
                },
                "comparison_terms": [
                    {"program_label": "Clock core", "term_id": "GO:0007623", "term_name": "circadian rhythm", "source": "GO:BP", "p_value": 1e-8, "intersection_size": 4},
                    {"program_label": "Lipid metabolism", "term_id": "GO:0019216", "term_name": "regulation of lipid metabolic process", "source": "GO:BP", "p_value": 3e-5, "intersection_size": 3},
                ],
            },
        )

        figure_types = [row["figure_type"] for row in catalog.figure_options(artifact)]
        self.assertIn("compare_programs_barplot", figure_types)
        self.assertIn("compare_programs_emapplot", figure_types)
        self.assertIn("compare_programs_cnetplot", figure_types)

    def test_ranked_gsea_artifact_exposes_running_score_option(self) -> None:
        catalog = FigureCatalog()
        artifact = Artifact(
            artifact_id="artifact_gsea",
            session_id="session_test",
            kind="ranked_gsea",
            title="Clock ranked signature",
            summary="Ranked GSEA",
            created_at=utc_now_iso(),
            provider="yulab_clusterprofiler",
            payload={
                "sources": ["GO:BP", "REAC"],
                "ranked_gene_scores": [
                    {"gene": "CLOCK", "score": 2.5},
                    {"gene": "ARNTL", "score": 2.3},
                    {"gene": "PER1", "score": 2.1},
                    {"gene": "CRY1", "score": 1.8},
                    {"gene": "MYC", "score": -1.7},
                ],
                "terms": [
                    {
                        "term_id": "GO:0007623",
                        "term_name": "circadian rhythm",
                        "source": "GO:BP",
                        "p_value": 1e-8,
                        "adjusted_p_value": 1e-6,
                        "normalized_enrichment_score": 2.2,
                    }
                ],
            },
        )

        figure_types = [row["figure_type"] for row in catalog.figure_options(artifact)]
        self.assertIn("enrichment_barplot", figure_types)
        self.assertIn("ranked_gsea_plot", figure_types)

    def test_rnaseq_artifact_exposes_native_enrichplot_graph_options(self) -> None:
        catalog = FigureCatalog()
        artifact = Artifact(
            artifact_id="artifact_rnaseq",
            session_id="session_test",
            kind="rnaseq_analysis",
            title="Tumor vs control",
            summary="RNA-seq analysis",
            created_at=utc_now_iso(),
            provider="gleskit_aggregate",
            payload={
                "input": {"result_path": "/tmp/deg.tsv"},
                "ranked_gene_scores": [
                    {"gene": "TP53", "score": 3.2},
                    {"gene": "MDM2", "score": 2.8},
                    {"gene": "CDK1", "score": 2.1},
                    {"gene": "CDKN1A", "score": -2.5},
                ],
                "significant_gene_sets": {
                    "upregulated": ["TP53", "MDM2", "CDK1"],
                    "downregulated": ["CDKN1A", "RB1", "PTEN"],
                },
                "enrichment": {
                    "upregulated": {
                        "sources": ["GO:BP", "REAC"],
                        "terms": [{"term_id": "GO:1", "term_name": "mitotic cell cycle", "source": "GO:BP", "p_value": 1e-5}],
                    },
                    "downregulated": {
                        "sources": ["GO:BP", "REAC"],
                        "terms": [{"term_id": "GO:2", "term_name": "apoptotic signaling", "source": "GO:BP", "p_value": 2e-4}],
                    },
                },
            },
        )

        figure_types = [row["figure_type"] for row in catalog.figure_options(artifact)]
        self.assertIn("rnaseq_up_emapplot", figure_types)
        self.assertIn("rnaseq_up_cnetplot", figure_types)
        self.assertIn("rnaseq_up_ppi_network", figure_types)
        self.assertIn("rnaseq_down_emapplot", figure_types)
        self.assertIn("rnaseq_down_cnetplot", figure_types)
        self.assertIn("rnaseq_down_ppi_network", figure_types)
        self.assertIn("ranked_gsea_plot", figure_types)

    def test_figure_type_aliases_resolve_for_supported_artifacts(self) -> None:
        catalog = FigureCatalog()
        rnaseq_artifact = Artifact(
            artifact_id="artifact_rnaseq",
            session_id="session_test",
            kind="rnaseq_analysis",
            title="Tumor vs control",
            summary="RNA-seq analysis",
            created_at=utc_now_iso(),
            provider="gleskit_aggregate",
            payload={
                "input": {"result_path": "/tmp/deg.tsv"},
                "significant_gene_sets": {
                    "upregulated": ["TP53", "MDM2", "CDK1"],
                    "downregulated": [],
                },
                "enrichment": {
                    "upregulated": {
                        "sources": ["GO:BP", "REAC"],
                        "terms": [{"term_id": "GO:1", "term_name": "mitotic cell cycle", "source": "GO:BP", "p_value": 1e-5}],
                    },
                    "downregulated": {"terms": []},
                },
            },
        )
        enrichment_artifact = Artifact(
            artifact_id="artifact_enrich",
            session_id="session_test",
            kind="enrichment",
            title="Clock enrichment",
            summary="Clock terms",
            created_at=utc_now_iso(),
            provider="yulab_clusterprofiler",
            payload={
                "genes": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                "sources": ["GO:BP", "REAC"],
                "terms": [
                    {"term_id": "GO:0007623", "term_name": "circadian rhythm", "source": "GO:BP", "p_value": 1e-8, "intersection_size": 4},
                ],
            },
        )
        compare_artifact = Artifact(
            artifact_id="artifact_compare",
            session_id="session_test",
            kind="program_comparison",
            title="Clock vs metabolism",
            summary="Program comparison",
            created_at=utc_now_iso(),
            provider="yulab_clusterprofiler",
            payload={
                "database": "GO_BP",
                "program_inputs": {
                    "Clock core": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                    "Lipid metabolism": ["PPARA", "PPARG", "FASN", "SREBF1"],
                },
                "comparison_terms": [
                    {"program_label": "Clock core", "term_id": "GO:0007623", "term_name": "circadian rhythm", "source": "GO:BP", "p_value": 1e-8, "intersection_size": 4},
                ],
            },
        )
        disease_artifact = Artifact(
            artifact_id="artifact_disease",
            session_id="session_test",
            kind="disease_enrichment",
            title="Disease context",
            summary="Disease terms",
            created_at=utc_now_iso(),
            provider="yulab_dose",
            payload={
                "input_genes": ["TP53", "BRCA1"],
                "database": "DO",
                "terms": [
                    {
                        "term_id": "DOID:162",
                        "term_name": "cancer",
                        "source": "DO",
                        "p_value": 1e-8,
                        "intersection_size": 2,
                        "matched_genes": ["TP53", "BRCA1"],
                    },
                ],
            },
        )

        self.assertEqual(catalog._resolve_requested_figure_type(rnaseq_artifact, "rnaseq_up_enrichmap"), "rnaseq_up_emapplot")
        self.assertEqual(catalog._resolve_requested_figure_type(rnaseq_artifact, "upregulated_gene_concept_network"), "rnaseq_up_cnetplot")
        self.assertEqual(catalog._resolve_requested_figure_type(rnaseq_artifact, "upregulated_gene_gene_network"), "rnaseq_up_ppi_network")
        self.assertEqual(catalog._resolve_requested_figure_type(enrichment_artifact, "enrichment_map"), "enrichment_emapplot")
        self.assertEqual(catalog._resolve_requested_figure_type(enrichment_artifact, "ppi_network"), "enrichment_ppi_network")
        self.assertEqual(catalog._resolve_requested_figure_type(disease_artifact, "disease_gene_concept_network"), "disease_cnetplot")
        self.assertEqual(catalog._resolve_requested_figure_type(compare_artifact, "comparecluster_enrichment_map"), "compare_programs_emapplot")
        self.assertEqual(catalog._resolve_requested_figure_type(compare_artifact, "comparecluster_cnetplot"), "compare_programs_cnetplot")

    def test_requested_rnaseq_enrichment_map_can_fallback_to_native_cnetplot(self) -> None:
        class FakeRunner:
            last_request = None

            def run(self, script_name, request, *, capability, timeout_seconds):
                self.last_request = request
                Path(request["output_png_path"]).write_bytes(b"png")
                Path(request["output_svg_path"]).write_text("<svg/>", encoding="utf-8")
                return {
                    "caption": "ClusterProfiler/enrichplot gene-concept network fallback.",
                    "render_mode": "cnetplot",
                    "rendered_source": "REAC",
                    "rendered_term_count": 4,
                    "rendered_top_n": 4,
                    "fallback_reason": "The requested enrichment map was too heavy to render cleanly.",
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            catalog = FigureCatalog(settings=settings)
            catalog.r_runner = FakeRunner()
            artifact = Artifact(
                artifact_id="artifact_rnaseq",
                session_id="session_test",
                kind="rnaseq_analysis",
                title="Tumor vs control",
                summary="RNA-seq analysis",
                created_at=utc_now_iso(),
                provider="gleskit_aggregate",
                payload={
                    "significant_gene_sets": {
                        "upregulated": ["TP53", "MDM2", "CDK1"],
                        "downregulated": [],
                    },
                    "enrichment": {
                        "upregulated": {
                            "sources": ["GO:BP", "REAC"],
                            "terms": [{
                                "term_id": "GO:1",
                                "term_name": "mitotic cell cycle",
                                "source": "GO:BP",
                                "p_value": 1e-5,
                                "intersection_size": 3,
                                "matched_genes": ["TP53", "MDM2", "CDK1"],
                            }],
                        },
                        "downregulated": {"terms": []},
                    },
                },
            )

            created = catalog.create_figure(
                artifact,
                output_dir=Path(tmp) / "figures",
                figure_type="rnaseq_up_emapplot",
            )

            self.assertEqual(created["figure_type"], "rnaseq_up_cnetplot")
            self.assertEqual(created["requested_figure_type"], "rnaseq_up_emapplot")
            self.assertEqual(created["render_mode"], "cnetplot")
            self.assertEqual(
                catalog.r_runner.last_request["saved_terms"][0]["matched_genes"],
                ["TP53", "MDM2", "CDK1"],
            )
            self.assertTrue(Path(created["png_path"]).exists())
            self.assertTrue(Path(created["svg_path"]).exists())

    def test_enrichment_artifact_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))
            workspace = WorkspaceService(store)
            session = store.create_session(
                title="Figure test",
                description="Figure generation",
                species="human",
            )
            artifact = store.create_artifact(
                session.session_id,
                kind="enrichment",
                title="Test enrichment",
                summary="Enrichment summary",
                provider="gprofiler",
                payload={
                    "terms": [
                        {"term_id": "GO:1", "term_name": "DNA repair", "source": "GO:BP", "p_value": 1e-8, "intersection_size": 6},
                        {"term_id": "R-HSA-1", "term_name": "Cell cycle", "source": "REAC", "p_value": 3e-5, "intersection_size": 4},
                    ]
                },
            )

            created = workspace.create_figure(
                session.session_id,
                artifact.artifact_id,
                figure_type="enrichment_barplot",
                title="DNA repair figure",
            )
            payload = created["payload"]
            self.assertEqual(created["kind"], "figure")
            self.assertTrue(Path(payload["png_path"]).exists())
            self.assertTrue(Path(payload["svg_path"]).exists())
            self.assertEqual(payload["inline_mime_type"], "image/png")
            self.assertEqual(payload["report_markdown"], payload["image_markdown"])

            tool_result = _figure_tool_result(created)
            self.assertIsInstance(tool_result, CallToolResult)
            self.assertEqual(tool_result.structuredContent["artifact_id"], created["artifact_id"])
            self.assertTrue(any(block.type == "image" for block in tool_result.content))
            self.assertTrue(any(block.type == "text" for block in tool_result.content))
            self.assertIn("preferred_embed_markdown", tool_result.structuredContent)
            self.assertIn("embed_markdown_candidates", tool_result.structuredContent)
            self.assertTrue(tool_result.structuredContent["embed_markdown_candidates"])

            registry = _FigureDownloadRegistry(ttl_seconds=600, max_entries=32)
            tool_result_with_links = _figure_tool_result(
                created,
                download_registry=registry,
                http_url_builder=lambda token: f"https://example.org/figure-download/{token}",
            )
            download_blocks = [block for block in tool_result_with_links.content if block.type == "resource_link"]
            self.assertTrue(download_blocks)
            self.assertIn("download_links", tool_result_with_links.structuredContent)
            self.assertTrue(tool_result_with_links.structuredContent["download_links"])
            self.assertIn("download_links_note", tool_result_with_links.structuredContent)
            self.assertIn("preferred_download_url", tool_result_with_links.structuredContent)
            self.assertIn("preferred_embed_markdown", tool_result_with_links.structuredContent)
            self.assertIn("embed_markdown_candidates", tool_result_with_links.structuredContent)
            self.assertTrue(
                str(tool_result_with_links.structuredContent["download_links"][0].get("http_url") or "").startswith(
                    "https://example.org/figure-download/"
                )
            )
            self.assertTrue(
                str(tool_result_with_links.structuredContent["preferred_download_url"] or "").startswith(
                    "https://example.org/figure-download/"
                )
            )
            self.assertIn(
                "https://example.org/figure-download/",
                str(tool_result_with_links.structuredContent["preferred_embed_markdown"] or ""),
            )
            text_blocks = [block.text for block in tool_result_with_links.content if block.type == "text"]
            self.assertTrue(any("Direct download URIs:" in block for block in text_blocks))
            self.assertTrue(any("https://example.org/figure-download/" in block for block in text_blocks))
            self.assertTrue(any("Preferred user-facing download URL:" in block for block in text_blocks))

            deleted = workspace.delete_artifacts(session.session_id, [created["artifact_id"]])
            self.assertEqual(deleted["deleted_count"], 1)
            self.assertFalse(Path(payload["png_path"]).exists())
            self.assertFalse(Path(payload["svg_path"]).exists())

    def test_r_backed_figure_can_succeed_with_png_only_output(self) -> None:
        class FakeRunner:
            def run(self, script_name, request, *, capability, timeout_seconds):
                Path(request["output_png_path"]).write_bytes(b"png")
                return {
                    "caption": "ClusterProfiler/enrichplot enrichment map.",
                    "render_mode": "emapplot",
                    "rendered_source": "REAC",
                    "rendered_term_count": 2,
                    "rendered_top_n": 2,
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            catalog = FigureCatalog(settings=settings)
            catalog.r_runner = FakeRunner()
            artifact = Artifact(
                artifact_id="artifact_enrich",
                session_id="session_test",
                kind="enrichment",
                title="Clock enrichment",
                summary="Clock terms",
                created_at=utc_now_iso(),
                provider="yulab_clusterprofiler",
                payload={
                    "genes": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                    "sources": ["GO:BP", "REAC"],
                    "terms": [
                        {
                            "term_id": "R-HSA-1368108",
                            "term_name": "Circadian Clock",
                            "source": "REAC",
                            "p_value": 2e-6,
                            "intersection_size": 3,
                            "matched_genes": ["CLOCK", "ARNTL", "PER1"],
                        },
                    ],
                },
            )

            created = catalog.create_figure(
                artifact,
                output_dir=Path(tmp) / "figures",
                figure_type="enrichment_emapplot",
            )

            self.assertTrue(Path(created["png_path"]).exists())
            self.assertEqual(created["svg_path"], "")
            self.assertEqual(created["figure_files"], [created["png_path"]])

    def test_r_runner_runtime_env_exports_r_max_vsize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict("os.environ", {"GLESKIT_R_MAX_VSIZE": "64Gb"}, clear=False):
                settings = load_settings(root)
            runner = RJsonScriptRunner(settings)
            env = runner._runtime_env()
            self.assertEqual(env["R_MAX_VSIZE"], "64Gb")

    def test_create_figure_can_follow_existing_figure_back_to_source_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))
            workspace = WorkspaceService(store)
            session = store.create_session(
                title="Figure source test",
                description="Figure generation",
                species="human",
            )
            source_artifact = store.create_artifact(
                session.session_id,
                kind="enrichment",
                title="Test enrichment",
                summary="Enrichment summary",
                provider="gprofiler",
                payload={
                    "terms": [
                        {"term_id": "GO:1", "term_name": "DNA repair", "source": "GO:BP", "p_value": 1e-8, "intersection_size": 6},
                        {"term_id": "R-HSA-1", "term_name": "Cell cycle", "source": "REAC", "p_value": 3e-5, "intersection_size": 4},
                    ]
                },
            )
            first_figure = workspace.create_figure(
                session.session_id,
                source_artifact.artifact_id,
                figure_type="enrichment_barplot",
                title="DNA repair figure",
            )

            second_figure = workspace.create_figure(
                session.session_id,
                first_figure["artifact_id"],
                figure_type="enrichment_barplot",
                title="DNA repair figure again",
            )

            self.assertEqual(second_figure["provenance"]["source_artifact_id"], source_artifact.artifact_id)
            self.assertEqual(second_figure["provenance"]["source_artifact_kind"], "enrichment")

    def test_rnaseq_artifact_can_create_volcano_plot_from_saved_result_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "deg.tsv"
            result_path.write_text(
                "\n".join(
                    [
                        "gene\tlog2FoldChange\tpadj\tpvalue",
                        "TP53\t2.1\t0.001\t0.001",
                        "MDM2\t1.5\t0.010\t0.010",
                        "CDKN1A\t-1.8\t0.020\t0.020",
                        "GAPDH\t0.1\t0.900\t0.900",
                    ]
                ),
                encoding="utf-8",
            )

            store = WorkspaceStore(root / "workspace")
            workspace = WorkspaceService(store)
            session = store.create_session(
                title="RNA-seq figure test",
                description="Volcano",
                species="human",
            )
            artifact = store.create_artifact(
                session.session_id,
                kind="rnaseq_analysis",
                title="Tumor vs control",
                summary="RNA-seq analysis",
                provider="gleskit_aggregate",
                payload={
                    "input": {"result_path": str(result_path), "label": "Tumor vs control", "comparison": "Tumor vs control"},
                    "table": {"column_map": {"gene": "gene", "log2_fold_change": "log2FoldChange", "padj": "padj", "p_value": "pvalue"}},
                    "summary_stats": {"padj_threshold": 0.05, "p_value_threshold": 0.05, "log2_fold_change_threshold": 1.0},
                    "enrichment": {"upregulated": {"terms": []}, "downregulated": {"terms": []}},
                },
            )

            created = workspace.create_figure(
                session.session_id,
                artifact.artifact_id,
                figure_type="rnaseq_volcano",
            )
            payload = created["payload"]
            self.assertTrue(Path(payload["png_path"]).exists())
            self.assertTrue(Path(payload["svg_path"]).exists())

    def test_auto_figure_tool_result_attaches_image_for_stateless_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(settings=settings)
            payload = {
                "summary": "Retrieved enrichment for 4 gene(s).",
                "provider": "gprofiler",
                "structured_data": {
                    "species": "human",
                    "genes": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                    "terms": [
                        {
                            "term_id": "GO:0007623",
                            "term_name": "circadian rhythm",
                            "source": "GO:BP",
                            "p_value": 1e-12,
                            "intersection_size": 4,
                        },
                        {
                            "term_id": "KEGG:04710",
                            "term_name": "Circadian rhythm",
                            "source": "KEGG",
                            "p_value": 2e-8,
                            "intersection_size": 3,
                        },
                    ],
                },
                "warnings": [],
                "workspace": {
                    "available": False,
                    "active_session_id": None,
                    "capture_enabled": False,
                    "saved": False,
                    "artifact_id": None,
                },
            }

            result = _auto_figure_tool_result("enrich_gene_set", payload, service)
            self.assertIsInstance(result, CallToolResult)
            self.assertTrue(any(block.type == "image" for block in result.content))
            self.assertTrue(any(block.type == "text" for block in result.content))
            self.assertIn("auto_figures", result.structuredContent)
            self.assertEqual(result.structuredContent["auto_figures"][0]["payload"]["figure_type"], "enrichment_barplot")

    def test_auto_figure_tool_result_attaches_download_links_for_stateless_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(settings=settings)
            payload = {
                "summary": "Retrieved enrichment for 4 gene(s).",
                "provider": "gprofiler",
                "structured_data": {
                    "species": "human",
                    "genes": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                    "terms": [
                        {
                            "term_id": "GO:0007623",
                            "term_name": "circadian rhythm",
                            "source": "GO:BP",
                            "p_value": 1e-12,
                            "intersection_size": 4,
                        },
                        {
                            "term_id": "KEGG:04710",
                            "term_name": "Circadian rhythm",
                            "source": "KEGG",
                            "p_value": 2e-8,
                            "intersection_size": 3,
                        },
                    ],
                },
                "warnings": [],
                "workspace": {
                    "available": False,
                    "active_session_id": None,
                    "capture_enabled": False,
                    "saved": False,
                    "artifact_id": None,
                },
            }
            registry = _FigureDownloadRegistry(ttl_seconds=600, max_entries=32)

            result = _auto_figure_tool_result(
                "enrich_gene_set",
                payload,
                service,
                download_registry=registry,
                http_url_builder=lambda token: f"https://example.org/figure-download/{token}",
            )
            self.assertIsInstance(result, CallToolResult)
            download_blocks = [block for block in result.content if block.type == "resource_link"]
            self.assertTrue(download_blocks)
            uri = str(download_blocks[0].uri)
            self.assertTrue(uri.startswith("gleskit://figure-download/"))
            token = uri.rsplit("/", 1)[-1]
            entry = registry.get(token)
            self.assertIsNotNone(entry)
            self.assertTrue(entry.data)
            self.assertTrue(entry.filename.endswith(".png") or entry.filename.endswith(".svg"))
            self.assertIn("download_links", result.structuredContent)
            self.assertTrue(result.structuredContent["download_links"])
            self.assertIn("download_links_note", result.structuredContent)
            self.assertIn("preferred_download_url", result.structuredContent)
            self.assertTrue(
                str(result.structuredContent["download_links"][0].get("http_url") or "").startswith(
                    "https://example.org/figure-download/"
                )
            )
            self.assertTrue(
                str(result.structuredContent["preferred_download_url"] or "").startswith(
                    "https://example.org/figure-download/"
                )
            )
            auto_figures = result.structuredContent.get("auto_figures") or []
            self.assertTrue(auto_figures)
            self.assertIn("download_links", auto_figures[0])
            self.assertTrue(auto_figures[0]["download_links"])
            text_blocks = [block.text for block in result.content if block.type == "text"]
            self.assertTrue(any("Direct download URIs:" in block for block in text_blocks))
            self.assertTrue(any("https://example.org/figure-download/" in block for block in text_blocks))
            self.assertTrue(any("Preferred user-facing download URL:" in block for block in text_blocks))

    def test_auto_figure_tool_result_attaches_image_for_stateless_disease_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(settings=settings)
            payload = {
                "summary": "Retrieved 2 disease enrichment term(s) from DO for 2 resolved gene(s).",
                "provider": "yulab_dose",
                "structured_data": {
                    "database": "DO",
                    "species": "human",
                    "input_genes": ["TP53", "BRCA1"],
                    "terms": [
                        {
                            "term_id": "DOID:162",
                            "term_name": "cancer",
                            "source": "DO",
                            "p_value": 1e-8,
                            "intersection_size": 2,
                            "matched_genes": ["TP53", "BRCA1"],
                        },
                        {
                            "term_id": "DOID:3908",
                            "term_name": "lung carcinoma",
                            "source": "DO",
                            "p_value": 5e-5,
                            "intersection_size": 1,
                            "matched_genes": ["TP53"],
                        },
                    ],
                },
                "warnings": [],
                "workspace": {
                    "available": False,
                    "active_session_id": None,
                    "capture_enabled": False,
                    "saved": False,
                    "artifact_id": None,
                },
            }

            result = _auto_figure_tool_result("gene_set_disease_context", payload, service)
            self.assertIsInstance(result, CallToolResult)
            self.assertTrue(any(block.type == "image" for block in result.content))
            self.assertIn("auto_figures", result.structuredContent)
            self.assertEqual(result.structuredContent["auto_figures"][0]["payload"]["figure_type"], "enrichment_barplot")

    def test_auto_figure_tool_result_attaches_image_for_ranked_gsea(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(settings=settings)
            payload = {
                "summary": "Retrieved 2 ranked GSEA term(s) for 6 ranked gene(s).",
                "provider": "yulab_clusterprofiler",
                "structured_data": {
                    "analysis_mode": "gsea",
                    "species": "human",
                    "terms": [
                        {
                            "term_id": "GO:0007623",
                            "term_name": "circadian rhythm",
                            "source": "GO:BP",
                            "p_value": 1e-7,
                            "adjusted_p_value": 1e-5,
                            "normalized_enrichment_score": 2.1,
                            "direction": "positive",
                        },
                        {
                            "term_id": "R-HSA-156590",
                            "term_name": "G1/S transition",
                            "source": "REAC",
                            "p_value": 3e-4,
                            "adjusted_p_value": 2e-3,
                            "normalized_enrichment_score": -1.8,
                            "direction": "negative",
                        },
                    ],
                },
                "warnings": [],
                "workspace": {
                    "available": False,
                    "active_session_id": None,
                    "capture_enabled": False,
                    "saved": False,
                    "artifact_id": None,
                },
            }

            result = _auto_figure_tool_result("ranked_gene_set_gsea", payload, service)
            self.assertIsInstance(result, CallToolResult)
            self.assertTrue(any(block.type == "image" for block in result.content))
            self.assertIn("auto_figures", result.structuredContent)
            self.assertEqual(result.structuredContent["auto_figures"][0]["payload"]["figure_type"], "enrichment_barplot")

    def test_auto_figure_tool_result_attaches_image_for_program_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(settings=settings)
            payload = {
                "summary": "Compared 2 gene program(s) and retrieved 2 enrichment row(s).",
                "provider": "yulab_clusterprofiler",
                "structured_data": {
                    "database": "GO_BP",
                    "program_inputs": {
                        "Clock core": ["CLOCK", "ARNTL", "PER1", "CRY1"],
                        "Lipid metabolism": ["PPARA", "PPARG", "FASN", "SREBF1"],
                    },
                    "comparison_terms": [
                        {
                            "program_label": "Clock core",
                            "term_id": "GO:0007623",
                            "term_name": "circadian rhythm",
                            "source": "GO:BP",
                            "p_value": 1e-8,
                            "intersection_size": 3,
                        },
                        {
                            "program_label": "Lipid metabolism",
                            "term_id": "GO:0019216",
                            "term_name": "regulation of lipid metabolic process",
                            "source": "GO:BP",
                            "p_value": 5e-5,
                            "intersection_size": 3,
                        },
                    ],
                },
                "warnings": [],
                "workspace": {
                    "available": False,
                    "active_session_id": None,
                    "capture_enabled": False,
                    "saved": False,
                    "artifact_id": None,
                },
            }

            result = _auto_figure_tool_result("compare_gene_programs", payload, service)
            self.assertIsInstance(result, CallToolResult)
            self.assertTrue(any(block.type == "image" for block in result.content))
            self.assertIn("auto_figures", result.structuredContent)
            self.assertEqual(result.structuredContent["auto_figures"][0]["payload"]["figure_type"], "compare_programs_barplot")

    def test_stateless_figure_source_lists_dynamic_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(settings=settings)
            payload = {
                "summary": "Retrieved 1 disease enrichment term(s) from DO for 2 resolved gene(s).",
                "provider": "yulab_dose",
                "structured_data": {
                    "database": "DO",
                    "species": "human",
                    "input_genes": ["TP53", "BRCA1"],
                    "terms": [
                        {
                            "term_id": "DOID:162",
                            "term_name": "cancer",
                            "source": "DO",
                            "p_value": 1e-8,
                            "intersection_size": 2,
                            "matched_genes": ["TP53", "BRCA1"],
                            "gene_ids": ["7157", "672"],
                        },
                    ],
                },
                "warnings": [],
                "workspace": {
                    "available": False,
                    "active_session_id": None,
                    "capture_enabled": False,
                    "saved": False,
                    "artifact_id": None,
                },
            }
            with_source = service.attach_figure_source_id("gene_set_disease_context", payload)
            source_id = str(with_source.get("figure_source_id") or "")
            self.assertTrue(source_id)

            options = service.list_figure_options(figure_source_id=source_id)
            self.assertEqual(options["source"]["mode"], "stateless")
            figure_types = [row["figure_type"] for row in options["options"]]
            self.assertIn("disease_cnetplot", figure_types)
            self.assertIn("disease_ppi_network", figure_types)

    def test_generate_disease_cnetplot_stateless_without_session(self) -> None:
        class FakeRunner:
            def run(self, script_name, request, *, capability, timeout_seconds):
                Path(request["output_png_path"]).write_bytes(b"png")
                Path(request["output_svg_path"]).write_text("<svg/>", encoding="utf-8")
                return {
                    "caption": "ClusterProfiler/enrichplot disease gene-concept network.",
                    "render_mode": "cnetplot",
                    "rendered_source": "DO",
                    "rendered_term_count": 1,
                    "rendered_top_n": 1,
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(Path(tmp))
            service = GLESKitService(settings=settings)
            service.figure_catalog.r_runner = FakeRunner()
            payload = {
                "summary": "Retrieved 1 disease enrichment term(s) from DO for 2 resolved gene(s).",
                "provider": "yulab_dose",
                "structured_data": {
                    "database": "DO",
                    "species": "human",
                    "input_genes": ["TP53", "BRCA1"],
                    "terms": [
                        {
                            "term_id": "DOID:162",
                            "term_name": "cancer",
                            "source": "DO",
                            "p_value": 1e-8,
                            "intersection_size": 2,
                            "matched_genes": ["TP53", "BRCA1"],
                            "gene_ids": ["7157", "672"],
                        },
                    ],
                },
                "warnings": [],
                "workspace": {
                    "available": False,
                    "active_session_id": None,
                    "capture_enabled": False,
                    "saved": False,
                    "artifact_id": None,
                },
            }
            with_source = service.attach_figure_source_id("gene_set_disease_context", payload)
            source_id = str(with_source.get("figure_source_id") or "")
            self.assertTrue(source_id)

            generated = service.generate_figure(
                figure_type="disease_cnetplot",
                figure_source_id=source_id,
            )
            self.assertEqual(generated["payload"]["figure_type"], "disease_cnetplot")
            self.assertFalse(generated["saved"])
            self.assertEqual(generated["source"]["mode"], "stateless")
            self.assertTrue(generated["_inline_image_bytes"])


if __name__ == "__main__":
    unittest.main()
