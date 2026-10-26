from __future__ import annotations

import json
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import normalize_gene_list, species_to_gprofiler


class GProfilerProvider:
    name = "gprofiler"
    _url = "https://biit.cs.ut.ee/gprofiler/api/gost/profile/"

    def enrich_genes(
        self,
        genes: list[str],
        *,
        species: str = "human",
        limit: int = 15,
        sources: list[str] | None = None,
        focus_terms: list[str] | None = None,
    ) -> ProviderResponse:
        cleaned_genes = normalize_gene_list(genes, species=species)
        if not cleaned_genes:
            raise ProviderError("At least one gene is required for enrichment.")

        payload = {
            "organism": species_to_gprofiler(species),
            "query": cleaned_genes,
            "sources": sources or ["GO:BP", "GO:MF", "GO:CC", "REAC", "KEGG", "WP"],
            "user_threshold": 0.05,
            "significance_threshold_method": "fdr",
            "no_evidences": False,
        }
        request = Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        raw = None
        last_error: ProviderError | None = None
        for attempt in range(2):
            try:
                with urlopen(request, timeout=30) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                if exc.code in {408, 425, 429, 500, 502, 503, 504} and attempt == 0:
                    time.sleep(0.2)
                    continue
                last_error = ProviderError(f"g:Profiler HTTP error: {exc.code} [error_code=provider_http_error]")
                break
            except (URLError, TimeoutError, socket.timeout) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = ProviderError(
                    f"g:Profiler provider request timed out or failed: {reason} "
                    "[error_code=provider_timeout]"
                )
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                break
            except Exception as exc:
                last_error = ProviderError(f"g:Profiler parse error: {type(exc).__name__}: {exc} [error_code=provider_error]")
                break
        if last_error is not None:
            raise last_error
        if raw is None:
            raise ProviderError("g:Profiler returned no response [error_code=provider_error]")

        rows = raw.get("result", []) if isinstance(raw, dict) else []
        background_size = 0
        terms = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            term = self._term_from_row(
                row,
                species=species,
                query_size=len(cleaned_genes),
            )
            background_size = max(background_size, int(term.get("background_size") or 0))
            terms.append(term)
        requested_focus_terms = _normalize_focus_terms(focus_terms)
        focused_terms, focus_terms_found = _select_focus_terms(terms, requested_focus_terms)
        ranked_terms = terms[: max(1, min(int(limit), 50))]
        summary = (
            f"Retrieved {len(ranked_terms)} enrichment term(s) for {len(cleaned_genes)} gene(s)."
            if ranked_terms
            else f"No enrichment terms were returned for {len(cleaned_genes)} gene(s)."
        )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "species": species,
                "organism": payload["organism"],
                "gene_count": len(cleaned_genes),
                "genes": cleaned_genes,
                "query_size": len(cleaned_genes),
                "background_size": background_size,
                "term_count": len(terms),
                "terms": ranked_terms,
                "focus_terms_requested": requested_focus_terms,
                "focused_terms": focused_terms,
                "focus_terms_found": focus_terms_found,
                "sources": payload["sources"],
                "backend": "gprofiler",
            },
        )

    def _term_from_row(self, row: dict[str, Any], *, species: str = "human", query_size: int = 0) -> dict[str, Any]:
        matched_genes = _string_list(row.get("intersections") or row.get("intersection"))
        term_size = _int_or_zero(row.get("term_size"))
        intersection_size = _int_or_zero(row.get("intersection_size")) or len(matched_genes)
        background_size = _int_or_zero(row.get("effective_domain_size") or row.get("background_size") or row.get("domain_size"))
        if not term_size and background_size and query_size:
            term_size = max(intersection_size, min(background_size, _int_or_zero(row.get("term_size"))))
        odds_ratio = _odds_ratio(query_size, term_size, intersection_size, background_size)
        return {
            "term_id": str(row.get("native") or "").strip() or None,
            "term_name": str(row.get("name") or "").strip() or None,
            "description": str(row.get("description") or "").strip() or None,
            "source": str(row.get("source") or "").strip() or None,
            "p_value": _float_or_none(row.get("p_value")),
            "significant": bool(row.get("significant")),
            "intersection_size": intersection_size,
            "term_size": term_size,
            "query_size": _int_or_zero(row.get("query_size")) or int(query_size),
            "background_size": background_size,
            "precision": _float_or_none(row.get("precision")) if row.get("precision") is not None else _safe_ratio(intersection_size, query_size),
            "recall": _float_or_none(row.get("recall")) if row.get("recall") is not None else _safe_ratio(intersection_size, term_size),
            "odds_ratio": odds_ratio,
            "matched_genes": matched_genes,
            "gene_ids": matched_genes,
            "backend": "gprofiler",
            "species": species,
        }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        token = value.strip()
        return [token] if token else []
    if isinstance(value, (list, tuple, set)):
        cleaned: list[str] = []
        for item in value:
            token = str(item or "").strip()
            if token:
                cleaned.append(token)
        return cleaned
    token = str(value).strip()
    return [token] if token else []


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return float(numerator / denominator)


def _normalize_focus_terms(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip()))[:10]


def _focus_key(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _select_focus_terms(
    terms: list[dict[str, Any]],
    requested: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not requested:
        return [], []
    selected: list[dict[str, Any]] = []
    found: list[str] = []
    for query in requested:
        query_key = _focus_key(query)
        matches = [
            term
            for term in terms
            if query_key
            and query_key
            in {
                _focus_key(term.get("term_id")),
                _focus_key(term.get("term_name")),
                _focus_key(term.get("description")),
            }
        ]
        if matches:
            found.append(query)
            selected.extend(matches)
    return selected[:20], found


def _odds_ratio(query_size: int, term_size: int, intersection_size: int, background_size: int) -> float | None:
    if query_size <= 0 or term_size <= 0 or background_size <= 0:
        return None
    a = float(intersection_size)
    b = float(max(0, query_size - intersection_size))
    c = float(max(0, term_size - intersection_size))
    d = float(max(0, background_size - a - b - c))
    if b <= 0 or c <= 0:
        if a > 0 and d > 0:
            return float("inf")
        return None
    return float((a * d) / (b * c))
