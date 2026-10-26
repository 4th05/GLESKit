from __future__ import annotations

import json
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text


class OpenTargetsProvider:
    name = "open_targets"
    _url = "https://api.platform.opentargets.org/api/v4/graphql"
    _attempts = (
        {"timeout": 20, "page_size_cap": 25},
        {"timeout": 12, "page_size_cap": 10},
        {"timeout": 8, "page_size_cap": 5},
    )
    _query = """
    query targetAssociations($ensemblId: String!, $pageIndex: Int!, $pageSize: Int!) {
      target(ensemblId: $ensemblId) {
        id
        approvedSymbol
        approvedName
        associatedDiseases(page: { index: $pageIndex, size: $pageSize }) {
          count
          rows {
            score
            disease {
              id
              name
              description
            }
            datasourceScores {
              id
              score
            }
          }
        }
      }
    }
    """

    def associated_diseases(self, ensembl_gene_id: str, *, limit: int = 10) -> ProviderResponse:
        cleaned_id = clean_text(ensembl_gene_id)
        if not cleaned_id:
            raise ProviderError("Open Targets requires an Ensembl gene identifier.")

        raw = self._request_with_retries(cleaned_id, limit=limit)

        if raw.get("errors"):
            message = "; ".join(str(item.get("message") or "unknown error") for item in raw["errors"] if isinstance(item, dict))
            raise ProviderError(f"Open Targets GraphQL error: {message}")

        target = (raw.get("data") or {}).get("target") if isinstance(raw, dict) else None
        if not isinstance(target, dict):
            raise ProviderError(f"Open Targets returned no target for {cleaned_id}.")

        associated = target.get("associatedDiseases") or {}
        rows = associated.get("rows") or []
        diseases = [self._disease_from_row(row) for row in rows if isinstance(row, dict)]
        summary = (
            f"Retrieved {len(diseases)} disease association(s) for {target.get('approvedSymbol') or cleaned_id}."
            if diseases
            else f"No disease associations were returned for {cleaned_id}."
        )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "ensembl_gene_id": target.get("id"),
                "approved_symbol": target.get("approvedSymbol"),
                "approved_name": target.get("approvedName"),
                "association_count": associated.get("count") or len(diseases),
                "diseases": diseases,
            },
        )

    def _request_with_retries(self, cleaned_id: str, *, limit: int) -> dict[str, Any]:
        last_error: Exception | None = None
        normalized_limit = max(1, min(int(limit), 25))
        for attempt in self._attempts:
            page_size = min(normalized_limit, int(attempt["page_size_cap"]))
            timeout = int(attempt["timeout"])
            try:
                return self._request(cleaned_id, page_size=page_size, timeout=timeout)
            except ProviderError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
                continue
            except URLError as exc:
                reason = exc.reason
                if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
                    last_error = exc
                    continue
                raise ProviderError(f"Open Targets network error: {exc.reason}") from exc
            except Exception as exc:
                if "timed out" in str(exc).lower():
                    last_error = exc
                    continue
                raise ProviderError(f"Open Targets parse error: {type(exc).__name__}: {exc}") from exc
        if last_error is not None:
            raise ProviderError(
                f"Open Targets timed out while retrieving disease associations for {cleaned_id}."
            ) from last_error
        raise ProviderError(f"Open Targets failed to retrieve disease associations for {cleaned_id}.")

    def _request(self, cleaned_id: str, *, page_size: int, timeout: int) -> dict[str, Any]:
        body = {
            "query": self._query,
            "variables": {
                "ensemblId": cleaned_id,
                "pageIndex": 0,
                "pageSize": page_size,
            },
        }
        request = Request(
            self._url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"Open Targets HTTP error: {exc.code}") from exc

    def _disease_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        disease = row.get("disease") or {}
        datasource_scores = [
            {
                "datasource_id": str(item.get("id") or "").strip() or None,
                "score": _float_or_none(item.get("score")),
            }
            for item in (row.get("datasourceScores") or [])
            if isinstance(item, dict)
        ]
        datasource_scores = [item for item in datasource_scores if item.get("datasource_id")]
        datasource_scores.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return {
            "disease_id": clean_text(disease.get("id")) or None,
            "disease_name": clean_text(disease.get("name")) or None,
            "description": clean_text(disease.get("description")) or None,
            "score": _float_or_none(row.get("score")),
            "top_datasources": datasource_scores[:5],
        }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
