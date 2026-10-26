from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text, species_to_ncbi_taxon


class QuickGOProvider:
    name = "quickgo"
    _base_url = "https://www.ebi.ac.uk/QuickGO/services/annotation/search"

    def gene_product_annotations(
        self,
        gene_product_id: str,
        *,
        species: str = "human",
        limit: int = 10,
    ) -> ProviderResponse:
        cleaned_id = clean_text(gene_product_id)
        if not cleaned_id:
            raise ProviderError("QuickGO requires a non-empty gene product identifier.")

        params = {
            "geneProductId": cleaned_id,
            "limit": max(1, min(int(limit), 25)),
            "page": 1,
            "includeFields": "goName",
        }
        taxon_id = species_to_ncbi_taxon(species)
        if taxon_id is not None:
            params["taxonId"] = taxon_id

        url = f"{self._base_url}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"QuickGO HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"QuickGO network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"QuickGO parse error: {type(exc).__name__}: {exc}") from exc

        rows = raw.get("results", []) if isinstance(raw, dict) else []
        annotations = [self._annotation_from_row(row) for row in rows if isinstance(row, dict)]
        unique_terms = _unique_terms(annotations)
        summary = (
            f"Retrieved {len(annotations)} QuickGO annotation(s) for {cleaned_id}."
            if annotations
            else f"No QuickGO annotations were returned for {cleaned_id}."
        )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "gene_product_id": cleaned_id,
                "species": species,
                "annotation_count": len(annotations),
                "annotations": annotations,
                "terms": unique_terms,
            },
        )

    def _annotation_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        references = row.get("reference")
        if isinstance(references, list):
            refs = [clean_text(item) for item in references if clean_text(item)]
        else:
            refs = [clean_text(references)] if clean_text(references) else []
        return {
            "gene_product_id": clean_text(row.get("geneProductId")) or None,
            "gene_product_symbol": clean_text(row.get("geneProductSymbol")) or None,
            "go_id": clean_text(row.get("goId")) or None,
            "go_name": clean_text(row.get("goName")) or None,
            "aspect": clean_text(row.get("aspect")) or None,
            "evidence_code": clean_text(row.get("evidenceCode")) or None,
            "qualifier": clean_text(row.get("qualifier")) or None,
            "assigned_by": clean_text(row.get("assignedBy")) or None,
            "references": refs,
        }


def _unique_terms(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in annotations:
        go_id = clean_text(row.get("go_id"))
        if not go_id or go_id in seen:
            continue
        seen.add(go_id)
        out.append(
            {
                "go_id": go_id,
                "go_name": clean_text(row.get("go_name")) or None,
                "aspect": clean_text(row.get("aspect")) or None,
                "evidence_code": clean_text(row.get("evidence_code")) or None,
            }
        )
    return out
