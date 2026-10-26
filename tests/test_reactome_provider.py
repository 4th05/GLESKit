from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.reactome import ReactomeProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict | list[dict]):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ReactomeProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.reactome.urlopen")
    def test_pathway_parses_summary_and_participants(self, mocked_urlopen) -> None:
        mocked_urlopen.side_effect = [
            _FakeHTTPResponse(
                {
                    "stId": "R-HSA-69620",
                    "displayName": "Cell Cycle Checkpoints",
                    "speciesName": "Homo sapiens",
                    "schemaClass": "Pathway",
                    "isInDisease": False,
                    "isInferred": False,
                    "summation": [{"text": "Checkpoint control of the cell cycle."}],
                    "compartment": [{"displayName": "nucleoplasm"}],
                }
            ),
            _FakeHTTPResponse(
                [
                    {
                        "stId": "R-HSA-TP53",
                        "displayName": "TP53",
                        "schemaClass": "EntityWithAccessionedSequence",
                    },
                    {
                        "stId": "R-HSA-CDK2",
                        "displayName": "CDK2",
                        "schemaClass": "EntityWithAccessionedSequence",
                    },
                ]
            ),
        ]

        provider = ReactomeProvider()
        result = provider.pathway("R-HSA-69620")

        self.assertEqual(result.payload["pathway_id"], "R-HSA-69620")
        self.assertEqual(result.payload["display_name"], "Cell Cycle Checkpoints")
        self.assertEqual(result.payload["summary"], "Checkpoint control of the cell cycle.")
        self.assertEqual(result.payload["compartment"], "nucleoplasm")
        self.assertEqual(result.payload["participant_count"], 2)
        self.assertEqual(result.payload["participants"][0]["display_name"], "TP53")


if __name__ == "__main__":
    unittest.main()
