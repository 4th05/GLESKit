from __future__ import annotations

from typing import Any

from gleskit_mcp.utils import clean_text, unique_preserve_order


def build_evidence_bundle_payload(
    *,
    question: str,
    genes: list[str],
    enrichment_terms: list[dict[str, Any]],
    disease_associations: list[dict[str, Any]],
    papers: list[dict[str, Any]],
) -> dict[str, Any]:
    top_terms = [str(row.get("term_name") or "") for row in enrichment_terms[:3] if str(row.get("term_name") or "").strip()]
    top_disease_rows = [row for row in disease_associations[:5] if isinstance(row, dict)]
    top_papers = [row for row in papers[:5] if isinstance(row, dict)]

    lead_genes = genes[:3]
    narrative: list[str] = []
    gaps: list[str] = []
    citations: list[str] = []

    if top_terms:
        narrative.append(
            f"The strongest functional themes for the current gene set are {', '.join(top_terms[:3])}."
        )
    else:
        gaps.append("No enrichment terms were available to anchor pathway interpretation.")

    if top_disease_rows:
        disease_summaries = unique_preserve_order(
            f"{row.get('gene_symbol', 'gene')} -> {row.get('disease_name', 'disease')}"
            for row in top_disease_rows
        )[:3]
        narrative.append(
            f"Structured disease evidence is strongest for {', '.join(disease_summaries)}."
        )
    else:
        gaps.append("No structured target-disease associations were retrieved.")

    if top_papers:
        paper_titles = [str(row.get("title") or "").strip() for row in top_papers if str(row.get("title") or "").strip()]
        if paper_titles:
            narrative.append(
                f"Literature support includes papers such as {', '.join(paper_titles[:2])}."
            )
        for row in top_papers[:3]:
            ref = _paper_reference(row)
            if ref:
                citations.append(ref)
    else:
        gaps.append("No Europe PMC papers were retrieved for the current focus query.")

    if not narrative:
        narrative.append("The current evidence bundle is still sparse and needs more retrieval to support a report-ready claim.")

    question_text = clean_text(question)
    summary = " ".join(narrative)

    return {
        "question": question_text,
        "focus_genes": genes,
        "lead_genes": lead_genes,
        "top_terms": top_terms,
        "top_disease_associations": top_disease_rows,
        "top_papers": top_papers,
        "summary": summary,
        "narrative": narrative,
        "citations": citations,
        "gaps": unique_preserve_order(gaps),
        "supporting_evidence_count": len(top_disease_rows) + len(top_papers),
    }


def _paper_reference(row: dict[str, Any]) -> str | None:
    doi = str(row.get("doi") or "").strip()
    if doi:
        return f"DOI:{doi}"
    source = str(row.get("source") or "").strip()
    identifier = str(row.get("id") or row.get("identifier") or "").strip()
    if source and identifier:
        return f"{source}:{identifier}"
    return None
