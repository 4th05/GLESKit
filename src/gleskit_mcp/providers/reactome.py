from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text


class ReactomeProvider:
    name = "reactome"
    _base_url = "https://reactome.org/ContentService/data"

    def pathway(
        self,
        pathway_id: str,
        *,
        participant_limit: int = 10,
        timeout_seconds: int = 5,
        participant_timeout_seconds: int = 3,
    ) -> ProviderResponse:
        cleaned_id = clean_text(pathway_id)
        if not cleaned_id:
            raise ProviderError("Reactome requires a non-empty pathway identifier.")

        query_url = f"{self._base_url}/query/{cleaned_id}"
        request = Request(query_url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=max(1, int(timeout_seconds))) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"Reactome HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"Reactome network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"Reactome parse error: {type(exc).__name__}: {exc}") from exc

        if not isinstance(raw, dict) or not raw.get("stId"):
            raise ProviderError(f"Reactome returned no pathway for {cleaned_id}.")

        participants = self._participants(
            cleaned_id,
            participant_limit=participant_limit,
            timeout_seconds=participant_timeout_seconds,
        )
        payload = {
            "pathway_id": clean_text(raw.get("stId")) or cleaned_id,
            "display_name": clean_text(raw.get("displayName")) or None,
            "species_name": clean_text(raw.get("speciesName")) or None,
            "schema_class": clean_text(raw.get("schemaClass")) or None,
            "is_in_disease": bool(raw.get("isInDisease")),
            "is_inferred": bool(raw.get("isInferred")),
            "summary": _summation_text(raw.get("summation")),
            "compartment": _compartment_text(raw.get("compartment")),
            "participant_count": len(participants),
            "participants": participants,
        }
        summary = (
            f"Retrieved Reactome pathway context for {payload.get('display_name') or payload.get('pathway_id')}."
        )
        return ProviderResponse(provider=self.name, summary=summary, payload=payload)

    def _participants(self, pathway_id: str, *, participant_limit: int = 10, timeout_seconds: int = 3) -> list[dict[str, Any]]:
        url = f"{self._base_url}/pathway/{pathway_id}/participants"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=max(1, int(timeout_seconds))) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        participants: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in raw:
            if not isinstance(row, dict):
                continue
            name = clean_text(row.get("displayName") or row.get("name"))
            identifier = clean_text(row.get("stId") or row.get("dbId") or name)
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            participants.append(
                {
                    "identifier": identifier,
                    "display_name": name or None,
                    "schema_class": clean_text(row.get("schemaClass")) or None,
                }
            )
            if len(participants) >= max(1, participant_limit):
                break
        return participants


def _summation_text(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            token = _summation_text(item)
            if token:
                return token
        return None
    if isinstance(value, dict):
        return clean_text(value.get("text")) or None
    return clean_text(value) or None


def _compartment_text(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            token = _compartment_text(item)
            if token:
                return token
        return None
    if isinstance(value, dict):
        return clean_text(value.get("displayName") or value.get("name")) or None
    return clean_text(value) or None
