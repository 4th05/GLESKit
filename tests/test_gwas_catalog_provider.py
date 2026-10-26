from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.gwas_catalog import GWASCatalogProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class GWASCatalogProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.gwas_catalog.urlopen")
    def test_variant_trait_associations_parse_hal_payload(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "_embedded": {
                    "associations": [
                        {
                            "associationId": "601",
                            "pvalue": 1e-12,
                            "orPerCopyNum": 1.8,
                            "strongestRiskAlleles": [{"riskAlleleName": "rs78378222-T"}],
                            "efoTraits": [{"trait": "Breast carcinoma", "shortForm": "EFO:0000305"}],
                            "mappedGenes": ["TP53"],
                            "reportedGenes": ["TP53"],
                            "study": {
                                "accessionId": "GCST000001",
                                "publicationInfo": {"pubmedId": "12345678"},
                            },
                        }
                    ]
                },
                "page": {"totalElements": 1},
            }
        )

        provider = GWASCatalogProvider()
        result = provider.variant_trait_associations("TP53")

        self.assertEqual(result.payload["gene_symbol"], "TP53")
        self.assertEqual(result.payload["association_count"], 1)
        self.assertEqual(result.payload["associations"][0]["variant_id"], "rs78378222-T")
        self.assertEqual(result.payload["associations"][0]["trait"], "Breast carcinoma")
        self.assertEqual(result.payload["associations"][0]["study_accession"], "GCST000001")


if __name__ == "__main__":
    unittest.main()
