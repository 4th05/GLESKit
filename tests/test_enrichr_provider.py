from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse
import unittest

from gleskit_mcp.providers.enrichr import EnrichrProvider, normalize_enrichr_library


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class EnrichrProviderTests(unittest.TestCase):
    def test_normalizes_supported_benchmark_library_names(self) -> None:
        self.assertEqual(normalize_enrichr_library("GO_Biological_Process_2021"), "GO_Biological_Process_2021")
        self.assertEqual(normalize_enrichr_library("reactome-2022"), "Reactome_2022")
        self.assertEqual(normalize_enrichr_library("GO_BP"), "")

    def test_submits_exact_library_and_standardizes_gseapy_rows(self) -> None:
        requests: list[tuple[str, dict[str, str]]] = []

        def opener(request, timeout=0):
            del timeout
            url = str(request.full_url)
            if url.endswith("/addList"):
                body = (request.data or b"").decode("utf-8")
                requests.append((url, {"body": body, "content_type": request.headers.get("Content-type", "")}))
                return _Response({"userListId": 42})
            query = parse_qs(urlparse(url).query)
            requests.append((url, {key: values[0] for key, values in query.items()}))
            return _Response(
                {
                    "GO_Biological_Process_2021": [
                        [
                            1,
                            "Canonical glycolysis",
                            0.0001,
                            1.2,
                            4.5,
                            ["ENO1", "GAPDH"],
                            0.001,
                        ]
                    ]
                }
            )

        result = EnrichrProvider(opener=opener).enrich_genes(
            ["ENO1", "GAPDH"],
            library="GO_Biological_Process_2021",
        )

        self.assertIn("multipart/form-data", requests[0][1]["content_type"].lower())
        self.assertIn("ENO1\nGAPDH", requests[0][1]["body"])
        self.assertEqual(requests[1][1]["backgroundType"], "GO_Biological_Process_2021")
        self.assertEqual(result.provider, "enrichr")
        self.assertEqual(result.payload["backend"], "gseapy_enrichr")
        self.assertEqual(result.payload["library"], "GO_Biological_Process_2021")
        self.assertEqual(result.payload["terms"][0]["term_name"], "Canonical glycolysis")
        self.assertEqual(result.payload["terms"][0]["adjusted_p_value"], 0.001)

    def test_uses_speedrichr_when_a_custom_background_is_provided(self) -> None:
        requests: list[tuple[str, str, dict[str, str]]] = []

        def opener(request, timeout=0):
            del timeout
            url = str(request.full_url)
            body = (request.data or b"").decode("utf-8")
            requests.append((url, str(request.method), {"body": body}))
            if url.endswith("/addList"):
                return _Response({"userListId": 17})
            if url.endswith("/addbackground"):
                return _Response({"backgroundid": "background-17"})
            if url.endswith("/backgroundenrich"):
                return _Response(
                    {
                        "KEGG_2019_Mouse": [
                            [1, "Cell cycle", 0.01, 2.0, 5.0, ["Trp53", "Cdk2"], 0.02]
                        ]
                    }
                )
            raise AssertionError(f"Unexpected request: {url}")

        result = EnrichrProvider(opener=opener).enrich_genes(
            ["Trp53", "Cdk2"],
            library="KEGG_2019_Mouse",
            species="Mus musculus",
            background_genes=["Trp53", "Cdk2", "Brca1"],
        )

        self.assertEqual(len(requests), 3)
        self.assertTrue(all(url.startswith("https://maayanlab.cloud/speedrichr/api/") for url, _, _ in requests))
        self.assertEqual(requests[1][1], "POST")
        self.assertIn("background=", requests[1][2]["body"])
        self.assertIn("backgroundid=background-17", requests[2][2]["body"])
        self.assertEqual(result.payload["species"], "mouse")
        self.assertEqual(result.payload["library"], "KEGG_2019_Mouse")
        self.assertEqual(result.payload["terms"][0]["term_name"], "Cell cycle")


if __name__ == "__main__":
    unittest.main()
