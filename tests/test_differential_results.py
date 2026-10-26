from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from gleskit_mcp.analysis import DifferentialResultBackend
from gleskit_mcp.analysis.r_runner import RJsonScriptRunner
from gleskit_mcp.config import load_settings
from gleskit_mcp.providers.base import ProviderResponse
from gleskit_mcp.services import GLESKitService


class DifferentialResultTests(unittest.TestCase):
    def test_backend_requires_unambiguous_columns_and_honors_significance_mode(self) -> None:
        backend = DifferentialResultBackend()
        parsed = backend.analyze(
            table_text="gene\tlogFC\tpadj\tpvalue\nTP53\t2.0\t0.01\t0.2\nBRCA1\t-1.5\t0.2\t0.01\n",
            identifier_column="gene",
            effect_column="logFC",
            p_value_column="pvalue",
            significance_mode="p_value",
        )
        self.assertTrue(parsed["rows"][1]["significant"])
        self.assertFalse(parsed["rows"][0]["significant"])

    def test_gene_level_integration_preserves_concordance_without_a_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GLESKitService(settings=load_settings(Path(tmp)))
            common = {
                "positive_condition": "case",
                "reference_condition": "reference",
                "result_scope": "full_results",
                "significance_mode": "padj",
                "identifier_column": "feature",
                "effect_column": "logFC",
                "padj_column": "padj",
            }
            transcript = service.analyze_differential_result(
                modality="transcriptomics",
                table_text="feature\tlogFC\tpadj\nTP53\t2\t0.01\nBRCA1\t-2\t0.01\n",
                identifier_mapping={"TP53": ["TP53"], "BRCA1": ["BRCA1"]},
                response_detail="full",
                **common,
            )
            protein = service.analyze_differential_result(
                modality="proteomics",
                table_text="feature\tlogFC\tpadj\nP53\t1\t0.01\nB1\t2\t0.01\n",
                identifier_mapping={"P53": ["TP53"], "B1": ["BRCA1"]},
                response_detail="full",
                **common,
            )
            integrated = service.integrate_gene_level_results(
                results=[transcript, protein],
                response_detail="full",
            )["structured_data"]
            states = {row["gene"]: row["integration_state"] for row in integrated["gene_evidence_matrix"]}
            self.assertEqual(states["TP53"], "shared_concordant")
            self.assertEqual(states["BRCA1"], "shared_discordant")
            significance_states = {
                row["gene"]: row["cross_modal_significance_status"]
                for row in integrated["gene_evidence_matrix"]
            }
            self.assertEqual(significance_states["TP53"], "cross_modality_significant_concordant")
            self.assertEqual(significance_states["BRCA1"], "cross_modality_significant_discordant")
            self.assertNotIn("biomarker_score", integrated)

    def test_filtered_proteomics_requires_background_and_keeps_ambiguous_mapping_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GLESKitService(settings=load_settings(Path(tmp)))
            result = service.analyze_differential_result(
                modality="proteomics",
                positive_condition="case",
                reference_condition="reference",
                result_scope="filtered_results",
                table_text="feature\tlogFC\tpadj\nP1\t2\t0.01\n",
                identifier_column="feature",
                effect_column="logFC",
                padj_column="padj",
                significance_mode="padj",
                identifier_mapping={"P1": ["GENE1", "GENE2"]},
                response_detail="full",
            )["structured_data"]
            self.assertEqual(result["mapping_audit"][0]["status"], "ambiguous")
            self.assertTrue(result["enrichment"]["upregulated"]["not_run"])

    def test_full_result_followups_can_be_deferred_with_reusable_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GLESKitService(settings=load_settings(Path(tmp)))
            result = service.analyze_differential_result(
                modality="transcriptomics",
                positive_condition="case",
                reference_condition="reference",
                result_scope="full_results",
                table_text="gene\tlogFC\tpvalue\nTP53\t2\t0.01\nBRCA1\t-2\t0.02\n",
                identifier_column="gene",
                effect_column="logFC",
                p_value_column="pvalue",
                significance_mode="p_value",
                identifier_mapping={"TP53": ["TP53"], "BRCA1": ["BRCA1"]},
                include_enrichment=False,
                include_ranked_gsea=False,
                response_detail="full",
            )["structured_data"]
            self.assertTrue(result["enrichment"]["upregulated"]["not_run"])
            self.assertTrue(result["ranked_gsea"]["not_run"])
            self.assertEqual(result["background"]["gene_count"], 2)
            self.assertTrue(Path(result["derived_inputs"]["tested_genes_path"]).exists())
            self.assertTrue(Path(result["derived_inputs"]["upregulated_genes_path"]).exists())
            self.assertTrue(Path(result["derived_inputs"]["downregulated_genes_path"]).exists())
            self.assertTrue(Path(result["derived_inputs"]["ranked_gene_scores_path"]).exists())

    def test_linear_ratio_effect_is_converted_and_oriented_from_declared_conditions(self) -> None:
        backend = DifferentialResultBackend()
        parsed = backend.analyze(
            table_text="accession\tratio\tpadj\nP1\t4\t0.01\n",
            identifier_column="accession",
            effect_column="ratio",
            padj_column="padj",
            significance_mode="padj",
            positive_condition="OCA",
            reference_condition="OA",
            effect_definition={
                "scale": "linear_ratio",
                "source_formula": "OA/OCA",
                "numerator_condition": "OA",
                "denominator_condition": "OCA",
            },
        )
        self.assertEqual(parsed["rows"][0]["log2_fold_change"], -2.0)
        self.assertEqual(parsed["effect_definition"]["transform_to_declared_contrast"], "invert")

    def test_preflight_recovers_exact_complete_protein_candidate_without_returning_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = root / "protein_results.xlsx"
            with pd.ExcelWriter(workbook_path) as writer:
                pd.DataFrame({
                    "Accession": ["P1", "P2"],
                    "logFC": [1.0, -1.0],
                    "p.adj": [0.01, 0.02],
                }).to_excel(writer, sheet_name="Final_DEPs", index=False)
                pd.DataFrame({
                    "Accession": ["P1", "P2", "P3"],
                    "Abundance Ratio: (OA)/(OCA)": [2.0, 0.5, 1.2],
                    "p.adj": [0.01, 0.02, 0.5],
                    "Abundance sample 1": [100.0, 110.0, 120.0],
                }).to_excel(writer, sheet_name="Proteins", index=False)
            service = GLESKitService(settings=load_settings(root))
            result = service.preflight_differential_result(
                result_path=str(workbook_path),
                modality="proteomics",
                sheet_name="Final_DEPs",
            )["structured_data"]
            self.assertEqual(result["route"]["selected_result_scope"], "filtered_results")
            self.assertEqual(result["route"]["recovered_background_sheet"], "Proteins")
            self.assertNotIn("feature_rows", result)
            self.assertTrue(Path(result["manifest_path"]).exists())

    def test_preflight_recognizes_embedded_adjusted_ratio_p_value_headers(self) -> None:
        backend = DifferentialResultBackend()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proteins.tsv"
            path.write_text(
                "Accession\tAbundance Ratio: (OA)/(OCA)\tAbundance Ratio Adj. P-Value: (OA)/(OCA)\n"
                "P1\t2\t0.01\n",
                encoding="utf-8",
            )
            schema = backend.inspect(result_path=str(path))
            self.assertEqual(schema["ratio_candidates"], ["Abundance Ratio: (OA)/(OCA)"])
            self.assertEqual(
                schema["padj_candidates"],
                ["Abundance Ratio Adj. P-Value: (OA)/(OCA)"],
            )

    def test_compact_differential_response_keeps_large_rows_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GLESKitService(settings=load_settings(Path(tmp)))
            result = service.analyze_differential_result(
                modality="transcriptomics",
                positive_condition="case",
                reference_condition="reference",
                result_scope="full_results",
                table_text="gene\tlogFC\tpvalue\nTP53\t2\t0.01\nBRCA1\t-2\t0.02\n",
                identifier_column="gene",
                effect_column="logFC",
                p_value_column="pvalue",
                significance_mode="p_value",
                identifier_mapping={"TP53": ["TP53"], "BRCA1": ["BRCA1"]},
                include_enrichment=False,
                include_ranked_gsea=False,
                response_detail="compact",
            )["structured_data"]
            self.assertEqual(result["agent_payload_mode"], "compact")
            self.assertNotIn("feature_rows", result)
            self.assertTrue(Path(result["derived_inputs"]["manifest_path"]).exists())

    def test_candidate_dossiers_preserve_evidence_without_a_biomarker_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GLESKitService(settings=load_settings(Path(tmp)))
            common = {
                "positive_condition": "case",
                "reference_condition": "reference",
                "result_scope": "full_results",
                "identifier_column": "feature",
                "effect_column": "logFC",
                "padj_column": "padj",
                "significance_mode": "padj",
                "include_enrichment": False,
                "include_ranked_gsea": False,
            }
            transcript = service.analyze_differential_result(
                modality="transcriptomics",
                table_text="feature\tlogFC\tpadj\nTP53\t2\t0.01\n",
                identifier_mapping={"TP53": ["TP53"]},
                **common,
            )
            protein = service.analyze_differential_result(
                modality="proteomics",
                table_text="feature\tlogFC\tpadj\nP53\t2\t0.01\n",
                identifier_mapping={"P53": ["TP53"]},
                **common,
            )
            dossier = service.build_biomarker_candidate_dossiers(
                results=[transcript, protein],
                genes=["TP53"],
                literature_evidence=[{"title": "Direct comparison", "url": "https://example.org/paper", "genes": ["TP53"]}],
            )["structured_data"]
            self.assertEqual(dossier["dossiers"][0]["evidence_status"], "shared_concordant")
            self.assertEqual(
                dossier["dossiers"][0]["cross_modal_significance_status"],
                "cross_modality_significant_concordant",
            )
            self.assertEqual(dossier["dossiers"][0]["clinical_validation_status"], "not_assessed")
            self.assertNotIn("biomarker_score", dossier["dossiers"][0])

    def test_dossier_does_not_mistake_a_nonsignificant_second_modality_for_corroboration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = GLESKitService(settings=load_settings(Path(tmp)))
            common = {
                "positive_condition": "case",
                "reference_condition": "reference",
                "result_scope": "full_results",
                "identifier_column": "feature",
                "effect_column": "logFC",
                "padj_column": "padj",
                "significance_mode": "padj",
                "include_enrichment": False,
                "include_ranked_gsea": False,
            }
            transcript = service.analyze_differential_result(
                modality="transcriptomics",
                table_text="feature\tlogFC\tpadj\nTP53\t2\t0.01\n",
                identifier_mapping={"TP53": ["TP53"]},
                **common,
            )
            protein = service.analyze_differential_result(
                modality="proteomics",
                table_text="feature\tlogFC\tpadj\nP53\t2\t0.9\n",
                identifier_mapping={"P53": ["TP53"]},
                **common,
            )
            dossier = service.build_biomarker_candidate_dossiers(
                results=[transcript, protein],
                genes=["TP53"],
            )["structured_data"]["dossiers"][0]
            self.assertEqual(dossier["cross_modal_significance_status"], "single_modality_significant")
            self.assertIn("not independent cross-modal corroboration", " ".join(dossier["caveats"]))

    def test_differential_identifier_mapping_uses_one_batch_fallback(self) -> None:
        class EmptyLocalCatalog:
            def resolve_genes(self, queries, *, species):
                return {}

        class RecordingMyGene:
            def __init__(self) -> None:
                self.calls = []

            def resolve_genes(self, queries, *, species):
                self.calls.append((list(queries), species))
                return {
                    query: ProviderResponse(
                        provider="fake_mygene",
                        summary="ok",
                        payload={"matches": [{"symbol": f"GENE_{query}"}]},
                    )
                    for query in queries
                }

        with tempfile.TemporaryDirectory() as tmp:
            service = GLESKitService(settings=load_settings(Path(tmp)))
            service.reference_catalog = EmptyLocalCatalog()
            mygene = RecordingMyGene()
            service.mygene = mygene
            mappings, warnings = service._map_differential_identifiers(["p1", "p2", "p1"], species="human")
            self.assertEqual(mygene.calls, [(["p1", "p2"], "human")])
            self.assertEqual(mappings["p1"]["canonical_gene"], "GENE_P1")
            self.assertEqual(mappings["p2"]["canonical_gene"], "GENE_P2")
            self.assertEqual(warnings, [])

    def test_r_runner_terminates_the_whole_process_group_after_timeout(self) -> None:
        class TimedOutProcess:
            pid = 4321
            returncode = -15

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(["Rscript"], timeout or 1)

        with tempfile.TemporaryDirectory() as tmp:
            runner = RJsonScriptRunner(settings=load_settings(Path(tmp)))
            with patch("gleskit_mcp.analysis.r_runner.subprocess.Popen", return_value=TimedOutProcess()), patch(
                "gleskit_mcp.analysis.r_runner.os.killpg"
            ) as killpg:
                with self.assertRaises(subprocess.TimeoutExpired):
                    runner._run_default_command(["Rscript"], timeout_seconds=1, env={})
            self.assertEqual(killpg.call_args_list[0].args, (4321, signal.SIGTERM))
            self.assertEqual(killpg.call_args_list[1].args, (4321, signal.SIGKILL))

    def test_r_runner_exposes_completed_job_health_without_leaving_it_active(self) -> None:
        job_id = RJsonScriptRunner._register_job(
            script_name="gleskit_ranked_gsea.R",
            capability="ranked GSEA",
            timeout_seconds=30,
        )
        RJsonScriptRunner._mark_job_started(job_id, 9876)
        RJsonScriptRunner._finish_job(job_id, status="timed_out", error="Timed out after 30s")
        status = RJsonScriptRunner.runtime_status()
        job = next(item for item in status["recent_jobs"] if item["job_id"] == job_id)
        self.assertEqual(job["status"], "timed_out")
        self.assertNotIn(job, status["active_jobs"])


if __name__ == "__main__":
    unittest.main()
