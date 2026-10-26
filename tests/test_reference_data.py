from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from gleskit_mcp.config import load_settings
from gleskit_mcp.reference_data import ReferenceCatalog


class ReferenceCatalogTests(unittest.TestCase):
    SAMPLE_TSV = "\n".join(
        [
            "hgnc_id\tsymbol\tname\tstatus\tlocus_group\tlocus_type\talias_symbol\tprev_symbol\tuniprot_ids\tensembl_gene_id\tentrez_id",
            "HGNC:1040\tBGN\tbiglycan\tApproved\tprotein-coding gene\tgene with protein product\tDSPG1|DSPG2\tPGS1\tP21810\tENSG00000182492\t633",
            "HGNC:11998\tTP53\ttumor protein p53\tApproved\tprotein-coding gene\tgene with protein product\tP53\tTRP53\tP04637\tENSG00000141510\t7157",
        ]
    )

    def build_catalog(self, root: Path) -> ReferenceCatalog:
        settings = load_settings(root)
        return ReferenceCatalog(settings, fetch_text=lambda _url: self.SAMPLE_TSV)

    def test_refresh_installs_local_hgnc_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build_catalog(Path(tmp))

            payload = catalog.refresh_all()

            self.assertTrue(payload["available"])
            self.assertEqual(payload["datasets"][0]["dataset_id"], "hgnc_human_genes")
            self.assertEqual(payload["datasets"][0]["record_count"], 2)
            self.assertGreaterEqual(payload["datasets"][0]["alias_count"], 6)

    def test_resolve_gene_matches_alias_previous_symbol_and_xrefs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build_catalog(Path(tmp))
            catalog.refresh_all()

            alias_result = catalog.resolve_gene("DSPG2")
            prev_result = catalog.resolve_gene("TRP53")
            ensembl_result = catalog.resolve_gene("ENSG00000141510")

            self.assertIsNotNone(alias_result)
            self.assertEqual(alias_result.payload["matches"][0]["symbol"], "BGN")
            self.assertEqual(alias_result.payload["matches"][0]["matched_on"], "alias")
            self.assertIsNotNone(prev_result)
            self.assertEqual(prev_result.payload["matches"][0]["symbol"], "TP53")
            self.assertEqual(prev_result.payload["matches"][0]["matched_on"], "previous_symbol")
            self.assertIsNotNone(ensembl_result)
            self.assertEqual(ensembl_result.payload["matches"][0]["matched_on"], "ensembl_gene_id")

    def test_resolve_gene_accepts_homo_sapiens_species_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build_catalog(Path(tmp))
            catalog.refresh_all()

            ensembl_result = catalog.resolve_gene("ENSG00000141510", species="Homo sapiens")

            self.assertIsNotNone(ensembl_result)
            self.assertEqual(ensembl_result.payload["matches"][0]["symbol"], "TP53")
            self.assertEqual(ensembl_result.payload["matches"][0]["matched_on"], "ensembl_gene_id")

    def test_resolve_genes_uses_one_local_batch_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.build_catalog(Path(tmp))
            catalog.refresh_all()

            resolved = catalog.resolve_genes(["ENSG00000182492.3", "TP53", "MISSING"])

            self.assertEqual(set(resolved), {"ENSG00000182492.3", "TP53"})
            self.assertEqual(resolved["ENSG00000182492.3"].payload["matches"][0]["symbol"], "BGN")
            self.assertEqual(resolved["TP53"].provider, "local_reference")


if __name__ == "__main__":
    unittest.main()
