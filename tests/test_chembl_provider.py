from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.chembl import ChEMBLProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ChEMBLProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.chembl.urlopen")
    def test_target_drug_context_merges_mechanisms_and_indications(self, mocked_urlopen) -> None:
        mocked_urlopen.side_effect = [
            _FakeHTTPResponse(
                {
                    "targets": [
                        {
                            "target_chembl_id": "CHEMBL123",
                            "pref_name": "Cellular tumor antigen p53",
                            "target_type": "SINGLE PROTEIN",
                            "organism": "Homo sapiens",
                            "target_components": [{"accession": "P04637"}],
                        }
                    ]
                }
            ),
            _FakeHTTPResponse(
                {
                    "mechanisms": [
                        {
                            "molecule_chembl_id": "CHEMBL25",
                            "molecule_pref_name": "Nutlin-3",
                            "action_type": "INHIBITOR",
                            "mechanism_of_action": "Blocks MDM2-p53 interaction",
                            "max_phase": 2,
                            "first_approval": None,
                        }
                    ]
                }
            ),
            _FakeHTTPResponse(
                {
                    "drug_indications": [
                        {
                            "mesh_heading": "Neoplasms",
                            "efo_id": "EFO:0000616",
                            "efo_term": "neoplasm",
                            "max_phase_for_ind": 2,
                        }
                    ]
                }
            ),
        ]

        provider = ChEMBLProvider()
        result = provider.target_drug_context(uniprot_accession="P04637", gene_symbol="TP53")

        self.assertEqual(result.payload["target"]["target_chembl_id"], "CHEMBL123")
        self.assertEqual(result.payload["mechanism_count"], 1)
        self.assertEqual(result.payload["drug_count"], 1)
        self.assertEqual(result.payload["drugs"][0]["molecule_name"], "Nutlin-3")
        self.assertEqual(result.payload["drugs"][0]["indications"][0]["efo_term"], "neoplasm")


if __name__ == "__main__":
    unittest.main()
