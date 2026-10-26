from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import os
from urllib.parse import quote
from urllib.request import urlopen
import unittest


RUN_EXTERNAL_PROTOTYPE = os.environ.get("GLESKIT_RUN_EXTERNAL_DISEASE_PROTOTYPE_TESTS") == "1"
ONTOLOGY_FILTER = "mondo,doid,efo,mesh,snomed,ncit"
SEED_TERMS = ("color blindness", "color vision deficiency", "daltonism")


@dataclass(frozen=True)
class _PrototypeMatch:
    score: float
    obo_id: str
    ontology_name: str
    label: str
    matched_on: str
    description: str


class _PrototypeResolver:
    """Throwaway ontology-backed resolver used only for feasibility testing.

    This is intentionally isolated to tests. It approximates a future disease-first
    resolver by combining:
    - open ontology search for a small seed candidate set
    - local fuzzy reranking over labels and ontology synonyms
    """

    def __init__(self) -> None:
        self._docs = self._load_seed_candidates()

    def _load_seed_candidates(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for term in SEED_TERMS:
            url = f"https://www.ebi.ac.uk/ols4/api/search?q={quote(term)}&ontology={ONTOLOGY_FILTER}"
            with urlopen(url, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for doc in payload.get("response", {}).get("docs", []):
                if doc.get("type") != "class":
                    continue
                obo_id = doc.get("obo_id")
                if obo_id and obo_id not in seen:
                    seen[obo_id] = doc
        return list(seen.values())

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())

    def _score_doc(self, query: str, doc: dict) -> _PrototypeMatch:
        normalized_query = self._normalize(query)
        query_tokens = set(normalized_query.split())
        names = [doc.get("label", "")]
        names.extend(doc.get("exact_synonyms") or [])
        names.extend(doc.get("related_synonyms") or [])

        best_score = -1.0
        best_name = ""
        for name in names:
            normalized_name = self._normalize(name)
            if not normalized_name:
                continue
            char_similarity = SequenceMatcher(None, normalized_query, normalized_name).ratio()
            token_overlap = len(query_tokens & set(normalized_name.split())) / max(
                1,
                len(query_tokens | set(normalized_name.split())),
            )
            token_bonus = 0.15 if len(normalized_name.split()) >= 2 and query_tokens & set(normalized_name.split()) else 0.0
            score = max(char_similarity, token_overlap) + token_bonus
            if score > best_score:
                best_score = score
                best_name = name

        descriptions = doc.get("description") or []
        description = descriptions[0] if descriptions else ""
        return _PrototypeMatch(
            score=best_score,
            obo_id=doc.get("obo_id", ""),
            ontology_name=doc.get("ontology_name", ""),
            label=doc.get("label", ""),
            matched_on=best_name,
            description=description,
        )

    def resolve(self, query: str, *, top_n: int = 5) -> list[_PrototypeMatch]:
        ranked = sorted((self._score_doc(query, doc) for doc in self._docs), key=lambda item: item.score, reverse=True)
        return ranked[:top_n]


@unittest.skipUnless(
    RUN_EXTERNAL_PROTOTYPE,
    "Set GLESKIT_RUN_EXTERNAL_DISEASE_PROTOTYPE_TESTS=1 to run external disease prototype tests.",
)
class DiseaseResolutionPrototypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolver = _PrototypeResolver()

    def test_visual_color_deficiency_returns_relevant_ontology_backed_matches(self) -> None:
        matches = self.resolver.resolve("visual color deficiency")
        labels = {match.label.lower() for match in matches}
        ids = {match.obo_id for match in matches}

        self.assertTrue(
            any(
                phrase in label
                for label in labels
                for phrase in ("color blindness", "color vision", "color-vision")
            )
        )
        self.assertTrue(any(prefix in ids for prefix in ("MONDO:0001703", "DOID:13399", "NCIT:C3891", "mesh:D003117")))

    def test_typo_variant_still_returns_relevant_matches(self) -> None:
        matches = self.resolver.resolve("visul color deficency")
        labels = [match.label.lower() for match in matches]

        self.assertTrue(
            any(
                phrase in label
                for label in labels
                for phrase in ("color blindness", "color vision", "color-vision")
            )
        )
        self.assertTrue(all(match.obo_id and match.ontology_name for match in matches[:3]))


if __name__ == "__main__":
    unittest.main()
