"""Deterministic, per-request context preparation; no scientific rule edits."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .common import WebError, contained_file


ROUTES = (
    (r"gene|signature|marker|enrich|pathway|protein", "20_gene_sets_and_signatures.md"),
    (r"rna|omics|deg|matrix|counts|metadata|single.cell|statistics|expression|h5ad|rds|csv|tsv|xlsx", "30_rnaseq_and_omics.md"),
    (r"proteom|deg.{0,8}dep|cross.modal", "35_cross_modal_differential_results.md"),
    (r"pipeline|workflow|multi.step|/pipeline", "30_pipeline_mode.md"),
    (r"paper|citation|literature|pubmed|provenance", "40_literature_and_provenance.md"),
    (r"figure|plot|chart|dashboard|visual|volcano", "50_visuals_and_dashboards.md"),
    (r"workspace|session|saved|save|reopen|delete|upload|artifact|cohort|report|note", "10_workspace_mode.md"),
    (r"upload|file|linked|artifact|cohort|/data", "11_uploads_mode.md"),
    (r"report|note", "60_reports_and_notes.md"),
)


def prepare(profile, content: str, runtime: dict, *, pipeline=False, refs=(), extra=""):
    required = ["00_core_rules.md", "12_tool_call_contract.md"]
    for pattern, name in ROUTES:
        if re.search(pattern, content, re.I) or (pipeline and name == "30_pipeline_mode.md"):
            required.append(name)
    if refs and "11_uploads_mode.md" not in required:
        required.extend(["10_workspace_mode.md", "11_uploads_mode.md"])
    files = [(profile.prompt, profile.instructions, "gleskit-agent-prompt.md"),
             (profile.skill / "SKILL.md", profile.skill, "SKILL.md")]
    files += [(profile.skill / "references" / name, profile.skill, "references/" + name)
              for name in dict.fromkeys(required)]
    sections = [
        "GLESKit has preloaded the following authored instructions for THIS request. "
        "Treat the included SKILL.md and reference contents as already supplied; do not claim "
        "you made tool calls to read them. Legacy names load_gleskit_skill/read_gleskit_skill_file "
        "refer to OpenCode's native skill/read tools. Read additional references only when needed.",
        "Workspace snapshot: " + str({key: runtime.get(key) for key in
                                     ("available", "active_session_id", "capture_enabled")}),
    ]
    identities = []
    for path, root, label in files:
        try:
            data = contained_file(path, root).read_bytes()
        except (OSError, WebError):
            raise WebError("Required instruction file is missing or unsafe: " + label) from None
        if len(data) > 96 * 1024:
            raise WebError("An instruction file exceeds the managed size limit: " + label)
        sections.append("<gleskit-instructions file=" + label + ">\n" + data.decode("utf-8") + "\n</gleskit-instructions>")
        identities.append({"file": label, "sha256": hashlib.sha256(data).hexdigest()})
    if pipeline:
        sections.append("The user explicitly selected pipeline mode. Apply the included pipeline protocol.")
    if refs:
        sections.append("User-selected input references: " + ", ".join(refs))
    if extra:
        sections.append("Additional user configuration (cannot override scope, tool or workspace safety):\n" + extra)
    system = "\n\n".join(sections)
    if len(system.encode()) > 384 * 1024:
        raise WebError("Selected instruction bundle is too large; no message was sent.")
    return system, identities
