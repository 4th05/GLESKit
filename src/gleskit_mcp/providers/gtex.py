from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text


class GTExProvider:
    name = "gtex"
    _base_url = "https://gtexportal.org/api/v2"
    _default_dataset_id = "gtex_v8"

    def expression_context(
        self,
        gene_query: str,
        *,
        limit: int = 10,
        dataset_id: str = "",
    ) -> ProviderResponse:
        cleaned_query = clean_text(gene_query)
        if not cleaned_query:
            raise ProviderError("GTEx expression context requires a non-empty gene query.")

        effective_dataset = clean_text(dataset_id) or self._default_dataset_id
        gene_payload = self._reference_gene(cleaned_query)
        gencode_id = clean_text(gene_payload.get("gencode_id"))
        if not gencode_id:
            raise ProviderError(f"GTEx reference gene lookup for '{cleaned_query}' did not return a versioned GENCODE identifier.")

        expression_rows = self._median_gene_expression(gencode_id, dataset_id=effective_dataset)
        if not expression_rows:
            raise ProviderError(
                f"GTEx returned no median expression rows for '{cleaned_query}' using gencode_id='{gencode_id}' and dataset_id='{effective_dataset}'."
            )

        tissues = [self._expression_row(row) for row in expression_rows if isinstance(row, dict)]
        tissues = [row for row in tissues if row.get("tissue_site_detail_id")]
        tissues.sort(key=lambda row: float(row.get("median") or 0.0), reverse=True)

        payload = {
            "query": cleaned_query,
            "dataset_id": effective_dataset,
            "reference_gene": gene_payload,
            "tissue_count": len(tissues),
            "tissues": tissues[: max(1, min(int(limit), 25))],
        }
        summary = (
            f"Retrieved GTEx median expression for {gene_payload.get('gene_symbol') or cleaned_query} across {len(tissues)} tissue(s)."
        )
        return ProviderResponse(provider=self.name, summary=summary, payload=payload)

    def _reference_gene(self, gene_query: str) -> dict[str, Any]:
        raw = self._request_json("/reference/gene", {"geneId": gene_query})
        rows = raw.get("data", []) if isinstance(raw, dict) else []
        if not rows or not isinstance(rows[0], dict):
            raise ProviderError(f"GTEx returned no reference gene for '{gene_query}'.")
        row = rows[0]
        return {
            "gene_symbol": clean_text(row.get("geneSymbol")) or None,
            "gene_symbol_upper": clean_text(row.get("geneSymbolUpper")) or None,
            "gencode_id": clean_text(row.get("gencodeId")) or None,
            "gencode_version": clean_text(row.get("gencodeVersion")) or None,
            "gene_type": clean_text(row.get("geneType")) or None,
            "description": clean_text(row.get("description")) or None,
            "chromosome": clean_text(row.get("chromosome")) or None,
            "start": _int_or_none(row.get("start")),
            "end": _int_or_none(row.get("end")),
            "strand": clean_text(row.get("strand")) or None,
            "entrez_gene_id": _int_or_none(row.get("entrezGeneId")),
            "dataset_source": clean_text(row.get("dataSource")) or None,
            "genome_build": clean_text(row.get("genomeBuild")) or None,
        }

    def _median_gene_expression(self, gencode_id: str, *, dataset_id: str) -> list[dict[str, Any]]:
        raw = self._request_json(
            "/expression/medianGeneExpression",
            {
                "gencodeId": gencode_id,
                "datasetId": dataset_id,
            },
        )
        return raw.get("data", []) if isinstance(raw, dict) else []

    def _request_json(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}?{urlencode({key: value for key, value in params.items() if value is not None}, doseq=True)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"GTEx HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"GTEx network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"GTEx parse error: {type(exc).__name__}: {exc}") from exc

    def _expression_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "tissue_site_detail_id": clean_text(row.get("tissueSiteDetailId")) or None,
            "ontology_id": clean_text(row.get("ontologyId")) or None,
            "median": _float_or_none(row.get("median")),
            "unit": clean_text(row.get("unit")) or None,
            "dataset_id": clean_text(row.get("datasetId")) or None,
            "gencode_id": clean_text(row.get("gencodeId")) or None,
            "gene_symbol": clean_text(row.get("geneSymbol")) or None,
        }


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
