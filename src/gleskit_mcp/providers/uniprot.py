from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text, species_to_uniprot_taxon


@dataclass(slots=True)
class ProteinMatch:
    query: str
    accession: str | None
    entry_name: str | None
    protein_name: str | None
    gene_primary: str | None
    gene_names: list[str]
    organism_name: str | None
    organism_id: int | None
    reviewed: bool
    length: int | None
    ensembl_gene_ids: list[str]
    matched_on: str | None
    matched_value: str | None


class UniProtProvider:
    name = "uniprot"
    _base_url = "https://rest.uniprot.org"

    def search_proteins(self, query: str, *, species: str = "human", size: int = 5) -> ProviderResponse:
        cleaned_query = clean_text(query)
        if not cleaned_query:
            raise ProviderError("UniProt search requires a non-empty query.")

        taxon_id = species_to_uniprot_taxon(species)
        effective_query = cleaned_query if taxon_id is None else f"({cleaned_query}) AND organism_id:{taxon_id}"
        params = {
            "query": effective_query,
            "format": "json",
            "size": max(1, min(int(size), 10)),
        }
        url = f"{self._base_url}/uniprotkb/search?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"UniProt HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"UniProt network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"UniProt parse error: {type(exc).__name__}: {exc}") from exc

        results = raw.get("results", []) if isinstance(raw, dict) else []
        matches = [self._match_from_row(cleaned_query, row) for row in results if isinstance(row, dict)]
        matches.sort(key=lambda item: _protein_match_sort_key(item, cleaned_query), reverse=True)
        payload = {
            "query": cleaned_query,
            "effective_query": effective_query,
            "species": species,
            "match_count": len(matches),
            "matches": [asdict(item) for item in matches],
        }
        summary = (
            f"Resolved {len(matches)} protein candidate(s) for '{cleaned_query}'."
            if matches
            else f"No UniProt protein matches were found for '{cleaned_query}'."
        )
        return ProviderResponse(provider=self.name, summary=summary, payload=payload)

    def get_protein(self, accession: str) -> ProviderResponse:
        cleaned_accession = clean_text(accession)
        if not cleaned_accession:
            raise ProviderError("UniProt protein lookup requires a non-empty accession.")

        url = f"{self._base_url}/uniprotkb/{cleaned_accession}.json"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"UniProt HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"UniProt network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"UniProt parse error: {type(exc).__name__}: {exc}") from exc

        if not isinstance(raw, dict) or not raw.get("primaryAccession"):
            raise ProviderError(f"UniProt returned no entry for {cleaned_accession}.")

        payload = self._protein_payload(raw)
        summary = (
            f"Retrieved UniProt protein context for {payload.get('protein_name') or payload.get('accession') or cleaned_accession}."
        )
        return ProviderResponse(provider=self.name, summary=summary, payload=payload)

    def _match_from_row(self, query: str, row: dict[str, Any]) -> ProteinMatch:
        protein_payload = self._protein_payload(row)
        return ProteinMatch(
            query=query,
            accession=protein_payload.get("accession"),
            entry_name=protein_payload.get("entry_name"),
            protein_name=protein_payload.get("protein_name"),
            gene_primary=protein_payload.get("gene_primary"),
            gene_names=list(protein_payload.get("gene_names", [])),
            organism_name=protein_payload.get("organism_name"),
            organism_id=protein_payload.get("organism_id"),
            reviewed=bool(protein_payload.get("reviewed")),
            length=protein_payload.get("length"),
            ensembl_gene_ids=list(protein_payload.get("ensembl_gene_ids", [])),
            matched_on=_matched_on(query, protein_payload),
            matched_value=_matched_value(query, protein_payload),
        )

    def _protein_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        gene_names = _gene_names(row.get("genes"))
        subcellular_locations = _subcellular_locations(row.get("comments"))
        function_summary = _function_summary(row.get("comments"))
        domains = _domains(row.get("features"))
        keywords = _keywords(row.get("keywords"))
        return {
            "accession": clean_text(row.get("primaryAccession")) or None,
            "entry_name": clean_text(row.get("uniProtkbId")) or None,
            "protein_name": _protein_name(row.get("proteinDescription")),
            "recommended_name": _recommended_name(row.get("proteinDescription")),
            "alternative_names": _alternative_names(row.get("proteinDescription")),
            "gene_primary": gene_names[0] if gene_names else None,
            "gene_names": gene_names,
            "organism_name": clean_text((row.get("organism") or {}).get("scientificName")) or None,
            "organism_id": _int_or_none((row.get("organism") or {}).get("taxonId")),
            "reviewed": "reviewed" in clean_text(row.get("entryType")).lower(),
            "length": _int_or_none((row.get("sequence") or {}).get("length")),
            "function_summary": function_summary,
            "subcellular_locations": subcellular_locations,
            "ensembl_gene_ids": _ensembl_gene_ids(row.get("uniProtKBCrossReferences")),
            "domains": domains,
            "keywords": keywords,
        }


def _protein_name(value: Any) -> str | None:
    recommended = _recommended_name(value)
    if recommended:
        return recommended
    alternatives = _alternative_names(value)
    return alternatives[0] if alternatives else None


