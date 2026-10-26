from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.ensembl import EnsemblProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class EnsemblProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.ensembl.urlopen")
    def test_lookup_gene_parses_transcripts_and_proteins(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "id": "ENSG00000141510",
                "display_name": "TP53",
                "description": "tumor protein p53 [Source:HGNC Symbol;Acc:HGNC:11998]",
                "species": "homo_sapiens",
                "assembly_name": "GRCh38",
                "seq_region_name": "17",
                "start": 7661779,
                "end": 7687550,
                "strand": -1,
                "biotype": "protein_coding",
                "canonical_transcript": "ENST00000269305",
                "Transcript": [
                    {
                        "id": "ENST00000269305",
                        "biotype": "protein_coding",
                        "is_canonical": 1,
                        "Translation": {"id": "ENSP00000269305"},
                    },
                    {
                        "id": "ENST00000420246",
                        "biotype": "processed_transcript",
                    },
                ],
            }
        )

        provider = EnsemblProvider()
        result = provider.lookup_gene("ENSG00000141510")

        self.assertEqual(result.payload["display_name"], "TP53")
        self.assertEqual(result.payload["description"], "tumor protein p53")
        self.assertEqual(result.payload["transcript_count"], 2)
        self.assertEqual(result.payload["protein_coding_transcript_count"], 1)
        self.assertEqual(result.payload["protein_ids"], ["ENSP00000269305"])

    @patch("gleskit_mcp.providers.ensembl.urlopen")
    def test_resolve_genes_posts_stable_ids_in_a_batch(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "ENSG00000141510": {
                    "id": "ENSG00000141510",
                    "display_name": "TP53",
                    "description": "tumor protein p53",
                    "biotype": "protein_coding",
                },
                "ENSG00000182492": {
                    "id": "ENSG00000182492",
                    "display_name": "BGN",
                    "description": "biglycan",
                    "biotype": "protein_coding",
                },
            }
        )

        resolved = EnsemblProvider().resolve_genes(["ENSG00000141510", "ENSG00000182492", "TP53"])

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads((request.data or b"{}").decode("utf-8"))["ids"], ["ENSG00000141510", "ENSG00000182492"])
        self.assertEqual(set(resolved), {"ENSG00000141510", "ENSG00000182492"})
        self.assertEqual(resolved["ENSG00000141510"].payload["matches"][0]["symbol"], "TP53")


if __name__ == "__main__":
    unittest.main()
