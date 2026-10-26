from __future__ import annotations

import unittest

from gleskit_mcp.utils import normalize_gene_token, species_to_ensembl, species_to_kegg_organism, species_to_mygene


class SpeciesNormalizerTests(unittest.TestCase):
    def test_normalize_gene_token_strips_versioned_ensembl_suffix(self) -> None:
        self.assertEqual(normalize_gene_token("ENSG00000123456.14", species="human"), "ENSG00000123456")
        self.assertEqual(normalize_gene_token("ENSMUSG00000012345.7", species="mouse"), "ENSMUSG00000012345")

    def test_species_to_mygene_human_variants(self) -> None:
        self.assertEqual(species_to_mygene("human"), "human")
        self.assertEqual(species_to_mygene("Homo sapiens"), "human")
        self.assertEqual(species_to_mygene("Homo_Sapiens"), "human")
        self.assertEqual(species_to_mygene("9606"), "human")

    def test_species_to_mygene_mouse_variants(self) -> None:
        self.assertEqual(species_to_mygene("mouse"), "mouse")
        self.assertEqual(species_to_mygene("Mus musculus"), "mouse")
        self.assertEqual(species_to_mygene("10090"), "mouse")

    def test_species_to_mygene_pseudomonas(self) -> None:
        self.assertEqual(species_to_mygene("Pseudomonas aeruginosa"), "Pseudomonas aeruginosa")
        self.assertEqual(species_to_mygene("Pseudomonas_aeruginosa"), "Pseudomonas aeruginosa")
        self.assertEqual(species_to_mygene("Pseudomonas aeruginosa UCBPP-PA14"), "Pseudomonas aeruginosa")

    def test_species_to_kegg_pseudomonas_pa14(self) -> None:
        self.assertEqual(species_to_kegg_organism("Pseudomonas aeruginosa"), "pau")
        self.assertEqual(species_to_kegg_organism("pseudomonas_aeruginosa_ucbpp_pa14"), "pau")

    def test_species_to_mygene_unknown_passthrough(self) -> None:
        self.assertEqual(species_to_mygene("danio_rerio"), "danio_rerio")

    def test_species_to_ensembl_human_variants(self) -> None:
        self.assertEqual(species_to_ensembl("human"), "homo_sapiens")
        self.assertEqual(species_to_ensembl("Homo sapiens"), "homo_sapiens")
        self.assertEqual(species_to_ensembl("9606"), "homo_sapiens")

    def test_species_to_ensembl_mouse_variants(self) -> None:
        self.assertEqual(species_to_ensembl("mouse"), "mus_musculus")
        self.assertEqual(species_to_ensembl("Mus musculus"), "mus_musculus")
        self.assertEqual(species_to_ensembl("10090"), "mus_musculus")

    def test_species_to_ensembl_pseudomonas(self) -> None:
        self.assertEqual(species_to_ensembl("Pseudomonas aeruginosa"), "pseudomonas_aeruginosa")
        self.assertEqual(species_to_ensembl("Pseudomonas_aeruginosa"), "pseudomonas_aeruginosa")

    def test_species_to_ensembl_unknown_passthrough(self) -> None:
        self.assertEqual(species_to_ensembl("danio_rerio"), "danio_rerio")
