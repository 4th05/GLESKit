from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs

from gleskit_mcp.providers.mygene import MyGeneProvider, _match_sort_key


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class MyGeneProviderTests(unittest.TestCase):
    def test_alias_match_is_ranked_ahead_of_generic_score_order(self) -> None:
        provider = MyGeneProvider()
        query = "biglycan"
        alias_hit = {
            "symbol": "BGN",
            "name": "biglycan",
            "alias": ["biglycan", "PG-S1"],
            "ensembl": {"gene": "ENSG00000182492"},
            "_score": 5.0,
        }
        generic_hit = {
            "symbol": "BGNL",
            "name": "Biglycan-like candidate",
            "alias": ["BGN-like"],
            "ensembl": {"gene": "ENSG00000999999"},
            "_score": 9.0,
        }

        alias_match = provider._match_from_hit(query, alias_hit)
        generic_match = provider._match_from_hit(query, generic_hit)
        ranked = sorted(
            [generic_match, alias_match],
            key=lambda item: _match_sort_key(item, query),
            reverse=True,
        )

        self.assertEqual(ranked[0].symbol, "BGN")
        self.assertEqual(ranked[0].matched_on, "alias")

    def test_resolve_genes_uses_a_single_post_batch_for_many_identifiers(self) -> None:
        requests: list[dict[str, list[str]]] = []

        def opener(request, timeout=0):
            del timeout
            self.assertEqual(request.method, "POST")
            values = parse_qs((request.data or b"").decode("utf-8"))
            requests.append(values)
            return _Response(
                [
                    {"query": "ENSG00000141510", "symbol": "TP53", "ensembl": {"gene": "ENSG00000141510"}},
                    {"query": "ENSG00000182492", "symbol": "BGN", "ensembl": {"gene": "ENSG00000182492"}},
                    {"query": "MISSING", "notfound": True},
                ]
            )

        resolved = MyGeneProvider(opener=opener).resolve_genes(
            ["ENSG00000141510", "ENSG00000182492", "MISSING"], species="human"
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["q"], ["ENSG00000141510,ENSG00000182492,MISSING"])
        self.assertEqual(set(resolved), {"ENSG00000141510", "ENSG00000182492"})
        self.assertEqual(resolved["ENSG00000141510"].payload["matches"][0]["symbol"], "TP53")


if __name__ == "__main__":
    unittest.main()
