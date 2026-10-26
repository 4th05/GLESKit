from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text, species_to_ensembl


class EnsemblProvider:
    name = "ensembl"
    _base_url = "https://rest.ensembl.org"

    def lookup_gene(self, ensembl_gene_id: str) -> ProviderResponse:
        cleaned_id = clean_text(ensembl_gene_id)
        if not cleaned_id:
            raise ProviderError("Ensembl lookup requires a non-empty gene identifier.")

        url = f"{self._base_url}/lookup/id/{quote(cleaned_id)}?expand=1"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"Ensembl HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"Ensembl network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"Ensembl parse error: {type(exc).__name__}: {exc}") from exc

        if not isinstance(raw, dict) or not raw.get("id"):
            raise ProviderError(f"Ensembl returned no lookup result for {cleaned_id}.")

        payload = self._payload_from_lookup(raw)
        summary = (
            f"Retrieved Ensembl gene context for {payload.get('display_name') or payload.get('ensembl_gene_id') or cleaned_id}."
        )
        return ProviderResponse(provider=self.name, summary=summary, payload=payload)

    def resolve_gene(self, query: str, *, species: str = "human") -> ProviderResponse | None:
        cleaned_query = clean_text(query)
        if not cleaned_query:
            raise ProviderError("Query must not be empty.")

        effective_species = species_to_ensembl(species)
        raw: dict[str, Any] | None = None

        if cleaned_query.upper().startswith("ENS"):
            raw = self._fetch_id_lookup(cleaned_query)
        else:
            raw = self._fetch_symbol_lookup(cleaned_query, effective_species)
            if raw is None:
                raw = self._fetch_id_lookup(cleaned_query)

        if raw is None or not raw.get("id"):
            return None

        ensembl_gene_id = clean_text(raw.get("id")) or ""
        loaded_xrefs: dict[str, Any] = self._fetch_xrefs(ensembl_gene_id)
        match = _match_from_ensembl_lookup(raw, cleaned_query, loaded_xrefs)
        if not match.get("symbol"):
            return None

        summary = f"Resolved 1 candidate gene match for '{cleaned_query}' from Ensembl. Matched on {match.get('matched_on')}."
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "query": cleaned_query,
                "species": effective_species,
                "match_count": 1,
                "matches": [match],
            },
        )

    def resolve_genes(self, queries: list[str], *, species: str = "human", chunk_size: int = 900) -> dict[str, ProviderResponse]:
        """Resolve Ensembl identifiers in batches after local and MyGene lookup.

        The REST lookup endpoint accepts a JSON list of stable IDs, avoiding a
        slow one-request-per-gene fallback for large count-matrix handoffs.
        """
        unique_queries = list(dict.fromkeys(clean_text(query) for query in queries if clean_text(query)))
        ensembl_queries = [query for query in unique_queries if query.upper().startswith("ENS")]
        resolved: dict[str, ProviderResponse] = {}
        effective_chunk_size = max(1, min(int(chunk_size), 900))
        for start in range(0, len(ensembl_queries), effective_chunk_size):
            chunk = ensembl_queries[start : start + effective_chunk_size]
            request = Request(
                f"{self._base_url}/lookup/id",
                data=json.dumps({"ids": chunk}).encode("utf-8"),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raise ProviderError(f"Ensembl batch HTTP error: {exc.code}") from exc
            except URLError as exc:
                raise ProviderError(f"Ensembl batch network error: {exc.reason}") from exc
            except Exception as exc:
                raise ProviderError(f"Ensembl batch parse error: {type(exc).__name__}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ProviderError("Ensembl batch lookup returned an invalid response.")
            for query in chunk:
                raw = payload.get(query)
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                match = _match_from_ensembl_lookup(raw, query, {})
                if not match.get("symbol"):
                    continue
                resolved[query] = ProviderResponse(
                    provider=self.name,
                    summary=f"Resolved 1 candidate gene match for '{query}' from Ensembl batch lookup.",
                    payload={
                        "query": query,
                        "species": species_to_ensembl(species),
                        "match_count": 1,
                        "matches": [match],
                    },
                )
        return resolved

    def _fetch_id_lookup(self, identifier: str) -> dict[str, Any] | None:
        url = f"{self._base_url}/lookup/id/{quote(identifier)}?expand=1"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {400, 404}:
                return None
            raise ProviderError(f"Ensembl HTTP error: {exc.code}") from exc
        except URLError as exc:
            return None
        except Exception as exc:
            raise ProviderError(f"Ensembl parse error: {type(exc).__name__}: {exc}") from exc

    def _fetch_symbol_lookup(self, symbol: str, species: str) -> dict[str, Any] | None:
        url = f"{self._base_url}/lookup/symbol/{quote(species)}/{quote(symbol)}?expand=1"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {400, 404}:
                return None
            raise ProviderError(f"Ensembl HTTP error: {exc.code}") from exc
        except URLError as exc:
            return None
        except Exception as exc:
            raise ProviderError(f"Ensembl parse error: {type(exc).__name__}: {exc}") from exc

    def _fetch_xrefs(self, ensembl_gene_id: str) -> dict[str, Any]:
        url = f"{self._base_url}/xrefs/id/{quote(ensembl_gene_id)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, list):
            return {}
        result: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            db = clean_text(entry.get("db_display_name") or entry.get("db_name"))
            primary_id = clean_text(entry.get("primary_id"))
            if db == "EntrezGene" and primary_id:
                result["entrez_gene_id"] = primary_id
            if db == "UniProt" and primary_id:
                result.setdefault("uniprot_ids", []).append(primary_id)
        return result

    def _payload_from_lookup(self, raw: dict[str, Any]) -> dict[str, Any]:
        transcripts = [row for row in raw.get("Transcript", []) if isinstance(row, dict)]
        transcript_rows: list[dict[str, Any]] = []
        protein_ids: list[str] = []
        protein_coding_count = 0
        for row in transcripts:
            transcript_id = clean_text(row.get("id")) or None
            biotype = clean_text(row.get("biotype")) or None
            translation = row.get("Translation") if isinstance(row.get("Translation"), dict) else {}
            protein_id = clean_text(translation.get("id")) or None
            if biotype == "protein_coding":
                protein_coding_count += 1
            if protein_id and protein_id not in protein_ids:
                protein_ids.append(protein_id)
            transcript_rows.append(
                {
                    "transcript_id": transcript_id,
                    "biotype": biotype,
                    "is_canonical": bool(row.get("is_canonical")),
                    "protein_id": protein_id,
                }
            )

        return {
            "ensembl_gene_id": clean_text(raw.get("id")) or None,
            "display_name": clean_text(raw.get("display_name")) or None,
            "description": _clean_description(raw.get("description")),
            "species": clean_text(raw.get("species")) or None,
            "assembly_name": clean_text(raw.get("assembly_name")) or None,
            "seq_region_name": clean_text(raw.get("seq_region_name")) or None,
            "start": _int_or_none(raw.get("start")),
            "end": _int_or_none(raw.get("end")),
            "strand": _int_or_none(raw.get("strand")),
            "biotype": clean_text(raw.get("biotype")) or None,
            "canonical_transcript_id": clean_text(raw.get("canonical_transcript")) or None,
            "transcript_count": len(transcript_rows),
            "protein_coding_transcript_count": protein_coding_count,
            "protein_ids": protein_ids,
            "transcripts": transcript_rows,
        }


def _match_from_ensembl_lookup(raw: dict[str, Any], query: str, xrefs: dict[str, Any]) -> dict[str, Any]:
    cleaned_query = clean_text(query) or ""
    uc_query = cleaned_query.upper()
    display_name = clean_text(raw.get("display_name")) or ""
    ensembl_gene_id = clean_text(raw.get("id")) or ""
    description = _clean_description(raw.get("description"))
    biotype = clean_text(raw.get("biotype")) or None
    entrez_gene_id = clean_text(xrefs.get("entrez_gene_id")) or None
    uniprot_ids = xrefs.get("uniprot_ids", [])

    uc_display = display_name.upper()
    if uc_display == uc_query:
        matched_on = "symbol"
        matched_value = display_name
    elif ensembl_gene_id.upper() == uc_query:
        matched_on = "ensembl_gene_id"
        matched_value = ensembl_gene_id
    elif entrez_gene_id and entrez_gene_id.upper() == uc_query:
        matched_on = "entrez_gene_id"
        matched_value = entrez_gene_id
    elif description and uc_query in description.upper():
        matched_on = "description"
        matched_value = clean_text(raw.get("description"))
    else:
        matched_on = "symbol"
        matched_value = display_name

    return {
        "query": cleaned_query,
        "symbol": display_name or None,
        "name": description or None,
        "summary": None,
        "ensembl_gene_id": ensembl_gene_id or None,
        "entrez_gene_id": entrez_gene_id,
        "aliases": [],
        "uniprot_ids": uniprot_ids,
        "genomic_position": None,
        "biotype": biotype,
        "score": 90.0,
        "matched_on": matched_on,
        "matched_value": matched_value,
    }


def _clean_description(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    marker = text.find("[Source:")
    return text[:marker].strip() if marker != -1 else text


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None
