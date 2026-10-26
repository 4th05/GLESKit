from __future__ import annotations

import unittest

from gleskit_mcp.providers.gprofiler import GProfilerProvider


class GProfilerProviderTests(unittest.TestCase):
    def test_term_from_row_preserves_intersection_genes(self) -> None:
        provider = GProfilerProvider()
        term = provider._term_from_row(
            {
                "native": "GO:0007049",
                "name": "cell cycle",
                "source": "GO:BP",
                "p_value": 1.2e-6,
                "intersection_size": 3,
                "intersections": ["CDC20", "TOP2A", "BIRC5"],
            }
        )

        self.assertEqual(term["matched_genes"], ["CDC20", "TOP2A", "BIRC5"])
        self.assertEqual(term["intersection_size"], 3)


if __name__ == "__main__":
    unittest.main()
