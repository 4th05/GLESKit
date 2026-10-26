from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import species_to_mygene


_BASE_URL = "https://mygene.info/v3/query"


@dataclass(slots=True)
class GeneMatch:
    query: str
    symbol: str | None
    name: str | None
    summary: str | None
    ensembl_gene_id: str | None
    entrez_gene_id: str | None
    aliases: list[str]
    uniprot_ids: list[str]
    genomic_position: str | None
    biotype: str | None
    score: float | None
    matched_on: str | None
    matched_value: str | None


class MyGeneProvider:
    name = "mygene"

    def __init__(self, *, opener: Any | None = None, timeout_seconds: int = 20) -> None:
        self._opener = opener or urlopen
        self.timeout_seconds = max(1, int(timeout_seconds))

    def resolve_gene(self, query: str, *, species: str = "human", size: int = 5) -> ProviderResponse:
        cleaned_query = " ".join(str(query or "").split()).strip()
        if not cleaned_query:
            raise ProviderError("Query must not be empty.")

        effective_species = species_to_mygene(species)
        params = {
            "q": cleaned_query,
            "species": effective_species,
            "size": max(1, min(int(size), 10)),
            "fields": ",".join(
                [
                    "symbol",
                    "name",
                    "summary",
                    "alias",
                    "ensembl.gene",
                    "entrezgene",
                    "uniprot.Swiss-Prot",
                    "uniprot.TrEMBL",
                    "genomic_pos",
                    "biotype",
                    "_score",
                ]
            ),
        }
        url = f"{_BASE_URL}?{urlencode(params)}"

        try:
            with self._opener(url, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"MyGene HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"MyGene network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"MyGene parse error: {type(exc).__name__}: {exc}") from exc

        hits = payload.get("hits", []) if isinstance(payload, dict) else []
        match_objects = [self._match_from_hit(cleaned_query, hit) for hit in hits if isinstance(hit, dict)]
        match_objects.sort(key=lambda item: _match_sort_key(item, cleaned_query), reverse=True)
        matches = [asdict(item) for item in match_objects]
        best = match_objects[0] if match_objects else None
        summary = (
            f"Resolved {len(matches)} candidate gene match(es) for '{cleaned_query}'."
            if matches
            else f"No gene matches found for '{cleaned_query}'."
        )
        if best and best.matched_on:
            summary = (
                f"Resolved {len(matches)} candidate gene match(es) for '{cleaned_query}'. "
                f"Top match uses {best.matched_on}."
            )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "query": cleaned_query,
                "species": species,
                "match_count": len(matches),
                "matches": matches,
            },
        )

    def resolve_genes(self, queries: list[str], *, species: str = "human", chunk_size: int = 1000) -> dict[str, ProviderResponse]:
        """Resolve many identifiers with MyGene's documented POST query endpoint.

        This is intentionally separate from ``resolve_gene``: high-cardinality RNA-seq
        handoffs must never degrade into one network request per gene identifier.
        """
        unique_queries = list(dict.fromkeys(" ".join(str(query or "").split()).strip() for query in queries))
        unique_queries = [query for query in unique_queries if query]
        if not unique_queries:
            return {}

        effective_species = species_to_mygene(species)
        query_by_key = {query.upper(): query for query in unique_queries}
        matches_by_query: dict[str, list[GeneMatch]] = {query: [] for query in unique_queries}
        fields = ",".join(
            [
                "symbol",
                "name",
                "summary",
                "alias",
                "ensembl.gene",
                "entrezgene",
                "uniprot.Swiss-Prot",
                "uniprot.TrEMBL",
                "genomic_pos",
                "biotype",
                "_score",
            ]
        )
        for start in range(0, len(unique_queries), max(1, min(int(chunk_size), 1000))):
            chunk = unique_queries[start : start + max(1, min(int(chunk_size), 1000))]
            request = Request(
                _BASE_URL,
                data=urlencode(
                    {
                        "q": ",".join(chunk),
                        "scopes": "ensembl.gene,entrezgene,symbol,alias",
                        "species": effective_species,
                        "fields": fields,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                method="POST",
            )
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raise ProviderError(f"MyGene batch HTTP error: {exc.code}") from exc
            except URLError as exc:
                raise ProviderError(f"MyGene batch network error: {exc.reason}") from exc
            except Exception as exc:
                raise ProviderError(f"MyGene batch parse error: {type(exc).__name__}: {exc}") from exc
            if not isinstance(payload, list):
                raise ProviderError("MyGene batch query returned an invalid response.")
            for hit in payload:
                if not isinstance(hit, dict) or hit.get("notfound"):
                    continue
                raw_query = " ".join(str(hit.get("query") or "").split()).strip()
                original_query = query_by_key.get(raw_query.upper())
                if not original_query:
                    continue
                matches_by_query[original_query].append(self._match_from_hit(original_query, hit))

        resolved: dict[str, ProviderResponse] = {}
        for query, matches in matches_by_query.items():
            matches.sort(key=lambda item: _match_sort_key(item, query), reverse=True)
            if not matches:
                continue
            resolved[query] = ProviderResponse(
                provider=self.name,
                summary=f"Resolved {len(matches)} candidate gene match(es) for '{query}' using a batch lookup.",
                payload={
                    "query": query,
                    "species": species,
                    "match_count": len(matches),
                    "matches": [asdict(match) for match in matches],
                },
            )
        return resolved

    def _match_from_hit(self, query: str, hit: dict[str, Any]) -> GeneMatch:
        return GeneMatch(
            query=query,
            symbol=_text(hit.get("symbol")),
            name=_text(hit.get("name")),
            summary=_text(hit.get("summary")),
            ensembl_gene_id=_first_ensembl_gene(hit.get("ensembl")),
            entrez_gene_id=_text(hit.get("entrezgene")),
            aliases=_normalize_list(hit.get("alias")),
            uniprot_ids=_normalize_uniprot(hit.get("uniprot")),
            genomic_position=_normalize_genomic_pos(hit.get("genomic_pos")),
            biotype=_text(hit.get("biotype")),
            score=_float_or_none(hit.get("_score")),
            matched_on=_matched_on(query, hit),
            matched_value=_matched_value(query, hit),
        )


def _text(value: Any) -> str | None:
    token = str(value).strip() if value is not None else ""
    return token or None


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    token = str(value).strip()
    return [token] if token else []


def _first_ensembl_gene(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("gene") or value.get("id"))
    if isinstance(value, list):
        for item in value:
            gene_id = _first_ensembl_gene(item)
            if gene_id:
                return gene_id
        return None
    return _text(value)


def _normalize_uniprot(value: Any) -> list[str]:
    if isinstance(value, dict):
        items: list[str] = []
        for key in ("Swiss-Prot", "TrEMBL"):
            items.extend(_normalize_list(value.get(key)))
        return items
    return _normalize_list(value)


def _normalize_genomic_pos(value: Any) -> str | None:
    if isinstance(value, dict):
        chrom = _text(value.get("chr"))
        start = _text(value.get("start"))
        end = _text(value.get("end"))
        if chrom and start and end:
            return f"{chrom}:{start}-{end}"
        return chrom
    return _text(value)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _match_sort_key(match: GeneMatch, query: str) -> tuple[int, float]:
    return (_match_rank(match, query), float(match.score or 0.0))


def _match_rank(match: GeneMatch, query: str) -> int:
    token = str(query or "").strip().upper()
    if not token:
        return 0
    symbol = (match.symbol or "").upper()
    aliases = [(item or "").upper() for item in match.aliases]
    name = (match.name or "").upper()
    ensembl = (match.ensembl_gene_id or "").upper()
    entrez = (match.entrez_gene_id or "").upper()
    uniprots = [(item or "").upper() for item in match.uniprot_ids]

    if symbol == token:
        return 7
    if token and token in aliases:
        return 6
    if ensembl == token or entrez == token or token in uniprots:
        return 5
    if name == token:
        return 4
    if symbol.startswith(token):
        return 3
    if any(alias.startswith(token) for alias in aliases):
        return 2
    if token in name:
        return 1
    return 0


def _matched_on(query: str, hit: dict[str, Any]) -> str | None:
    token = str(query or "").strip().upper()
    if not token:
        return None
    symbol = (_text(hit.get("symbol")) or "").upper()
    aliases = [item.upper() for item in _normalize_list(hit.get("alias"))]
    name = (_text(hit.get("name")) or "").upper()
    ensembl = (_first_ensembl_gene(hit.get("ensembl")) or "").upper()
    entrez = (_text(hit.get("entrezgene")) or "").upper()
    uniprots = [item.upper() for item in _normalize_uniprot(hit.get("uniprot"))]

    if symbol == token:
        return "symbol"
    if token in aliases:
        return "alias"
    if ensembl == token:
        return "ensembl_gene_id"
    if entrez == token:
        return "entrez_gene_id"
    if token in uniprots:
        return "uniprot_id"
    if name == token:
        return "name"
    if symbol.startswith(token):
        return "symbol_prefix"
    if any(alias.startswith(token) for alias in aliases):
        return "alias_prefix"
    if token in name:
        return "name_partial"
    return None


def _matched_value(query: str, hit: dict[str, Any]) -> str | None:
    token = str(query or "").strip().upper()
    if not token:
        return None
    symbol = _text(hit.get("symbol"))
    if symbol and symbol.upper() == token:
        return symbol
    for alias in _normalize_list(hit.get("alias")):
        if alias.upper() == token:
            return alias
    ensembl = _first_ensembl_gene(hit.get("ensembl"))
    if ensembl and ensembl.upper() == token:
        return ensembl
    entrez = _text(hit.get("entrezgene"))
    if entrez and entrez.upper() == token:
        return entrez
    for accession in _normalize_uniprot(hit.get("uniprot")):
        if accession.upper() == token:
            return accession
    name = _text(hit.get("name"))
    if name and token in name.upper():
        return name
    return symbol or name or None
