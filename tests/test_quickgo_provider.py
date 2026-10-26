from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.quickgo import QuickGOProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class QuickGOProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.quickgo.urlopen")
    def test_gene_product_annotations_collect_unique_terms(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "results": [
                    {
                        "geneProductId": "UniProtKB:P04637",
                        "geneProductSymbol": "TP53",
                        "goId": "GO:0006977",
                        "goName": "DNA damage response",
                        "aspect": "biological_process",
                        "evidenceCode": "IDA",
                        "reference": ["PMID:1"],
                    },
                    {
                        "geneProductId": "UniProtKB:P04637",
                        "geneProductSymbol": "TP53",
                        "goId": "GO:0006977",
                        "goName": "DNA damage response",
                        "aspect": "biological_process",
                        "evidenceCode": "IMP",
                        "reference": ["PMID:2"],
                    },
                ]
            }
        )

        provider = QuickGOProvider()
        result = provider.gene_product_annotations("UniProtKB:P04637")

        self.assertEqual(result.payload["annotation_count"], 2)
        self.assertEqual(len(result.payload["terms"]), 1)
        self.assertEqual(result.payload["terms"][0]["go_id"], "GO:0006977")


if __name__ == "__main__":
    unittest.main()
