from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import os
from urllib.parse import quote
from urllib.request import urlopen
import unittest


RUN_EXTERNAL_GOLDEN = os.environ.get("GLESKIT_RUN_EXTERNAL_DISEASE_GOLDEN_TESTS") == "1"


@dataclass(frozen=True)
class _GoldenCase:
    query: str
    expected_ids: dict[str, set[str]]


@dataclass(frozen=True)
class _ResolvedDisease:
    original_query: str
    corrected_query: str
    matched_candidate: str | None
    normalized_name: str | None
    ids_by_source: dict[str, str]


class _DiseaseResolutionHarness:
    """External-only disease resolver harness for feasibility and regression testing.

    Pipeline:
    1. NCBI spelling suggestion for typo repair
    2. disease-normalizer normalization for canonical disease concept lookup
    3. Europe PMC title-window fallback when direct normalization fails
    4. MONDO xref expansion via OLS to recover missing ontology mappings such as SNOMED
    """

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())

    def _correct_query(self, query: str) -> str:
        url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/espell.fcgi?db=pubmed&term={quote(query)}"
        with urlopen(url, timeout=20) as response:
            payload = response.read().decode("utf-8")

        start = "<CorrectedQuery>"
        end = "</CorrectedQuery>"
        if start in payload and end in payload:
            corrected = payload.split(start, 1)[1].split(end, 1)[0].strip()
            if corrected:
                return corrected
        return query

    def _normalize_disease(self, query: str) -> dict:
        url = f"https://normalize.cancervariants.org/disease/normalize?q={quote(query)}"
        with urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def _candidate_phrases_from_titles(self, query: str) -> list[str]:
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json&pageSize=8&query={quote(query)}"
        with urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        titles = [
            result.get("title", "")
            for result in payload.get("resultList", {}).get("result", [])
            if result.get("title")
        ]
        normalized_query = self._normalize_text(query)
        query_tokens = set(normalized_query.split())
        scored_phrases: list[tuple[float, str]] = []

        for title in titles:
            tokens = self._normalize_text(title).split()
            for window_size in range(2, 6):
                for idx in range(len(tokens) - window_size + 1):
                    phrase = " ".join(tokens[idx : idx + window_size])
                    overlap = query_tokens & set(phrase.split())
                    if not overlap:
                        continue
                    score = SequenceMatcher(None, normalized_query, phrase).ratio() + 0.1 * len(overlap)
                    scored_phrases.append((score, phrase))

        phrases: list[str] = []
        for _, phrase in sorted(scored_phrases, reverse=True):
            if phrase not in phrases:
                phrases.append(phrase)
        return phrases[:15]

    def _get_mondo_xrefs(self, mondo_code: str) -> list[str]:
        iri = f"http://purl.obolibrary.org/obo/{mondo_code.replace(':', '_')}"
        encoded_iri = quote(quote(iri, safe=""), safe="")
        url = f"https://www.ebi.ac.uk/ols4/api/ontologies/mondo/terms/{encoded_iri}"
        with urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("annotation", {}).get("database_cross_reference", [])

    @staticmethod
    def _ids_from_normalized_payload(payload: dict) -> dict[str, str]:
        disease = payload.get("disease") or {}
        ids: dict[str, str] = {}
        for mapping in disease.get("mappings") or []:
            coding = mapping.get("coding") or {}
            code = coding.get("code") or coding.get("id") or ""
            coding_id = (coding.get("id") or "").lower()
            if code.startswith("MONDO:"):
                ids["MONDO"] = code
            elif code.startswith("DOID:"):
                ids["DOID"] = code
            elif coding_id.startswith("ncit:"):
                ids["NCIT"] = coding["id"].upper()
            elif coding_id.startswith("mesh:"):
                ids["MESH"] = f"MESH:{code}" if not code.startswith("MESH:") else code
            elif coding_id.startswith("orphanet:"):
                ids["ORPHANET"] = f"Orphanet:{code}" if code.isdigit() else coding["id"]
        return ids

    def _expand_from_mondo(self, ids: dict[str, str]) -> None:
        mondo_code = ids.get("MONDO")
        if not mondo_code:
            return
        for xref in self._get_mondo_xrefs(mondo_code):
            if xref.startswith("MESH:"):
                ids.setdefault("MESH", xref)
            elif xref.startswith("NCIT:"):
                ids.setdefault("NCIT", xref)
            elif xref.startswith("Orphanet:"):
                ids.setdefault("ORPHANET", xref)
            elif xref.startswith("SCTID:"):
                ids.setdefault("SNOMED", xref)

    def resolve(self, query: str) -> _ResolvedDisease:
        corrected_query = self._correct_query(query)
        candidate_queries: list[str] = [corrected_query]
        if corrected_query != query:
            candidate_queries.append(query)
        for phrase in self._candidate_phrases_from_titles(corrected_query):
            if phrase not in candidate_queries:
                candidate_queries.append(phrase)

        for candidate in candidate_queries:
            payload = self._normalize_disease(candidate)
            if payload.get("match_type", 0) <= 0 or not payload.get("disease"):
                continue

            ids = self._ids_from_normalized_payload(payload)
            self._expand_from_mondo(ids)
            return _ResolvedDisease(
                original_query=query,
                corrected_query=corrected_query,
                matched_candidate=candidate,
                normalized_name=(payload.get("disease") or {}).get("name"),
                ids_by_source=ids,
            )

        return _ResolvedDisease(
            original_query=query,
            corrected_query=corrected_query,
            matched_candidate=None,
            normalized_name=None,
            ids_by_source={},
        )


