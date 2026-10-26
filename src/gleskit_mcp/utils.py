from __future__ import annotations

import re
from typing import Iterable


_VERSIONED_ENSEMBL_RE = re.compile(r"^(ENS[A-Z0-9_]+)\.[0-9]+$", re.IGNORECASE)


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def normalize_gene_list(values: Iterable[object], *, species: str | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = clean_text(value).strip(",;")
        if not token:
            continue
        parts = [part.strip() for part in token.split(",") if part.strip()] if "," in token else [token]
        for part in parts:
            gene = normalize_gene_token(part, species=species)
            if not gene or gene in seen:
                continue
            seen.add(gene)
            out.append(gene)
    return out


def normalize_gene_token(value: object, *, species: str | None = None) -> str:
    token = clean_text(value).strip(",;")
    if not token:
        return ""
    # Ensembl exports commonly append a version (for example ENSG000001.14).
    # Providers and local annotation tables generally use the stable base ID.
    version_match = _VERSIONED_ENSEMBL_RE.match(token)
    if version_match:
        token = version_match.group(1)
    if _species_is_human(species):
        return token.upper()
    return token


def normalize_term_list(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = clean_text(value)
        if not token:
            continue
        parts = [part.strip() for part in token.split(",") if part.strip()] if "," in token else [token]
        for part in parts:
            lowered = part.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            out.append(part)
    return out


def unique_preserve_order(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = clean_text(value)
        if not token:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(token)
    return out


def species_to_gprofiler(species: str) -> str:
    token = clean_text(species).lower().replace(" ", "_")
    mapping = {
        "human": "hsapiens",
        "homo_sapiens": "hsapiens",
        "mouse": "mmusculus",
        "mus_musculus": "mmusculus",
        "rat": "rnorvegicus",
        "rattus_norvegicus": "rnorvegicus",
    }
    return mapping.get(token, token or "hsapiens")


def species_to_ncbi_taxon(species: str) -> int | None:
    token = clean_text(species).lower().replace(" ", "_")
    mapping = {
        "human": 9606,
        "homo_sapiens": 9606,
        "mouse": 10090,
        "mus_musculus": 10090,
        "rat": 10116,
        "rattus_norvegicus": 10116,
        "pseudomonas_aeruginosa": 208963,
        "pseudomonas_aeruginosa_ucbpp_pa14": 208963,
    }
    return mapping.get(token)


def species_to_uniprot_taxon(species: str) -> int | None:
    return species_to_ncbi_taxon(species)


def species_to_kegg_organism(species: str) -> str:
    token = clean_text(species).lower().replace(" ", "_")
    mapping = {
        "human": "hsa",
        "homo_sapiens": "hsa",
        "mouse": "mmu",
        "mus_musculus": "mmu",
        "rat": "rno",
        "rattus_norvegicus": "rno",
        "pseudomonas_aeruginosa": "pau",
        "pseudomonas_aeruginosa_ucbpp_pa14": "pau",
        "pseudomonas_aeruginosa_pa14": "pau",
    }
    return mapping.get(token, token[:3] if token else "")


def species_to_mygene(species: str) -> str:
    token = clean_text(species).lower().replace(" ", "_").replace("-", "_")
    mapping = {
        "human": "human",
        "homo_sapiens": "human",
        "9606": "human",
        "mouse": "mouse",
        "mus_musculus": "mouse",
        "10090": "mouse",
        "pseudomonas_aeruginosa": "Pseudomonas aeruginosa",
        "pseudomonas_aeruginosa_ucbpp_pa14": "Pseudomonas aeruginosa",
        "pseudomonas_aeruginosa_pa14": "Pseudomonas aeruginosa",
    }
    return mapping.get(token, token)


def species_to_ensembl(species: str) -> str:
    token = clean_text(species).lower().replace(" ", "_").replace("-", "_")
    mapping = {
        "human": "homo_sapiens",
        "homo_sapiens": "homo_sapiens",
        "9606": "homo_sapiens",
        "mouse": "mus_musculus",
        "mus_musculus": "mus_musculus",
        "10090": "mus_musculus",
        "pseudomonas_aeruginosa": "pseudomonas_aeruginosa",
        "pseudomonas_aeruginosa_ucbpp_pa14": "pseudomonas_aeruginosa",
        "pseudomonas_aeruginosa_pa14": "pseudomonas_aeruginosa",
    }
    return mapping.get(token, token)


def species_to_expression_atlas(species: str) -> str:
    token = clean_text(species).lower().replace(" ", "_")
    mapping = {
        "human": "Homo sapiens",
        "homo_sapiens": "Homo sapiens",
        "mouse": "Mus musculus",
        "mus_musculus": "Mus musculus",
        "rat": "Rattus norvegicus",
        "rattus_norvegicus": "Rattus norvegicus",
        "pseudomonas_aeruginosa": "Pseudomonas aeruginosa",
        "pseudomonas_aeruginosa_ucbpp_pa14": "Pseudomonas aeruginosa",
    }
    if token in mapping:
        return mapping[token]
    if not token:
        return "Homo sapiens"
    return " ".join(part.capitalize() for part in token.split("_"))


def _species_is_human(species: str | None) -> bool:
    token = clean_text(species).lower().replace(" ", "_")
    return token in {"human", "homo_sapiens", "9606"}


def build_literature_query(*, genes: list[str], terms: list[str], question: str = "") -> str:
    focus_genes = normalize_gene_list(genes)[:3]
    focus_terms = normalize_term_list(terms)[:2]
    if focus_genes and focus_terms:
        return clean_text(f"{' AND '.join(focus_genes)} AND ({' OR '.join(focus_terms)})")
    if question and focus_genes:
        return clean_text(f"{' AND '.join(focus_genes)} AND {question}")
    if question:
        return clean_text(question)
    fragments = focus_genes + focus_terms
    return clean_text(" AND ".join(fragments))
