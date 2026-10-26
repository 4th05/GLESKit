from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.gtex import GTExProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class GTExProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.gtex.urlopen")
    def test_expression_context_parses_reference_gene_and_sorted_tissues(self, mocked_urlopen) -> None:
        mocked_urlopen.side_effect = [
            _FakeHTTPResponse(
                {
                    "data": [
                        {
                            "geneSymbol": "TP53",
                            "geneSymbolUpper": "TP53",
                            "gencodeId": "ENSG00000141510.16",
                            "gencodeVersion": "v26",
                            "geneType": "protein coding",
                            "description": "tumor protein p53 [Source:HGNC Symbol;Acc:HGNC:11998]",
                            "chromosome": "chr17",
                            "start": 7661779,
                            "end": 7687550,
                            "strand": "-",
                            "entrezGeneId": 7157,
                            "dataSource": "HAVANA",
                            "genomeBuild": "GRCh38/hg38",
                        }
                    ]
                }
            ),
            _FakeHTTPResponse(
                {
                    "data": [
                        {
                            "tissueSiteDetailId": "Breast_Mammary_Tissue",
                            "ontologyId": "UBERON:0008367",
                            "median": 24.5574,
                            "unit": "TPM",
                            "datasetId": "gtex_v8",
                            "gencodeId": "ENSG00000141510.16",
                            "geneSymbol": "TP53",
                        },
                        {
                            "tissueSiteDetailId": "Cells_EBV-transformed_lymphocytes",
                            "ontologyId": "EFO:0000572",
                            "median": 72.9493,
                            "unit": "TPM",
                            "datasetId": "gtex_v8",
                            "gencodeId": "ENSG00000141510.16",
                            "geneSymbol": "TP53",
                        },
                    ]
                }
            ),
        ]

        provider = GTExProvider()
        result = provider.expression_context("TP53", limit=5)

        self.assertEqual(result.payload["reference_gene"]["gene_symbol"], "TP53")
        self.assertEqual(result.payload["dataset_id"], "gtex_v8")
        self.assertEqual(result.payload["tissue_count"], 2)
        self.assertEqual(result.payload["tissues"][0]["tissue_site_detail_id"], "Cells_EBV-transformed_lymphocytes")


if __name__ == "__main__":
    unittest.main()
