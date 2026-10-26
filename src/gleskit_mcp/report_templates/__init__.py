from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ReportTemplateSection:
    heading: str
    guidance: str


@dataclass(slots=True)
class ReportTemplate:
    template_id: str
    label: str
    description: str
    intent: str
    sections: list[ReportTemplateSection]
    custom_guidance_allowed: bool = False
    custom_guidance_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["id"] = payload.pop("template_id")
        return payload


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent


def list_report_templates() -> list[ReportTemplate]:
    templates: list[ReportTemplate] = []
    for path in sorted(_templates_dir().glob("*.json")):
        raw = json.loads(path.read_text())
        template_id = str(raw.get("id") or "").strip()
        if not template_id:
            raise ValueError(f"Template file {path.name} is missing an id.")
        if template_id != path.stem:
            raise ValueError(f"Template file {path.name} must match template id {template_id}.")
        sections = [
            ReportTemplateSection(
                heading=str(section.get("heading") or "").strip(),
                guidance=str(section.get("guidance") or "").strip(),
            )
            for section in raw.get("sections", [])
            if str(section.get("heading") or "").strip()
        ]
        templates.append(
            ReportTemplate(
                template_id=template_id,
                label=str(raw.get("label") or template_id).strip() or template_id,
                description=str(raw.get("description") or "").strip(),
                intent=str(raw.get("intent") or "").strip(),
                sections=sections,
                custom_guidance_allowed=bool(raw.get("custom_guidance_allowed", False)),
                custom_guidance_required=bool(raw.get("custom_guidance_required", False)),
            )
        )
    return templates


def get_report_template(template_id: str) -> ReportTemplate:
    token = str(template_id or "").strip()
    for template in list_report_templates():
        if template.template_id == token:
            return template
    raise FileNotFoundError(f"Unknown report template: {template_id}")


def list_report_templates_payload() -> dict[str, Any]:
    templates = [template.to_dict() for template in list_report_templates()]
    return {
        "template_count": len(templates),
        "templates": templates,
    }


def get_report_template_payload(template_id: str) -> dict[str, Any]:
    return get_report_template(template_id).to_dict()


__all__ = [
    "ReportTemplate",
    "ReportTemplateSection",
    "get_report_template",
    "get_report_template_payload",
    "list_report_templates",
    "list_report_templates_payload",
]
