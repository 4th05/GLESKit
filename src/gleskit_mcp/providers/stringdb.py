from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text, species_to_ncbi_taxon, unique_preserve_order


class StringDBProvider:
    name = "string"
    _base_url = "https://string-db.org/api/json"
    _caller_identity = "gleskit-mcp"

    def interaction_network(
        self,
        identifiers: list[str],
        *,
        species: str = "human",
        limit: int = 25,
        required_score: int = 700,
    ) -> ProviderResponse:
        cleaned = unique_preserve_order(identifiers)
        if not cleaned:
            raise ProviderError("STRING network requests require at least one identifier.")

        taxon_id = _species_taxon(species)
        params = {
            "identifiers": "\r".join(cleaned),
            "species": taxon_id,
            "required_score": max(0, min(int(required_score), 1000)),
            "network_type": "functional",
            "caller_identity": self._caller_identity,
        }
        edges_raw = self._request("network", params)
        edges = [self._edge_from_row(row) for row in edges_raw if isinstance(row, dict)]
        edges.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
        edges = edges[: max(1, min(int(limit), 100))]

        enrichment = None
        try:
            enrichment_rows = self._request(
                "ppi_enrichment",
                {
                    "identifiers": "\r".join(cleaned),
                    "species": taxon_id,
                    "required_score": max(0, min(int(required_score), 1000)),
                    "caller_identity": self._caller_identity,
                },
            )
            if isinstance(enrichment_rows, list) and enrichment_rows:
                enrichment = self._ppi_enrichment_from_row(enrichment_rows[0])
        except ProviderError:
            enrichment = None

        summary = (
            f"Retrieved {len(edges)} STRING interaction edge(s) for {len(cleaned)} input identifier(s)."
            if edges
            else f"No STRING interaction edges were returned for {len(cleaned)} input identifier(s)."
        )
        if enrichment and enrichment.get("p_value") is not None:
            summary += f" PPI enrichment p-value: {enrichment['p_value']}."
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "identifiers": cleaned,
                "species": species,
                "taxon_id": taxon_id,
                "edge_count": len(edges),
                "edges": edges,
                "ppi_enrichment": enrichment,
            },
        )

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        url = f"{self._base_url}/{method}"
        body = urlencode({key: value for key, value in params.items() if value is not None}).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"STRING HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"STRING network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"STRING parse error: {type(exc).__name__}: {exc}") from exc

    def _edge_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "protein_a": clean_text(row.get("preferredName_A")) or None,
            "protein_b": clean_text(row.get("preferredName_B")) or None,
            "string_id_a": clean_text(row.get("stringId_A")) or None,
            "string_id_b": clean_text(row.get("stringId_B")) or None,
            "score": _float_or_none(row.get("score")),
            "neighborhood_score": _float_or_none(row.get("nscore")),
            "fusion_score": _float_or_none(row.get("fscore")),
            "cooccurrence_score": _float_or_none(row.get("pscore")),
            "coexpression_score": _float_or_none(row.get("ascore")),
            "experimental_score": _float_or_none(row.get("escore")),
            "database_score": _float_or_none(row.get("dscore")),
            "textmining_score": _float_or_none(row.get("tscore")),
        }

    def _ppi_enrichment_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "number_of_nodes": _int_or_none(row.get("number_of_nodes")),
            "number_of_edges": _int_or_none(row.get("number_of_edges")),
            "expected_number_of_edges": _float_or_none(row.get("expected_number_of_edges")),
            "average_node_degree": _float_or_none(row.get("average_node_degree")),
            "local_clustering_coefficient": _float_or_none(row.get("local_clustering_coefficient")),
            "p_value": _float_or_none(row.get("p_value")),
        }


def _species_taxon(species: str) -> int:
    mapped = species_to_ncbi_taxon(species)
    if mapped is not None:
        return mapped
    token = clean_text(species)
    if token.isdigit():
        return int(token)
    raise ProviderError(f"STRING requires a supported species or numeric NCBI taxon. Received '{species}'.")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None