CASES = (
    _GoldenCase(
        query="visual color deficiency",
        expected_ids={
            "NCIT": {"NCIT:C3891"},
            "MONDO": {"MONDO:0001703"},
            "MESH": {"MESH:D003117"},
            "ORPHANET": {"Orphanet:98658"},
            "SNOMED": {"SCTID:193683001"},
        },
    ),
    _GoldenCase(
        query="visul color deficency",
        expected_ids={
            "NCIT": {"NCIT:C3891"},
            "MONDO": {"MONDO:0001703"},
            "MESH": {"MESH:D003117"},
            "ORPHANET": {"Orphanet:98658"},
            "SNOMED": {"SCTID:193683001"},
        },
    ),
    _GoldenCase(
        query="heart attack",
        expected_ids={
            "NCIT": {"NCIT:C27996"},
            "MONDO": {"MONDO:0005068"},
            "MESH": {"MESH:D009203"},
            "SNOMED": {"SCTID:22298006"},
        },
    ),
    _GoldenCase(
        query="hart atack",
        expected_ids={
            "NCIT": {"NCIT:C27996"},
            "MONDO": {"MONDO:0005068"},
            "MESH": {"MESH:D009203"},
            "SNOMED": {"SCTID:22298006"},
        },
    ),
    _GoldenCase(
        query="lou gehrig disease",
        expected_ids={
            "NCIT": {"NCIT:C34373"},
            "MONDO": {"MONDO:0004976"},
            "MESH": {"MESH:D000690"},
            "ORPHANET": {"Orphanet:803"},
            "SNOMED": {"SCTID:86044005"},
        },
    ),
    _GoldenCase(
        query="breast cancer",
        expected_ids={
            "NCIT": {"NCIT:C9335"},
            "MONDO": {"MONDO:0007254"},
            "MESH": {"MESH:D001943"},
            "SNOMED": {"SCTID:254837009"},
        },
    ),
    _GoldenCase(
        query="brest canser",
        expected_ids={
            "NCIT": {"NCIT:C9335"},
            "MONDO": {"MONDO:0007254"},
            "MESH": {"MESH:D001943"},
            "SNOMED": {"SCTID:254837009"},
        },
    ),
    _GoldenCase(
        query="diabtes mellitus",
        expected_ids={
            "NCIT": {"NCIT:C2985"},
            "MONDO": {"MONDO:0005015"},
            "MESH": {"MESH:D003920"},
            "SNOMED": {"SCTID:73211009"},
        },
    ),
)


@unittest.skipUnless(
    RUN_EXTERNAL_GOLDEN,
    "Set GLESKIT_RUN_EXTERNAL_DISEASE_GOLDEN_TESTS=1 to run external disease golden-suite tests.",
)
class DiseaseResolutionGoldenSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = _DiseaseResolutionHarness()

    def test_golden_queries_resolve_expected_cross_ontology_ids(self) -> None:
        failures: list[str] = []

        for case in CASES:
            resolved = self.harness.resolve(case.query)
            for source, expected in case.expected_ids.items():
                actual = resolved.ids_by_source.get(source)
                if actual not in expected:
                    failures.append(
                        (
                            f"query={case.query!r} source={source} expected one of {sorted(expected)} "
                            f"but got {actual!r}; corrected={resolved.corrected_query!r}; "
                            f"candidate={resolved.matched_candidate!r}; name={resolved.normalized_name!r}; "
                            f"all_ids={resolved.ids_by_source!r}"
                        )
                    )

        if failures:
            self.fail("\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
