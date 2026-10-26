from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.expression_atlas import ExpressionAtlasProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ExpressionAtlasProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.expression_atlas.urlopen")
    def test_reference_expression_parses_reference_experiment_and_query_profile(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "columnHeaders": [
                    {
                        "assayGroupId": "g1",
                        "factorValue": "adipose tissue",
                        "factorValueOntologyTermId": "UBERON_0001013",
                        "assayGroupSummary": {"replicates": 3},
                    },
                    {
                        "assayGroupId": "g2",
                        "factorValue": "liver",
                        "factorValueOntologyTermId": "UBERON_0002107",
                        "assayGroupSummary": {"replicates": 3},
                    },
                ],
                "profiles": {
                    "searchResultTotal": "9570",
                    "rows": [
                        {
                            "id": "ENSG00000141510",
                            "name": "TP53",
                            "expressionUnit": "tpms",
                            "uri": "genes/ENSG00000141510",
                            "expressions": [{}, {"value": 21.0}],
                        }
                    ],
                },
                "experiment": {
                    "accession": "E-MTAB-2836",
                    "type": "rnaseq_mrna_baseline",
                    "description": "Reference human tissue baseline RNA-seq study.",
                    "species": "Homo sapiens",
                    "urls": {
                        "main_page": "experiments/E-MTAB-2836",
                        "download": "experiments-content/E-MTAB-2836/download/RNASEQ_MRNA_BASELINE",
                        "genome_browsers": "experiments/E-MTAB-2836/redirect/genome-browsers",
                    },
                },
            }
        )

        provider = ExpressionAtlasProvider()
        result = provider.reference_expression("TP53", species="human", alternate_identifiers=["ENSG00000141510"], limit=5)

        self.assertEqual(result.payload["reference_experiment"]["accession"], "E-MTAB-2836")
        self.assertEqual(result.payload["assay_group_count"], 2)
        self.assertEqual(result.payload["query_gene_profile"]["gene_name"], "TP53")
        self.assertEqual(result.payload["query_gene_profile"]["tissues"][0]["tissue"], "liver")


if __name__ == "__main__":
    unittest.main()
