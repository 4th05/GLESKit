from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import normalize_gene_list


class EnrichrProvider:
    """Small Enrichr API adapter compatible with gseapy.enrichr semantics.

    gseapy submits a gene list to Enrichr and requests a named Enrichr library.
    Keeping that protocol here avoids making the optional gseapy package part of
    the core GLESKit installation while preserving the library-level contract.
    """

    name = "enrichr"
    _base_url = "https://maayanlab.cloud/Enrichr"
    _speedrichr_url = "https://maayanlab.cloud/speedrichr/api"
    _species_aliases = {
        "human": "human",
        "homo sapiens": "human",
        "hsapiens": "human",
        "hs": "human",
        "mouse": "mouse",
        "mus musculus": "mouse",
        "mmusculus": "mouse",
        "mm": "mouse",
        "fly": "fly",
        "drosophila": "fly",
        "drosophila melanogaster": "fly",
        "yeast": "yeast",
        "saccharomyces cerevisiae": "yeast",
        "worm": "worm",
        "caenorhabditis elegans": "worm",
        "fish": "fish",
        "zebrafish": "fish",
        "danio rerio": "fish",
    }

    def __init__(self, *, opener: Callable[..., Any] | None = None, timeout_seconds: int = 30) -> None:
        self._opener = opener or urlopen
        self.timeout_seconds = max(1, int(timeout_seconds))

    def enrich_genes(
        self,
        genes: list[str],
        *,
        library: str,
        species: str = "human",
        limit: int = 15,
        background_genes: list[str] | None = None,
        focus_terms: list[str] | None = None,
    ) -> ProviderResponse:
        canonical_species = self._canonical_species(species)
        cleaned_genes = normalize_gene_list(genes, species=canonical_species)
        if len(cleaned_genes) < 2:
            raise ProviderError("At least two genes are required for Enrichr enrichment.")
        library_name = normalize_enrichr_library(library, allow_named=True)
        if not library_name:
            raise ProviderError(
                "Enrichr requires an explicit named library, such as "
                "GO_Biological_Process_2021, Reactome_2022, or KEGG_2019_Mouse."
            )

        cleaned_background = normalize_gene_list(background_genes or [], species=canonical_species)
        endpoint_base = self._speedrichr_url if cleaned_background else self._base_url
        list_payload = self._post_form(
            "/addList",
            {
                "list": "\n".join(cleaned_genes),
                "description": "GLESKit pathway enrichment",
            },
            base_url=endpoint_base,
            operation="Speedrichr gene-list submission" if cleaned_background else "Enrichr gene-list submission",
        )
        user_list_id = list_payload.get("userListId")
        if user_list_id in (None, ""):
            raise ProviderError("Enrichr did not return a userListId [error_code=provider_error]")

        if cleaned_background:
            background_payload = self._post_urlencoded(
                "/addbackground",
                {
                    "background": "\n".join(cleaned_background),
                },
                base_url=self._speedrichr_url,
                operation="Speedrichr background submission",
            )
            background_id = background_payload.get("backgroundid")
            if background_id in (None, ""):
                raise ProviderError("Speedrichr did not return a backgroundid [error_code=provider_error]")
            result = self._post_urlencoded(
                "/backgroundenrich",
                {
                    "userListId": str(user_list_id),
                    "backgroundid": str(background_id),
                    "backgroundType": library_name,
                },
                base_url=self._speedrichr_url,
                operation="Speedrichr enrichment",
            )
        else:
            query = urlencode({"userListId": str(user_list_id), "backgroundType": library_name})
            result = self._get_json(f"/enrich?{query}")
        raw_rows = result.get(library_name, []) if isinstance(result, dict) else []
        terms = [
            self._term_from_row(
                row,
                library=library_name,
                query_size=len(cleaned_genes),
                species=canonical_species,
            )
            for row in raw_rows
            if isinstance(row, list) and len(row) >= 3
        ]
        terms = [row for row in terms if row.get("term_name")]
        requested_focus_terms = _normalize_focus_terms(focus_terms)
        focused_terms, focus_terms_found = _select_focus_terms(terms, requested_focus_terms)
        limited_terms = terms[: max(1, min(int(limit), 50))]
        summary = (
            f"Retrieved {len(limited_terms)} Enrichr term(s) from {library_name} for {len(cleaned_genes)} gene(s)."
            if limited_terms
            else f"Enrichr returned no terms from {library_name} for {len(cleaned_genes)} gene(s)."
        )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "species": canonical_species,
                "genes": cleaned_genes,
                "input_genes": cleaned_genes,
                "query_size": len(cleaned_genes),
                "background_genes": cleaned_background,
                "background_size": len(cleaned_background) or None,
                "library": library_name,
                "database": library_name,
                "term_count": len(terms),
                "terms": limited_terms,
                "focus_terms_requested": requested_focus_terms,
                "focused_terms": focused_terms,
                "focus_terms_found": focus_terms_found,
                "backend": "gseapy_enrichr",
                "method": "gseapy.enrichr-compatible Enrichr API",
            },
        )

    def _post_form(
        self,
        path: str,
        values: dict[str, str],
        *,
        base_url: str | None = None,
        operation: str = "Enrichr submission",
    ) -> dict[str, Any]:
        body, content_type = _multipart_form(values)
        request = Request(
            f"{base_url or self._base_url}{path}",
            data=body,
            headers={
                "Content-Type": content_type,
                "User-Agent": "GLESKit/0.2",
            },
            method="POST",
        )
        return self._request_json(request, operation=operation)

    def _post_urlencoded(
        self,
        path: str,
        values: dict[str, str],
        *,
        base_url: str,
        operation: str,
    ) -> dict[str, Any]:
        request = Request(
            f"{base_url}{path}",
            data=urlencode(values).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "GLESKit/0.2",
            },
            method="POST",
        )
        return self._request_json(request, operation=operation)

    def _get_json(self, path: str) -> dict[str, Any]:
        request = Request(
            f"{self._base_url}{path}",
            headers={"Accept": "application/json", "User-Agent": "GLESKit/0.2"},
            method="GET",
        )
        return self._request_json(request, operation="Enrichr enrichment")

    def _request_json(self, request: Request, *, operation: str) -> dict[str, Any]:
        last_error: ProviderError | None = None
        for attempt in range(2):
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ProviderError(f"{operation} returned a non-object response [error_code=provider_error]")
                if payload.get("error"):
                    raise ProviderError(f"{operation} failed: {payload['error']} [error_code=provider_error]")
                return payload
            except HTTPError as exc:
                transient = exc.code in {408, 425, 429, 500, 502, 503, 504}
                last_error = ProviderError(
                    f"{operation} HTTP error: {exc.code} "
                    f"[error_code={'provider_timeout' if transient else 'provider_http_error'}]"
                )
                if transient and attempt == 0:
                    time.sleep(0.2)
                    continue
                break
            except (URLError, TimeoutError, socket.timeout) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = ProviderError(
                    f"{operation} timed out or failed: {reason} [error_code=provider_timeout]"
                )
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                break
            except ProviderError:
                raise
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
                raise ProviderError(f"{operation} returned invalid JSON: {exc} [error_code=provider_error]") from exc
        raise last_error or ProviderError(f"{operation} returned no response [error_code=provider_error]")

    @classmethod
    def _canonical_species(cls, species: str) -> str:
        normalized = " ".join(str(species or "").strip().lower().split())
        canonical = cls._species_aliases.get(normalized)
        if canonical:
            return canonical
        supported = ", ".join(sorted(set(cls._species_aliases.values())))
        raise ProviderError(
            f"The Enrichr compatibility backend does not support species {species!r}.",
            error_code="invalid_input",
            retryable=False,
            suggested_next_step=f"Use a supported Enrichr species ({supported}) or select a local enrichment backend.",
        )

    @staticmethod
    def _term_from_row(row: list[Any], *, library: str, query_size: int, species: str) -> dict[str, Any]:
        matched_genes = _string_list(row[5] if len(row) > 5 else [])
        return {
            "rank": _float_or_none(row[0]),
            "term_id": None,
            "term_name": str(row[1] or "").strip(),
            "description": str(row[1] or "").strip(),
            "source": library,
            "p_value": _float_or_none(row[2] if len(row) > 2 else None),
            "z_score": _float_or_none(row[3] if len(row) > 3 else None),
            "combined_score": _float_or_none(row[4] if len(row) > 4 else None),
            "adjusted_p_value": _float_or_none(row[6] if len(row) > 6 else None),
            "intersection_size": len(matched_genes),
            "term_size": None,
            "query_size": query_size,
            "background_size": None,
            "matched_genes": matched_genes,
            "gene_ids": matched_genes,
            "backend": "gseapy_enrichr",
            "library": library,
            "species": species,
        }


def normalize_enrichr_library(value: str, *, allow_named: bool = False) -> str:
    token = str(value or "").strip().replace("-", "_").replace(" ", "_")
    compact = token.lower()
    aliases = {
        "go_biological_process_2021": "GO_Biological_Process_2021",
        "go_bp_2021": "GO_Biological_Process_2021",
        "reactome_2022": "Reactome_2022",
        "reactome2022": "Reactome_2022",
    }
    if compact in aliases:
        return aliases[compact]
    if compact in {"", "go_bp", "go_biological_process", "reac", "reactome", "kegg", "wp", "wikipathways"}:
        return ""
    if allow_named and all(character.isalnum() or character == "_" for character in token):
        return token
    return ""


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


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _multipart_form(values: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----gleskit-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in values.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
