from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


def _eval_assets_root() -> Path:
    return Path(__file__).resolve().parent / "assets"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class EvalWorkflowStep:
    step_id: str
    title: str
    instructions: str
    allowed_tools: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_skill_files: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvalWorkflowStep":
        return cls(
            step_id=str(payload["step_id"]).strip(),
            title=str(payload["title"]).strip(),
            instructions=str(payload["instructions"]).strip(),
            allowed_tools=tuple(str(item).strip() for item in payload.get("allowed_tools") or [] if str(item).strip()),
            required_tools=tuple(str(item).strip() for item in payload.get("required_tools") or [] if str(item).strip()),
            required_skill_files=tuple(
                str(item).strip() for item in payload.get("required_skill_files") or [] if str(item).strip()
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "instructions": self.instructions,
            "allowed_tools": list(self.allowed_tools),
            "required_tools": list(self.required_tools),
            "required_skill_files": list(self.required_skill_files),
        }


@dataclass(frozen=True)
class EvalSectionGroundingExpectation:
    heading: str
    evidence_expectation: str
    provenance_expectation: str
    interpretation_expectation: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvalSectionGroundingExpectation":
        return cls(
            heading=str(payload["heading"]).strip(),
            evidence_expectation=str(payload.get("evidence_expectation") or "").strip(),
            provenance_expectation=str(payload.get("provenance_expectation") or "").strip(),
            interpretation_expectation=str(payload.get("interpretation_expectation") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "evidence_expectation": self.evidence_expectation,
            "provenance_expectation": self.provenance_expectation,
            "interpretation_expectation": self.interpretation_expectation,
        }


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    template_id: str
    title: str
    description: str
    task: str
    brief: str
    kickoff_context: dict[str, Any]
    expected_nuggets: tuple[str, ...]
    forbidden_overclaims: tuple[str, ...]
    tags: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, template_id: str) -> "EvalCase":
        return cls(
            case_id=str(payload["case_id"]).strip(),
            template_id=str(payload.get("template_id") or template_id).strip(),
            title=str(payload["title"]).strip(),
            description=str(payload["description"]).strip(),
            task=str(payload["task"]).strip(),
            brief=str(payload["brief"]).strip(),
            kickoff_context=dict(payload.get("kickoff_context") or {}),
            expected_nuggets=tuple(
                str(item).strip() for item in payload.get("expected_nuggets") or [] if str(item).strip()
            ),
            forbidden_overclaims=tuple(
                str(item).strip() for item in payload.get("forbidden_overclaims") or [] if str(item).strip()
            ),
            tags=tuple(str(item).strip() for item in payload.get("tags") or [] if str(item).strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "template_id": self.template_id,
            "title": self.title,
            "description": self.description,
            "task": self.task,
            "brief": self.brief,
            "kickoff_context": self.kickoff_context,
            "expected_nuggets": list(self.expected_nuggets),
            "forbidden_overclaims": list(self.forbidden_overclaims),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class ReportTypeSpec:
    report_type: str
    template_id: str
    display_name: str
    overview: str
    grounding_requirements: tuple[str, ...]
    section_grounding_expectations: tuple[EvalSectionGroundingExpectation, ...]
    workflow_steps: tuple[EvalWorkflowStep, ...]
    cases: tuple[EvalCase, ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReportTypeSpec":
        template_id = str(payload["template_id"]).strip()
        return cls(
            report_type=str(payload["report_type"]).strip(),
            template_id=template_id,
            display_name=str(payload["display_name"]).strip(),
            overview=str(payload["overview"]).strip(),
            grounding_requirements=tuple(
                str(item).strip() for item in payload.get("grounding_requirements") or [] if str(item).strip()
            ),
            section_grounding_expectations=tuple(
                EvalSectionGroundingExpectation.from_dict(item)
                for item in payload.get("section_grounding_expectations") or []
            ),
            workflow_steps=tuple(EvalWorkflowStep.from_dict(item) for item in payload.get("workflow_steps") or []),
            cases=tuple(EvalCase.from_dict(item, template_id=template_id) for item in payload.get("cases") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_type": self.report_type,
            "template_id": self.template_id,
            "display_name": self.display_name,
            "overview": self.overview,
            "grounding_requirements": list(self.grounding_requirements),
            "section_grounding_expectations": [item.to_dict() for item in self.section_grounding_expectations],
            "workflow_steps": [item.to_dict() for item in self.workflow_steps],
            "cases": [item.to_dict() for item in self.cases],
        }

    def case_map(self) -> dict[str, EvalCase]:
        return {case.case_id: case for case in self.cases}


@dataclass(frozen=True)
class EvalRunConfig:
    payload: dict[str, Any]

    @property
    def run_name(self) -> str:
        return str(self.payload.get("run_name") or "gleskit-eval-run").strip() or "gleskit-eval-run"

    @property
    def selection(self) -> dict[str, Any]:
        return dict(self.payload.get("selection") or {})

    @property
    def prompt(self) -> dict[str, Any]:
        return dict(self.payload.get("prompt") or {})

    @property
    def skills(self) -> dict[str, Any]:
        return dict(self.payload.get("skills") or {})

    @property
    def execution(self) -> dict[str, Any]:
        return dict(self.payload.get("execution") or {})

    @property
    def judging(self) -> dict[str, Any]:
        return dict(self.payload.get("judging") or {})

    @property
    def storage(self) -> dict[str, Any]:
        return dict(self.payload.get("storage") or {})

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def load_report_type_specs() -> dict[str, ReportTypeSpec]:
    specs: dict[str, ReportTypeSpec] = {}
    root = _eval_assets_root() / "report_types"
    for path in sorted(root.glob("*.json")):
        spec = ReportTypeSpec.from_dict(_read_json(path))
        specs[spec.report_type] = spec
    return specs


def load_run_config(path: Path | None = None) -> EvalRunConfig:
    template_path = _eval_assets_root() / "run_config.template.json"
    payload = _read_json(template_path)
    if path is not None:
        override = _read_json(path)
        payload = _deep_merge(payload, override)
    return EvalRunConfig(payload=payload)


def write_default_run_config(path: Path) -> None:
    template_path = _eval_assets_root() / "run_config.template.json"
    _write_json(path, _read_json(template_path))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def expand_episode_matrix(
    run_config: EvalRunConfig,
    *,
    specs: dict[str, ReportTypeSpec],
    providers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selection = run_config.selection
    requested_report_types = [str(item).strip() for item in selection.get("report_types") or [] if str(item).strip()]
    report_types = requested_report_types or sorted(specs.keys())

    requested_provider_ids = {str(item).strip() for item in selection.get("provider_ids") or [] if str(item).strip()}
    requested_models = {str(item).strip() for item in selection.get("models") or [] if str(item).strip()}
    requested_case_ids = {str(item).strip() for item in selection.get("case_ids") or [] if str(item).strip()}
    default_provider_only = bool(selection.get("default_provider_only", False))
    seeds = [int(item) for item in selection.get("seeds") or [1]]
    max_cases = int(selection.get("max_cases_per_report_type") or 0)

    raw_skills_modes = selection.get("skills_modes") or ["off", "on"]
    skills_modes: list[bool] = []
    for item in raw_skills_modes:
        normalized = str(item).strip().lower()
        if normalized in {"on", "true", "1"}:
            skills_modes.append(True)
        elif normalized in {"off", "false", "0"}:
            skills_modes.append(False)
    if not skills_modes:
        skills_modes = [False, True]

    filtered_providers: list[dict[str, Any]] = []
    for provider in providers:
        if not provider.get("enabled", True):
            continue
        provider_id = str(provider.get("provider_id") or "").strip()
        if requested_provider_ids and provider_id not in requested_provider_ids:
            continue
        filtered_providers.append(provider)
    if default_provider_only and filtered_providers:
        filtered_providers = filtered_providers[:1]

    rows: list[dict[str, Any]] = []
    for report_type in report_types:
        spec = specs[report_type]
        cases = list(spec.cases)
        if requested_case_ids:
            cases = [case for case in cases if case.case_id in requested_case_ids]
        if max_cases > 0:
            cases = cases[:max_cases]
        for case in cases:
            for provider in filtered_providers:
                provider_models = [str(item).strip() for item in provider.get("models") or [] if str(item).strip()]
                if requested_models:
                    model_ids = [item for item in provider_models if item in requested_models]
                    if not model_ids:
                        default_model = str(provider.get("default_model") or "").strip()
                        if default_model and default_model in requested_models:
                            model_ids = [default_model]
                else:
                    default_model = str(provider.get("default_model") or "").strip()
                    model_ids = [default_model] if default_model else (provider_models[:1] if provider_models else [])
                for model_id in model_ids:
                    for skills_mode in skills_modes:
                        for seed in seeds:
                            rows.append(
                                {
                                    "episode_id": f"{report_type}__{case.case_id}__{provider['provider_id']}__{model_id}__skills_{'on' if skills_mode else 'off'}__seed_{seed}",
                                    "report_type": report_type,
                                    "template_id": spec.template_id,
                                    "case_id": case.case_id,
                                    "provider_id": provider["provider_id"],
                                    "provider_kind": provider.get("kind"),
                                    "model_id": model_id,
                                    "skills_mode": skills_mode,
                                    "seed": seed,
                                }
                            )
    return rows
