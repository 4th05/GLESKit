from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text, species_to_expression_atlas


class ExpressionAtlasProvider:
    name = "expression_atlas"
    _base_url = "https://www.ebi.ac.uk/gxa/"

    def reference_expression(
        self,
        query: str,
        *,
        species: str = "human",
        alternate_identifiers: list[str] | None = None,
        limit: int = 10,
    ) -> ProviderResponse:
        cleaned_query = clean_text(query)
        if not cleaned_query:
            raise ProviderError("Expression Atlas reference expression requires a non-empty query.")

        atlas_species = species_to_expression_atlas(species)
        raw = self._request_json(
            "json/baseline_refexperiment",
            {
                "geneQuery": json.dumps([{"value": cleaned_query}]),
                "species": atlas_species,
            },
        )
        if not isinstance(raw, dict):
            raise ProviderError(f"Expression Atlas returned an invalid response for '{cleaned_query}'.")

        assay_groups = [self._assay_group(row) for row in raw.get("columnHeaders", []) if isinstance(row, dict)]
        experiment = self._experiment_payload(raw.get("experiment"))
        identifiers = [cleaned_query] + [clean_text(item) for item in (alternate_identifiers or []) if clean_text(item)]
        gene_profile = self._query_gene_profile(raw.get("profiles"), assay_groups, identifiers, limit=limit)

        payload = {
            "query": cleaned_query,
            "species": atlas_species,
            "reference_experiment": experiment,
            "assay_group_count": len(assay_groups),
            "assay_groups": assay_groups,
            "query_gene_profile": gene_profile,
            "search_result_total": _int_or_none(((raw.get("profiles") or {}).get("searchResultTotal"))),
        }
        summary = (
            f"Retrieved Expression Atlas reference baseline context for {cleaned_query} from {experiment.get('accession') or 'a reference experiment'}."
        )
        if gene_profile and gene_profile.get("tissue_count"):
            summary += f" Query-linked expression values were found in {gene_profile['tissue_count']} assay group(s)."
        else:
            summary += " The response did not surface a query-specific expression row, but reference experiment metadata was recovered."
        return ProviderResponse(provider=self.name, summary=summary, payload=payload)

    def _request_json(self, path: str, params: dict[str, Any]) -> Any:
        url = urljoin(self._base_url, path)
        if params:
            url = f"{url}?{urlencode({key: value for key, value in params.items() if value is not None}, doseq=True)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"Expression Atlas HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"Expression Atlas network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"Expression Atlas parse error: {type(exc).__name__}: {exc}") from exc

    def _assay_group(self, row: dict[str, Any]) -> dict[str, Any]:
        summary = row.get("assayGroupSummary") if isinstance(row.get("assayGroupSummary"), dict) else {}
        return {
            "assay_group_id": clean_text(row.get("assayGroupId")) or None,
            "tissue": clean_text(row.get("factorValue")) or None,
            "ontology_term_id": clean_text(row.get("factorValueOntologyTermId")) or None,
            "replicate_count": _int_or_none(summary.get("replicates")),
        }

    def _experiment_payload(self, value: Any) -> dict[str, Any]:
        row = value if isinstance(value, dict) else {}
        urls = row.get("urls") if isinstance(row.get("urls"), dict) else {}
        return {
            "accession": clean_text(row.get("accession")) or None,
            "type": clean_text(row.get("type")) or None,
            "description": clean_text(row.get("description")) or None,
            "species": clean_text(row.get("species")) or None,
            "main_page_url": _absolute_gxa_url(urls.get("main_page")),
            "download_url": _absolute_gxa_url(urls.get("download")),
            "genome_browser_url": _absolute_gxa_url(urls.get("genome_browsers")),
        }

    def _query_gene_profile(
        self,
        profiles: Any,
        assay_groups: list[dict[str, Any]],
        identifiers: list[str],
        *,
        limit: int,
    ) -> dict[str, Any] | None:
        rows = ((profiles or {}).get("rows") or []) if isinstance(profiles, dict) else []
        match = None
        matched_on = None
        matched_value = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = clean_text(row.get("id"))
            row_name = clean_text(row.get("name"))
            for token in identifiers:
                lowered = token.lower()
                if row_id.lower() == lowered or row_id.lower().split(".")[0] == lowered:
                    match = row
                    matched_on = "id"
                    matched_value = row_id
                    break
                if row_name.lower() == lowered:
                    match = row
                    matched_on = "name"
                    matched_value = row_name
                    break
            if match is not None:
                break
        if match is None:
            return None

        expressions = match.get("expressions", []) if isinstance(match.get("expressions"), list) else []
        tissues: list[dict[str, Any]] = []
        for index, assay_group in enumerate(assay_groups):
            if index >= len(expressions):
                break
            cell = expressions[index]
            if not isinstance(cell, dict) or "value" not in cell:
                continue
            tissues.append(
                {
                    **assay_group,
                    "value": _float_or_none(cell.get("value")),
                }
            )
        tissues = [row for row in tissues if row.get("value") is not None]
        tissues.sort(key=lambda row: float(row.get("value") or 0.0), reverse=True)
        return {
            "matched_on": matched_on,
            "matched_value": matched_value,
            "gene_id": clean_text(match.get("id")) or None,
            "gene_name": clean_text(match.get("name")) or None,
            "expression_unit": clean_text(match.get("expressionUnit")) or None,
            "tissue_count": len(tissues),
            "tissues": tissues[: max(1, min(int(limit), 25))],
            "gene_page_url": _absolute_gxa_url(clean_text(match.get("uri")) or None),
        }


def _absolute_gxa_url(value: Any) -> str | None:
    token = clean_text(value)
    if not token:
        return None
    return urljoin("https://www.ebi.ac.uk/gxa/", token)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None