def _recommended_name(value: Any) -> str | None:
    recommended = ((value or {}).get("recommendedName") or {}) if isinstance(value, dict) else {}
    return clean_text((((recommended or {}).get("fullName") or {}).get("value"))) or None


def _alternative_names(value: Any) -> list[str]:
    alternatives = ((value or {}).get("alternativeNames") or []) if isinstance(value, dict) else []
    out: list[str] = []
    for item in alternatives:
        if not isinstance(item, dict):
            continue
        token = clean_text((((item or {}).get("fullName") or {}).get("value")))
        if token and token not in out:
            out.append(token)
    return out


def _gene_names(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        gene_name = clean_text((((item or {}).get("geneName") or {}).get("value")))
        if gene_name and gene_name not in out:
            out.append(gene_name)
        for synonym in item.get("synonyms", []) or []:
            token = clean_text((synonym or {}).get("value"))
            if token and token not in out:
                out.append(token)
    return out


def _function_summary(value: Any) -> str | None:
    parts: list[str] = []
    for item in value or []:
        if not isinstance(item, dict) or clean_text(item.get("commentType")).upper() != "FUNCTION":
            continue
        for text in item.get("texts", []) or []:
            token = clean_text((text or {}).get("value"))
            if token and token not in parts:
                parts.append(token)
    joined = " ".join(parts).strip()
    return joined or None


def _subcellular_locations(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        if not isinstance(item, dict) or clean_text(item.get("commentType")).upper() != "SUBCELLULAR LOCATION":
            continue
        for location in item.get("subcellularLocations", []) or []:
            token = clean_text((((location or {}).get("location") or {}).get("value")))
            if token and token not in out:
                out.append(token)
    return out


def _domains(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        feature_type = clean_text(item.get("type"))
        if feature_type.lower() not in {"domain", "region", "motif", "repeat"}:
            continue
        out.append(
            {
                "type": feature_type or None,
                "description": clean_text(item.get("description")) or None,
                "start": _location_value(((item.get("location") or {}).get("start"))),
                "end": _location_value(((item.get("location") or {}).get("end"))),
            }
        )
    return out


def _keywords(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        token = clean_text((item or {}).get("name")) if isinstance(item, dict) else clean_text(item)
        if token and token not in out:
            out.append(token)
    return out


def _ensembl_gene_ids(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        if not isinstance(item, dict) or clean_text(item.get("database")) != "Ensembl":
            continue
        identifier = clean_text(item.get("id"))
        if identifier.startswith("ENSG") and identifier not in out:
            out.append(identifier)
        for prop in item.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            token = clean_text(prop.get("value"))
            if token.startswith("ENSG") and token not in out:
                out.append(token)
    return out


def _location_value(value: Any) -> int | str | None:
    if isinstance(value, dict):
        if "value" in value:
            parsed = _int_or_none(value.get("value"))
            return parsed if parsed is not None else clean_text(value.get("value")) or None
        return None
    parsed = _int_or_none(value)
    return parsed if parsed is not None else clean_text(value) or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _protein_match_sort_key(match: ProteinMatch, query: str) -> tuple[int, int, int]:
    return (
        _protein_match_rank(match, query),
        1 if match.reviewed else 0,
        int(match.length or 0),
    )


def _protein_match_rank(match: ProteinMatch, query: str) -> int:
    token = clean_text(query).upper()
    if not token:
        return 0
    accession = (match.accession or "").upper()
    entry_name = (match.entry_name or "").upper()
    protein_name = (match.protein_name or "").upper()
    gene_primary = (match.gene_primary or "").upper()
    gene_names = [(item or "").upper() for item in match.gene_names]
    if accession == token:
        return 7
    if entry_name == token:
        return 6
    if gene_primary == token:
        return 5
    if token in gene_names:
        return 4
    if protein_name == token:
        return 3
    if accession.startswith(token) or entry_name.startswith(token):
        return 2
    if token in protein_name:
        return 1
    return 0


def _matched_on(query: str, payload: dict[str, Any]) -> str | None:
    token = clean_text(query).upper()
    if not token:
        return None
    accession = clean_text(payload.get("accession")).upper()
    entry_name = clean_text(payload.get("entry_name")).upper()
    gene_primary = clean_text(payload.get("gene_primary")).upper()
    gene_names = [clean_text(item).upper() for item in payload.get("gene_names", [])]
    protein_name = clean_text(payload.get("protein_name")).upper()
    if accession == token:
        return "accession"
    if entry_name == token:
        return "entry_name"
    if gene_primary == token:
        return "gene_primary"
    if token in gene_names:
        return "gene_name"
    if protein_name == token:
        return "protein_name"
    if accession.startswith(token) or entry_name.startswith(token):
        return "identifier_prefix"
    if token in protein_name:
        return "protein_name_partial"
    return None


def _matched_value(query: str, payload: dict[str, Any]) -> str | None:
    token = clean_text(query).upper()
    if not token:
        return None
    for candidate in [
        payload.get("accession"),
        payload.get("entry_name"),
        payload.get("gene_primary"),
        *(payload.get("gene_names") or []),
        payload.get("protein_name"),
    ]:
        cleaned = clean_text(candidate)
        if cleaned and token in cleaned.upper():
            return cleaned
    return None
