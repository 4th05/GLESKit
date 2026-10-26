from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.stringdb import StringDBProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict | list[dict]):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class StringDBProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.stringdb.urlopen")
    def test_interaction_network_sorts_edges_and_parses_enrichment(self, mocked_urlopen) -> None:
        mocked_urlopen.side_effect = [
            _FakeHTTPResponse(
                [
                    {
                        "preferredName_A": "TP53",
                        "preferredName_B": "ATM",
                        "stringId_A": "9606.ENSP00000269305",
                        "stringId_B": "9606.ENSP00000369497",
                        "score": 0.82,
                        "nscore": 0,
                        "fscore": 0,
                        "pscore": 0,
                        "ascore": 0.1,
                        "escore": 0.8,
                        "dscore": 0.6,
                        "tscore": 0.4,
                    },
                    {
                        "preferredName_A": "TP53",
                        "preferredName_B": "MDM2",
                        "stringId_A": "9606.ENSP00000269305",
                        "stringId_B": "9606.ENSP00000258149",
                        "score": 0.99,
                        "nscore": 0,
                        "fscore": 0,
                        "pscore": 0,
                        "ascore": 0.2,
                        "escore": 0.9,
                        "dscore": 0.7,
                        "tscore": 0.5,
                    },
                ]
            ),
            _FakeHTTPResponse(
                [
                    {
                        "number_of_nodes": 2,
                        "number_of_edges": 1,
                        "expected_number_of_edges": 0.11,
                        "average_node_degree": 1.0,
                        "local_clustering_coefficient": 0.0,
                        "p_value": 1e-5,
                    }
                ]
            ),
        ]

        provider = StringDBProvider()
        result = provider.interaction_network(["TP53", "MDM2"], species="human", limit=5)

        self.assertEqual(result.payload["taxon_id"], 9606)
        self.assertEqual(result.payload["edge_count"], 2)
        self.assertEqual(result.payload["edges"][0]["protein_b"], "MDM2")
        self.assertEqual(result.payload["ppi_enrichment"]["p_value"], 1e-5)


if __name__ == "__main__":
    unittest.main()
