from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text


class ChEMBLProvider:
    name = "chembl"
    _base_url = "https://www.ebi.ac.uk/chembl/api/data"

    def target_drug_context(
        self,
        *,
        uniprot_accession: str,
        gene_symbol: str = "",
        limit: int = 10,
    ) -> ProviderResponse:
        cleaned_accession = clean_text(uniprot_accession)
        cleaned_symbol = clean_text(gene_symbol).upper()
        if not cleaned_accession:
            raise ProviderError("ChEMBL target context requires a UniProt accession.")

        target_rows = self._collection(
            "target",
            {
                "target_components__accession": cleaned_accession,
                "limit": 20,
            },
        )
        if not target_rows and cleaned_symbol:
            target_rows = self._collection(
                "target",
                {
                    "pref_name__icontains": cleaned_symbol,
                    "limit": 20,
                },
            )

        target = self._select_target(target_rows, accession=cleaned_accession, gene_symbol=cleaned_symbol)
        if target is None:
            raise ProviderError(
                f"ChEMBL returned no matching target for UniProt accession '{cleaned_accession}'."
            )

        target_chembl_id = clean_text(target.get("target_chembl_id"))
        if not target_chembl_id:
            raise ProviderError(f"Matched ChEMBL target for '{cleaned_accession}' did not include a ChEMBL id.")

        mechanism_rows = self._collection(
            "mechanism",
            {
                "target_chembl_id": target_chembl_id,
                "limit": max(5, min(int(limit) * 3, 50)),
            },
        )
        mechanisms = [self._mechanism_from_row(row) for row in mechanism_rows if isinstance(row, dict)]
        mechanisms = [row for row in mechanisms if row.get("molecule_chembl_id")]

        drugs = self._drugs_from_mechanisms(mechanisms, limit=max(1, min(int(limit), 25)))
        summary = (
            f"Retrieved ChEMBL target context for {target.get('pref_name') or cleaned_symbol or cleaned_accession} "
            f"with {len(mechanisms)} mechanism row(s) and {len(drugs)} drug(s)."
        )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "query_uniprot_accession": cleaned_accession,
                "query_gene_symbol": cleaned_symbol or None,
                "target": target,
                "mechanism_count": len(mechanisms),
                "mechanisms": mechanisms[: max(1, min(int(limit) * 2, 30))],
                "drug_count": len(drugs),
                "drugs": drugs,
            },
        )

    def _collection(self, resource: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        query_params = {key: value for key, value in params.items() if value not in (None, "", [])}
        url = f"{self._base_url}/{resource}.json"
        if query_params:
            url = f"{url}?{urlencode(query_params, doseq=True)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"ChEMBL HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"ChEMBL network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"ChEMBL parse error: {type(exc).__name__}: {exc}") from exc

        if not isinstance(raw, dict):
            return []
        for key, value in raw.items():
            if key == "page_meta":
                continue
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return []

    def _select_target(
        self,
        rows: list[dict[str, Any]],
        *,
        accession: str,
        gene_symbol: str,
    ) -> dict[str, Any] | None:
        if not rows:
            return None
        best_row = max(rows, key=lambda row: self._target_rank(row, accession=accession, gene_symbol=gene_symbol))
        return self._target_from_row(best_row)

    def _target_rank(self, row: dict[str, Any], *, accession: str, gene_symbol: str) -> tuple[int, int, int]:
        target_type = clean_text(row.get("target_type")).upper()
        organism = clean_text(row.get("organism"))
        pref_name = clean_text(row.get("pref_name")).upper()
        accessions = [item.upper() for item in _component_accessions(row.get("target_components"))]
        exact_accession = 1 if accession.upper() in accessions else 0
        single_protein = 1 if target_type == "SINGLE PROTEIN" else 0
        human = 1 if organism.lower() == "homo sapiens" else 0
        symbol_match = 1 if gene_symbol and gene_symbol in pref_name else 0
        return (exact_accession, single_protein + human, symbol_match)

    def _target_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "target_chembl_id": clean_text(row.get("target_chembl_id")) or None,
            "pref_name": clean_text(row.get("pref_name")) or None,
            "target_type": clean_text(row.get("target_type")) or None,
            "organism": clean_text(row.get("organism")) or None,
            "accessions": _component_accessions(row.get("target_components")),
        }

    def _mechanism_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "molecule_chembl_id": clean_text(row.get("molecule_chembl_id")) or None,
            "molecule_name": clean_text(row.get("molecule_pref_name")) or None,
            "action_type": clean_text(row.get("action_type")) or None,
            "mechanism_of_action": clean_text(row.get("mechanism_of_action")) or None,
            "max_phase": _int_or_none(row.get("max_phase")),
            "first_approval": _int_or_none(row.get("first_approval")),
        }

    def _drugs_from_mechanisms(self, mechanisms: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        drugs_by_id: dict[str, dict[str, Any]] = {}
        for row in mechanisms:
            chembl_id = clean_text(row.get("molecule_chembl_id"))
            if not chembl_id:
                continue
            item = drugs_by_id.setdefault(
                chembl_id,
                {
                    "molecule_chembl_id": chembl_id,
                    "molecule_name": row.get("molecule_name"),
                    "max_phase": row.get("max_phase"),
                    "first_approval": row.get("first_approval"),
                    "action_types": [],
                    "mechanisms_of_action": [],
                    "indications": [],
                },
            )
            action_type = clean_text(row.get("action_type"))
            if action_type and action_type not in item["action_types"]:
                item["action_types"].append(action_type)
            moa = clean_text(row.get("mechanism_of_action"))
            if moa and moa not in item["mechanisms_of_action"]:
                item["mechanisms_of_action"].append(moa)
            max_phase = row.get("max_phase")
            if isinstance(max_phase, int):
                current_phase = item.get("max_phase")
                if current_phase is None or max_phase > current_phase:
                    item["max_phase"] = max_phase
            first_approval = row.get("first_approval")
            if isinstance(first_approval, int):
                current_approval = item.get("first_approval")
                if current_approval is None or first_approval < current_approval:
                    item["first_approval"] = first_approval

        out: list[dict[str, Any]] = []
        for chembl_id, item in drugs_by_id.items():
            if not clean_text(item.get("molecule_name")):
                molecule_name = self._molecule_name(chembl_id)
                if molecule_name:
                    item["molecule_name"] = molecule_name
            indications_raw = self._collection(
                "drug_indication",
                {
                    "molecule_chembl_id": chembl_id,
                    "limit": 20,
                },
            )
            indications = [self._indication_from_row(row) for row in indications_raw if isinstance(row, dict)]
            indications = [row for row in indications if row.get("mesh_heading") or row.get("efo_term")]
            item["indications"] = indications[:10]
            out.append(item)

        out.sort(key=lambda row: (-(row.get("max_phase") or -1), clean_text(row.get("molecule_name") or row["molecule_chembl_id"])))
        return out[:limit]

    def _indication_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "mesh_heading": clean_text(row.get("mesh_heading")) or None,
            "efo_id": clean_text(row.get("efo_id")) or None,
            "efo_term": clean_text(row.get("efo_term")) or None,
            "max_phase_for_indication": _int_or_none(row.get("max_phase_for_ind")),
        }

    def _molecule_name(self, chembl_id: str) -> str | None:
        url = f"{self._base_url}/molecule/{chembl_id}.json"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        name = clean_text(raw.get("pref_name"))
        if name:
            return name
        synonyms = raw.get("molecule_synonyms")
        if isinstance(synonyms, list):
            for row in synonyms:
                if not isinstance(row, dict):
                    continue
                token = clean_text(row.get("molecule_synonym"))
                if token:
                    return token
        return None


def _component_accessions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            continue
        token = clean_text(row.get("accession"))
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None
