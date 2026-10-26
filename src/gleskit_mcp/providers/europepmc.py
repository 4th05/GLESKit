from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text


class EuropePMCProvider:
    name = "europe_pmc"
    _base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def search_papers(
        self,
        query: str,
        *,
        limit: int = 5,
        open_access_only: bool = False,
    ) -> ProviderResponse:
        cleaned_query = clean_text(query)
        if not cleaned_query:
            raise ProviderError("Europe PMC query must not be empty.")

        effective_query = cleaned_query
        if open_access_only:
            effective_query = f"({cleaned_query}) AND OPEN_ACCESS:y"

        url = f"{self._base_url}?{urlencode({'query': effective_query, 'format': 'json', 'pageSize': max(1, min(int(limit), 25)), 'resultType': 'core'})}"
        try:
            with urlopen(url, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"Europe PMC HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"Europe PMC network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"Europe PMC parse error: {type(exc).__name__}: {exc}") from exc

        results = payload.get("resultList", {}).get("result", []) if isinstance(payload, dict) else []
        papers = [self._paper_from_result(row) for row in results if isinstance(row, dict)]
        summary = (
            f"Retrieved {len(papers)} Europe PMC paper(s) for '{cleaned_query}'."
            if papers
            else f"No Europe PMC papers were found for '{cleaned_query}'."
        )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "query": cleaned_query,
                "effective_query": effective_query,
                "paper_count": len(papers),
                "papers": papers,
            },
        )

    def _paper_from_result(self, row: dict[str, Any]) -> dict[str, Any]:
        source = clean_text(row.get("source"))
        identifier = clean_text(row.get("id"))
        authors = _authors_from_result(row)
        author_string = clean_text(row.get("authorString")) or None
        return {
            "id": identifier or None,
            "source": source or None,
            "pmid": clean_text(row.get("pmid")) or None,
            "pmcid": clean_text(row.get("pmcid")) or None,
            "doi": clean_text(row.get("doi")) or None,
            "title": clean_text(row.get("title")) or None,
            "authors": authors,
            "author_count": len(authors),
            "author_string": author_string,
            "first_author_surname": _first_author_surname(row, authors),
            "journal": clean_text(row.get("journalTitle")) or None,
            "year": clean_text(row.get("pubYear")) or None,
            "cited_by_count": _int_or_none(row.get("citedByCount")),
            "is_open_access": str(row.get("isOpenAccess") or "").strip().upper() == "Y",
            "snippet": clean_text(row.get("abstractText"))[:1200] or None,
            "url": f"https://europepmc.org/article/{source}/{identifier}" if source and identifier else None,
        }


def _authors_from_result(row: dict[str, Any]) -> list[str]:
    author_list = row.get("authorList") if isinstance(row, dict) else None
    raw_authors = author_list.get("author") if isinstance(author_list, dict) else None
    if isinstance(raw_authors, dict):
        raw_authors = [raw_authors]

    authors: list[str] = []
    if isinstance(raw_authors, list):
        for raw_author in raw_authors:
            if not isinstance(raw_author, dict):
                continue
            full_name = clean_text(raw_author.get("fullName"))
            if not full_name:
                first_name = clean_text(raw_author.get("firstName"))
                last_name = clean_text(raw_author.get("lastName"))
                initials = clean_text(raw_author.get("initials"))
                full_name = " ".join(part for part in [first_name, last_name or initials] if part).strip()
            if full_name:
                authors.append(full_name)
    if authors:
        return authors

    author_string = clean_text(row.get("authorString"))
    if not author_string:
        return []
    return [part.strip() for part in author_string.split(",") if part.strip()]


def _first_author_surname(row: dict[str, Any], authors: list[str]) -> str | None:
    author_list = row.get("authorList") if isinstance(row, dict) else None
    raw_authors = author_list.get("author") if isinstance(author_list, dict) else None
    if isinstance(raw_authors, dict):
        raw_authors = [raw_authors]
    if isinstance(raw_authors, list) and raw_authors:
        first = raw_authors[0]
        if isinstance(first, dict):
            last_name = clean_text(first.get("lastName"))
            if last_name:
                return last_name

    if not authors:
        return None
    first_author = clean_text(authors[0])
    if not first_author:
        return None
    if "," in first_author:
        head = clean_text(first_author.split(",", 1)[0])
        return head or None
    parts = first_author.split()
    if len(parts) == 2:
        trailing = parts[1].replace(".", "")
        if trailing.isalpha() and len(trailing) <= 3:
            return parts[0]
    if len(parts) >= 2:
        return parts[-1]
    return parts[0] if parts else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None
