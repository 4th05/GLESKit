from __future__ import annotations

import json
import os
from urllib.parse import quote
from urllib.request import urlopen
import unittest


RUN_EXTERNAL_SMOKE = os.environ.get("GLESKIT_RUN_EXTERNAL_DISEASE_NORMALIZER_TESTS") == "1"


@unittest.skipUnless(
    RUN_EXTERNAL_SMOKE,
    "Set GLESKIT_RUN_EXTERNAL_DISEASE_NORMALIZER_TESTS=1 to run external disease normalizer smoke tests.",
)
class DiseaseNormalizerSmokeTests(unittest.TestCase):
    """Quick evaluation of the public disease-normalizer service for disease-first queries.

    The local package is not a lightweight drop-in dependency for this repo because it requires
    a separate storage backend and loaded data. This smoke test instead checks the hosted service
    that the project itself documents for normalization behavior.
    """

    def _normalize(self, query: str) -> dict:
        url = f"https://normalize.cancervariants.org/disease/normalize?q={quote(query)}"
        with urlopen(url, timeout=20) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload.get("query"), query)
        return payload

    def test_normalizes_nsclc_to_a_structured_disease_record(self) -> None:
        payload = self._normalize("NSCLC")

        disease = payload.get("disease")
        self.assertIsInstance(disease, dict)
        self.assertEqual(disease.get("conceptType"), "Disease")
        self.assertTrue(disease.get("name"))

        primary_id = disease.get("primaryCode", "")
        self.assertTrue(primary_id)
        self.assertIn(primary_id.split(":", 1)[0], {"ncit", "mondo", "doid", "omim", "oncotree"})

    def test_returns_aliases_or_mappings_for_a_common_disease_name(self) -> None:
        payload = self._normalize("liver cancer")

        disease = payload.get("disease") or {}
        self.assertTrue(disease.get("name"))

        aliases = []
        for extension in disease.get("extensions") or []:
            if extension.get("name") == "aliases" and isinstance(extension.get("value"), list):
                aliases = extension["value"]
                break

        mappings = disease.get("mappings") or disease.get("xrefs") or []
        self.assertTrue(aliases or mappings)


if __name__ == "__main__":
    unittest.main()
