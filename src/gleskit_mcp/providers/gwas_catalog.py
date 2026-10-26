from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gleskit_mcp.providers.base import ProviderError, ProviderResponse
from gleskit_mcp.utils import clean_text


class GWASCatalogProvider:
    name = "gwas_catalog"
    _base_url = "https://www.ebi.ac.uk/gwas/rest/api/v2"

    def variant_trait_associations(self, gene_symbol: str, *, limit: int = 10) -> ProviderResponse:
        cleaned_symbol = clean_text(gene_symbol).upper()
        if not cleaned_symbol:
            raise ProviderError("GWAS Catalog requires a non-empty gene symbol.")

        params = {
            "mapped_gene": cleaned_symbol,
            "size": max(1, min(int(limit), 25)),
        }
        url = f"{self._base_url}/associations?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError(f"GWAS Catalog HTTP error: {exc.code}") from exc
        except URLError as exc:
            raise ProviderError(f"GWAS Catalog network error: {exc.reason}") from exc
        except Exception as exc:
            raise ProviderError(f"GWAS Catalog parse error: {type(exc).__name__}: {exc}") from exc

        rows = _association_rows(raw)
        associations = [self._association_from_row(row) for row in rows if isinstance(row, dict)]
        associations = [row for row in associations if row.get("trait") or row.get("variant_id")]
        associations.sort(key=lambda row: _association_sort_key(row))
        associations = associations[: max(1, min(int(limit), 25))]

        association_count = _association_total(raw, fallback=len(associations))
        summary = (
            f"Retrieved {len(associations)} GWAS Catalog association(s) for {cleaned_symbol}."
            if associations
            else f"No GWAS Catalog associations were returned for {cleaned_symbol}."
        )
        return ProviderResponse(
            provider=self.name,
            summary=summary,
            payload={
                "gene_symbol": cleaned_symbol,
                "association_count": association_count,
                "associations": associations,
            },
        )

    def _association_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        study = row.get("study") or {}
        publication = study.get("publicationInfo") or row.get("publicationInfo") or {}
        strongest = _risk_alleles(row)
        trait = (
            clean_text(row.get("diseaseTrait"))
            or clean_text(_first_nested(study, ("diseaseTrait", "trait")))
            or clean_text(_first_trait(row.get("efoTraits")))
            or clean_text(_first_trait(row.get("efo_traits")))
            or clean_text(_first_list_value(row.get("reported_trait")))
            or clean_text(_first_nested(study, ("efoTraits", 0, "trait")))
            or None
        )
        return {
            "association_id": clean_text(row.get("associationId") or row.get("association_id")) or None,
            "variant_id": (
                clean_text(row.get("variantId"))
                or clean_text(row.get("rsId"))
                or clean_text(row.get("snp"))
                or clean_text(_first_nested(row, ("snp_allele", 0, "rs_id")))
                or (strongest[0] if strongest else None)
            ),
            "risk_alleles": strongest,
            "trait": trait,
            "efo_ids": _efo_ids(row.get("efoTraits") or row.get("efo_traits")),
            "p_value": _float_or_none(row.get("pvalue") or row.get("pValue") or row.get("p_value")),
            "pvalue_description": clean_text(row.get("pvalueDescription") or row.get("pvalue_description")) or None,
            "odds_ratio": _float_or_none(row.get("orPerCopyNum") or row.get("oddsRatio")),
            "beta": _float_or_none(row.get("betaNum") or row.get("beta")),
            "beta_unit": clean_text(row.get("betaUnit")) or None,
            "risk_frequency": clean_text(row.get("riskFrequency") or row.get("risk_frequency")) or None,
            "confidence_interval": clean_text(row.get("ci") or row.get("range")) or None,
            "study_accession": (
                clean_text(study.get("accessionId"))
                or clean_text(row.get("accession_id"))
                or clean_text(row.get("studyAccession"))
                or clean_text(row.get("study_accession"))
                or None
            ),
            "pubmed_id": (
                clean_text(publication.get("pubmedId"))
                or clean_text(row.get("pubmed_id"))
                or clean_text(row.get("pubmedId"))
                or None
            ),
            "mapped_genes": _gene_list(row.get("mappedGenes") or row.get("mapped_genes")),
            "reported_genes": _gene_list(row.get("reportedGenes") or row.get("reported_trait")),
        }


def _association_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        embedded = raw.get("_embedded")
        if isinstance(embedded, dict):
            rows = embedded.get("associations")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        rows = raw.get("associations")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _association_total(raw: Any, *, fallback: int) -> int:
    if isinstance(raw, dict):
        page = raw.get("page")
        if isinstance(page, dict):
            total = page.get("totalElements") or page.get("total_elements")
            if isinstance(total, int):
                return total
    return fallback


def _first_nested(value: Any, path: tuple[Any, ...]) -> Any:
    current = value
    for token in path:
        if isinstance(token, int):
            if not isinstance(current, list) or token >= len(current):
                return None
            current = current[token]
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(token)
    return current


def _first_trait(value: Any) -> str | None:
    if isinstance(value, list):
        for row in value:
            if not isinstance(row, dict):
                continue
            token = clean_text(row.get("trait") or row.get("label") or row.get("efo_trait"))
            if token:
                return token
    return None


def _first_list_value(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            token = clean_text(item)
            if token:
                return token
    return clean_text(value) or None


def _efo_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            continue
        token = clean_text(row.get("shortForm") or row.get("efo_id") or row.get("id"))
        if not token or token in seen:
            continue
        seen.add(token)
        ids.append(token)
    return ids


def _risk_alleles(row: dict[str, Any]) -> list[str]:
    values = row.get("strongestRiskAlleles") or row.get("snp_effect_allele") or row.get("snp_allele")
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                token = clean_text(item.get("riskAlleleName") or item.get("riskAllele"))
                if not token:
                    rs_id = clean_text(item.get("rs_id"))
                    effect = clean_text(item.get("effect_allele"))
                    token = f"{rs_id}-{effect}" if rs_id and effect else rs_id
            else:
                token = clean_text(item)
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(token)
    return out


def _gene_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    if isinstance(value, str):
        return [clean_text(item) for item in value.replace(";", ",").split(",") if clean_text(item)]
    return []


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _association_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    p_value = row.get("p_value")
    normalized = float(p_value) if isinstance(p_value, (int, float)) else 1.0
    return (normalized, clean_text(row.get("trait") or "zzz"))
