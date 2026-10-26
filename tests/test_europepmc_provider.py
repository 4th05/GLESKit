from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from gleskit_mcp.providers.europepmc import EuropePMCProvider


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class EuropePMCProviderTests(unittest.TestCase):
    @patch("gleskit_mcp.providers.europepmc.urlopen")
    def test_search_papers_parses_structured_authors(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "resultList": {
                    "result": [
                        {
                            "source": "MED",
                            "id": "12345",
                            "pmid": "12345",
                            "doi": "10.1000/test",
                            "title": "Clock genes in cancer",
                            "authorString": "Smith J, Doe P",
                            "authorList": {
                                "author": [
                                    {"fullName": "Jane Smith", "lastName": "Smith", "firstName": "Jane"},
                                    {"fullName": "Paul Doe", "lastName": "Doe", "firstName": "Paul"},
                                ]
                            },
                            "journalTitle": "Nature",
                            "pubYear": "2025",
                            "citedByCount": "7",
                            "isOpenAccess": "Y",
                            "abstractText": "Study of circadian genes in tumor biology.",
                        }
                    ]
                }
            }
        )

        provider = EuropePMCProvider()
        result = provider.search_papers("clock genes cancer", limit=1)
        paper = result.payload["papers"][0]

        self.assertEqual(paper["authors"], ["Jane Smith", "Paul Doe"])
        self.assertEqual(paper["author_count"], 2)
        self.assertEqual(paper["author_string"], "Smith J, Doe P")
        self.assertEqual(paper["first_author_surname"], "Smith")

    @patch("gleskit_mcp.providers.europepmc.urlopen")
    def test_search_papers_falls_back_to_author_string_when_author_list_missing(self, mocked_urlopen) -> None:
        mocked_urlopen.return_value = _FakeHTTPResponse(
            {
                "resultList": {
                    "result": [
                        {
                            "source": "PMC",
                            "id": "PMC123",
                            "title": "DNA damage and TP53",
                            "authorString": "Garcia M, Nguyen T, Patel R",
                            "journalTitle": "Genome Biology",
                            "pubYear": "2024",
                        }
                    ]
                }
            }
        )

        provider = EuropePMCProvider()
        result = provider.search_papers("TP53 DNA damage", limit=1)
        paper = result.payload["papers"][0]

        self.assertEqual(paper["authors"], ["Garcia M", "Nguyen T", "Patel R"])
        self.assertEqual(paper["author_count"], 3)
        self.assertEqual(paper["first_author_surname"], "Garcia")


if __name__ == "__main__":
    unittest.main()
