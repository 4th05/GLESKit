from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.uniprot import UniProtProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class UniProtProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.uniprot.urlopen")
    def test_search_proteins_ranks_exact_gene_match(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "results": [
                    {
                        "primaryAccession": "Q99999",
                        "uniProtkbId": "TP53P_HUMAN",
                        "entryType": "UniProtKB unreviewed (TrEMBL)",
                        "proteinDescription": {
                            "recommendedName": {"fullName": {"value": "Tumor protein 53-like candidate"}}
                        },
                        "genes": [{"geneName": {"value": "TP53P"}}],
                        "organism": {"scientificName": "Homo sapiens", "taxonId": 9606},
                        "sequence": {"length": 200},
                    },
                    {
                        "primaryAccession": "P04637",
                        "uniProtkbId": "P53_HUMAN",
                        "entryType": "UniProtKB reviewed (Swiss-Prot)",
                        "proteinDescription": {
                            "recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}
                        },
                        "genes": [{"geneName": {"value": "TP53"}}],
                        "organism": {"scientificName": "Homo sapiens", "taxonId": 9606},
                        "sequence": {"length": 393},
                    },
                ]
            }
        )

        provider = UniProtProvider()
        result = provider.search_proteins("TP53")

        self.assertEqual(result.payload["matches"][0]["accession"], "P04637")
        self.assertEqual(result.payload["matches"][0]["matched_on"], "gene_primary")

    @patch("gleskit_mcp.providers.uniprot.urlopen")
    def test_get_protein_parses_function_domain_and_locations(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "primaryAccession": "P04637",
                "uniProtkbId": "P53_HUMAN",
                "entryType": "UniProtKB reviewed (Swiss-Prot)",
                "proteinDescription": {
                    "recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}},
                    "alternativeNames": [{"fullName": {"value": "Tumor suppressor p53"}}],
                },
                "genes": [
                    {
                        "geneName": {"value": "TP53"},
                        "synonyms": [{"value": "P53"}],
                    }
                ],
                "organism": {"scientificName": "Homo sapiens", "taxonId": 9606},
                "sequence": {"length": 393},
                "comments": [
                    {
                        "commentType": "FUNCTION",
                        "texts": [{"value": "Acts as a tumor suppressor."}],
                    },
                    {
                        "commentType": "SUBCELLULAR LOCATION",
                        "subcellularLocations": [{"location": {"value": "Nucleus"}}],
                    },
                ],
                "features": [
                    {
                        "type": "Domain",
                        "description": "DNA-binding domain",
                        "location": {"start": {"value": 102}, "end": {"value": 292}},
                    }
                ],
                "keywords": [{"name": "Reference proteome"}],
                "uniProtKBCrossReferences": [
                    {"database": "Ensembl", "id": "ENST00000269305", "properties": [{"value": "ENSG00000141510"}]}
                ],
            }
        )

        provider = UniProtProvider()
        result = provider.get_protein("P04637")

        self.assertEqual(result.payload["protein_name"], "Cellular tumor antigen p53")
        self.assertEqual(result.payload["gene_primary"], "TP53")
        self.assertEqual(result.payload["subcellular_locations"], ["Nucleus"])
        self.assertEqual(result.payload["ensembl_gene_ids"], ["ENSG00000141510"])
        self.assertEqual(result.payload["domains"][0]["description"], "DNA-binding domain")


if __name__ == "__main__":
    unittest.main()
