from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import subprocess
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class CohortDatasetSpec:
    dataset_id: str
    title: str
    tissues: list[str] = field(default_factory=list)
    access: str = "free"
    mode: str = "remote"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "dataset_id": self.dataset_id,
            "title": self.title,
            "tissues": list(self.tissues),
            "access": self.access,
            "mode": self.mode,
        }
        payload.update(dict(self.metadata))
        return payload


@dataclass(slots=True, frozen=True)
class CohortProviderSpec:
    provider_id: str
    name: str
    description: str
    supports_tissue: bool
    supports_remote: bool
    connectors: list[str] = field(default_factory=list)
    datasets: list[CohortDatasetSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        payload = {
            "provider_id": self.provider_id,
            "name": self.name,
            "description": self.description,
            "supports_tissue": self.supports_tissue,
            "supports_remote": self.supports_remote,
            "connectors": list(self.connectors),
            "dataset_count": len(self.datasets),
        }
        payload.update(dict(self.metadata))
        return payload


_TCGA_PROJECTS: list[tuple[str, str, str, int]] = [
    ("ACC", "Adrenocortical Carcinoma", "Adrenal Gland", 92),
    ("BLCA", "Bladder Urothelial Carcinoma", "Bladder", 412),
    ("BRCA", "Breast Invasive Carcinoma", "Breast", 1098),
    ("CESC", "Cervical Squamous Cell Carcinoma and Endocervical Adenocarcinoma", "Cervix", 307),
    ("CHOL", "Cholangiocarcinoma", "Bile Duct", 51),
    ("COAD", "Colon Adenocarcinoma", "Colon", 521),
    ("DLBC", "Lymphoid Neoplasm Diffuse Large B-cell Lymphoma", "Blood", 48),
    ("ESCA", "Esophageal Carcinoma", "Esophagus", 185),
    ("GBM", "Glioblastoma Multiforme", "Brain", 617),
    ("HNSC", "Head and Neck Squamous Cell Carcinoma", "Head and Neck", 528),
    ("KICH", "Kidney Chromophobe", "Kidney", 113),
    ("KIRC", "Kidney Renal Clear Cell Carcinoma", "Kidney", 537),
    ("KIRP", "Kidney Renal Papillary Cell Carcinoma", "Kidney", 291),
    ("LAML", "Acute Myeloid Leukemia", "Bone Marrow", 200),
    ("LGG", "Brain Lower Grade Glioma", "Brain", 516),
    ("LIHC", "Liver Hepatocellular Carcinoma", "Liver", 377),
    ("LUAD", "Lung Adenocarcinoma", "Lung", 585),
    ("LUSC", "Lung Squamous Cell Carcinoma", "Lung", 504),
    ("MESO", "Mesothelioma", "Pleura", 87),
    ("OV", "Ovarian Serous Cystadenocarcinoma", "Ovary", 379),
    ("PAAD", "Pancreatic Adenocarcinoma", "Pancreas", 185),
    ("PCPG", "Pheochromocytoma and Paraganglioma", "Adrenal Gland", 179),
    ("PRAD", "Prostate Adenocarcinoma", "Prostate", 497),
    ("READ", "Rectum Adenocarcinoma", "Rectum", 176),
    ("SARC", "Sarcoma", "Soft Tissue", 261),
    ("SKCM", "Skin Cutaneous Melanoma", "Skin", 470),
    ("STAD", "Stomach Adenocarcinoma", "Stomach", 443),
    ("TGCT", "Testicular Germ Cell Tumors", "Testis", 156),
    ("THCA", "Thyroid Carcinoma", "Thyroid", 507),
    ("THYM", "Thymoma", "Thymus", 124),
    ("UCEC", "Uterine Corpus Endometrial Carcinoma", "Uterus", 560),
    ("UCS", "Uterine Carcinosarcoma", "Uterus", 57),
    ("UVM", "Uveal Melanoma", "Eye", 80),
]

_GTEX_TISSUES_V8: list[str] = [
    "Adipose - Subcutaneous",
    "Adipose - Visceral (Omentum)",
    "Adrenal Gland",
    "Artery - Aorta",
    "Artery - Coronary",
    "Artery - Tibial",
    "Bladder",
    "Brain - Amygdala",
    "Brain - Anterior cingulate cortex (BA24)",
    "Brain - Caudate (basal ganglia)",
    "Brain - Cerebellar Hemisphere",
    "Brain - Cerebellum",
    "Brain - Cortex",
    "Brain - Frontal Cortex (BA9)",
    "Brain - Hippocampus",
    "Brain - Hypothalamus",
    "Brain - Nucleus accumbens (basal ganglia)",
    "Brain - Putamen (basal ganglia)",
    "Brain - Spinal cord (cervical c-1)",
    "Brain - Substantia nigra",
    "Breast - Mammary Tissue",
    "Cells - Cultured fibroblasts",
    "Cells - EBV-transformed lymphocytes",
    "Cervix - Ectocervix",
    "Cervix - Endocervix",
    "Colon - Sigmoid",
    "Colon - Transverse",
    "Esophagus - Gastroesophageal Junction",
    "Esophagus - Mucosa",
    "Esophagus - Muscularis",
    "Fallopian Tube",
    "Heart - Atrial Appendage",
    "Heart - Left Ventricle",
    "Kidney - Cortex",
    "Kidney - Medulla",
    "Liver",
    "Lung",
    "Minor Salivary Gland",
    "Muscle - Skeletal",
    "Nerve - Tibial",
    "Ovary",
    "Pancreas",
    "Pituitary",
    "Prostate",
    "Skin - Not Sun Exposed (Suprapubic)",
    "Skin - Sun Exposed (Lower leg)",
    "Small Intestine - Terminal Ileum",
    "Spleen",
    "Stomach",
    "Testis",
    "Thyroid",
    "Uterus",
    "Vagina",
    "Whole Blood",
]


def _gtex_dataset_id_from_tissue(tissue: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", str(tissue or "").strip().upper()).strip("_")
    token = token or "UNSPECIFIED"
    return f"GTEX-V8-{token}"


def cohort_providers_path(project_root: Path | None = None) -> Path:
    raw = str(os.getenv("GLESKIT_COHORT_PROVIDERS_PATH", "")).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    return (root / "var" / "gleskit_mcp" / "cohort_providers.json").resolve()


def gtex_catalog_cache_path(project_root: Path | None = None) -> Path:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    return (root / "var" / "gleskit_mcp" / "cohort_catalog" / "gtex_catalog.json").resolve()


def _build_default_provider_specs() -> list[CohortProviderSpec]:
    tcga_datasets = [
        CohortDatasetSpec(
            dataset_id=f"TCGA-{code}",
            title=f"TCGA {title} ({code})",
            tissues=[tissue],
            mode="remote",
            metadata={"sample_count": sample_count},
        )
        for code, title, tissue, sample_count in _TCGA_PROJECTS
    ]
    gtex_datasets = [
        CohortDatasetSpec(
            dataset_id=_gtex_dataset_id_from_tissue(tissue),
            title=f"GTEx v8 {tissue}",
            tissues=[tissue],
            mode="remote",
        )
        for tissue in _GTEX_TISSUES_V8
    ]
    return [
        CohortProviderSpec(
            provider_id="tcga",
            name="TCGA",
            description="The Cancer Genome Atlas cohorts.",
            supports_tissue=True,
            supports_remote=True,
            connectors=["r:TCGAbiolinks"],
            datasets=tcga_datasets,
        ),
        CohortProviderSpec(
            provider_id="gtex",
            name="GTEx",
            description="Genotype-Tissue Expression cohorts.",
            supports_tissue=True,
            supports_remote=True,
            connectors=["r:recount3"],
            datasets=gtex_datasets,
        ),
        CohortProviderSpec(
            provider_id="metabric",
            name="METABRIC",
            description="Molecular Taxonomy of Breast Cancer International Consortium.",
            supports_tissue=True,
            supports_remote=True,
            connectors=["r:cgdsr_or_curated"],
            datasets=[
                CohortDatasetSpec(
                    dataset_id="METABRIC-BREAST",
                    title="METABRIC Breast Cancer",
                    tissues=["Breast"],
                    mode="remote",
                    metadata={"sample_count": 1904},
                )
            ],
        ),
        CohortProviderSpec(
            provider_id="evomedgenomics",
            name="EvoMedGenomics",
            description="External genomics studies exposed by EvoMedGenomics.",
            supports_tissue=True,
            supports_remote=True,
            connectors=["r:api_client"],
            datasets=[
                CohortDatasetSpec(
                    dataset_id="EVMG-DEMO-001",
                    title="EvoMedGenomics RNA-seq cohort",
                    tissues=["Peripheral blood"],
                    mode="remote",
                )
            ],
        ),
    ]


def _normalize_text(value: Any, default: str = "") -> str:
    token = str(value if value is not None else "").strip()
    return token if token else default


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        token = str(item if item is not None else "").strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _parse_dataset(raw: Any) -> CohortDatasetSpec | None:
    data = _as_dict(raw)
    dataset_id = _normalize_text(data.get("dataset_id"))
    if not dataset_id:
        return None
    title = _normalize_text(data.get("title"), default=dataset_id)
    known = {"dataset_id", "title", "tissues", "access", "mode"}
    metadata = {key: value for key, value in data.items() if key not in known}
    return CohortDatasetSpec(
        dataset_id=dataset_id,
        title=title,
        tissues=_normalize_str_list(data.get("tissues")),
        access=_normalize_text(data.get("access"), default="free"),
        mode=_normalize_text(data.get("mode"), default="remote"),
        metadata=metadata,
    )


def _parse_provider(raw: Any) -> CohortProviderSpec | None:
    data = _as_dict(raw)
    provider_id = _normalize_text(data.get("provider_id")).lower()
    if not provider_id:
        return None
    name = _normalize_text(data.get("name"), default=provider_id.upper())
    description = _normalize_text(data.get("description"))
    datasets_raw = data.get("datasets")
    datasets: list[CohortDatasetSpec] = []
    if isinstance(datasets_raw, list):
        for item in datasets_raw:
            parsed = _parse_dataset(item)
            if parsed is not None:
                datasets.append(parsed)
    known = {
        "provider_id",
        "name",
        "description",
        "supports_tissue",
        "supports_remote",
        "datasets",
        "connectors",
    }
    metadata = {key: value for key, value in data.items() if key not in known}
    return CohortProviderSpec(
        provider_id=provider_id,
        name=name,
        description=description,
        supports_tissue=_normalize_bool(data.get("supports_tissue"), default=True),
        supports_remote=_normalize_bool(data.get("supports_remote"), default=False),
        datasets=datasets,
        connectors=_normalize_str_list(data.get("connectors")),
        metadata=metadata,
    )


def _load_registry_specs(path: Path) -> list[CohortProviderSpec]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    parsed: list[CohortProviderSpec] = []
    for item in rows:
        provider = _parse_provider(item)
        if provider is not None:
            parsed.append(provider)
    return parsed


def _merge_unique_text(primary: list[str], secondary: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for source in (primary, secondary):
        for raw in source:
            token = str(raw if raw is not None else "").strip()
            if not token:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(token)
    return out


def _merge_datasets(base: list[CohortDatasetSpec], override: list[CohortDatasetSpec]) -> list[CohortDatasetSpec]:
    if not base:
        return list(override)
    if not override:
        return list(base)
    out = list(base)
    index_by_id = {item.dataset_id: idx for idx, item in enumerate(out)}
    for dataset in override:
        idx = index_by_id.get(dataset.dataset_id)
        if idx is None:
            out.append(dataset)
            index_by_id[dataset.dataset_id] = len(out) - 1
            continue
        base_dataset = out[idx]
        out[idx] = CohortDatasetSpec(
            dataset_id=dataset.dataset_id,
            title=dataset.title or base_dataset.title,
            tissues=_merge_unique_text(dataset.tissues, base_dataset.tissues),
            access=dataset.access or base_dataset.access,
            mode=dataset.mode or base_dataset.mode,
            metadata={**dict(base_dataset.metadata), **dict(dataset.metadata)},
        )
    return out


def _merge_provider_specs(defaults: list[CohortProviderSpec], registry: list[CohortProviderSpec]) -> list[CohortProviderSpec]:
    if not registry:
        return list(defaults)
    merged_by_id = {spec.provider_id: spec for spec in defaults}
    ordered_ids = [spec.provider_id for spec in defaults]
    for override in registry:
        base = merged_by_id.get(override.provider_id)
        if base is None:
            merged_by_id[override.provider_id] = override
            ordered_ids.append(override.provider_id)
            continue
        merged_by_id[override.provider_id] = CohortProviderSpec(
            provider_id=override.provider_id,
            name=override.name or base.name,
            description=override.description or base.description,
            supports_tissue=override.supports_tissue,
            supports_remote=override.supports_remote,
            datasets=_merge_datasets(base.datasets, override.datasets),
            connectors=_merge_unique_text(override.connectors, base.connectors),
            metadata={**dict(base.metadata), **dict(override.metadata)},
        )
    return [merged_by_id[provider_id] for provider_id in ordered_ids if provider_id in merged_by_id]


def _release_number(value: Any) -> int:
    token = str(value if value is not None else "").strip().lower()
    match = re.search(r"(\d+)", token)
    if not match:
        return -1
    try:
        return int(match.group(1))
    except ValueError:
        return -1


def _sort_gtex_datasets(datasets: list[CohortDatasetSpec]) -> list[CohortDatasetSpec]:
    return sorted(
        datasets,
        key=lambda dataset: (
            -_release_number(dataset.metadata.get("release")),
            str(dataset.title).lower(),
            str(dataset.dataset_id).lower(),
        ),
    )


def _gtex_catalog_script_path(project_root: Path | None = None) -> Path:
    packaged = Path(str(resources.files("gleskit_mcp").joinpath("r", "gtex_catalog.R")))
    if packaged.exists():
        return packaged.resolve()
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    return (root / "src" / "gleskit_mcp" / "r" / "gtex_catalog.R").resolve()


def _refresh_gtex_catalog_via_r(project_root: Path | None = None) -> list[CohortDatasetSpec]:
    script_path = _gtex_catalog_script_path(project_root)
    if not script_path.exists():
        raise RuntimeError(f"GTEx catalog refresh script not found: {script_path}")
    rscript_bin = str(os.getenv("GLESKIT_RSCRIPT_BIN", "Rscript")).strip() or "Rscript"
    with tempfile.TemporaryDirectory(prefix="gleskit_gtex_catalog_") as tmp:
        output_path = Path(tmp) / "gtex_catalog.json"
        env = dict(os.environ)
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("OPENBLAS_NUM_THREADS", "1")
        proc = subprocess.run(
            [rscript_bin, script_path.as_posix(), output_path.as_posix()],
            capture_output=True,
            text=True,
            env=env,
            timeout=900,
        )
        if proc.returncode != 0:
            stderr = str(proc.stderr or "").strip()
            stdout = str(proc.stdout or "").strip()
            detail = stderr or stdout or "Unknown GTEx refresh error."
            raise RuntimeError(f"GTEx catalog refresh failed: {detail}")
        if not output_path.exists():
            raise RuntimeError("GTEx catalog refresh did not produce output JSON.")
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    rows = payload.get("datasets") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("GTEx catalog refresh output is invalid (missing datasets array).")
    datasets: list[CohortDatasetSpec] = []
    for row in rows:
        parsed = _parse_dataset(row)
        if parsed is not None:
            datasets.append(parsed)
    if not datasets:
        raise RuntimeError("GTEx catalog refresh returned no datasets.")
    return _sort_gtex_datasets(datasets)


def _load_cached_gtex_datasets(project_root: Path | None = None) -> list[CohortDatasetSpec]:
    cache_path = gtex_catalog_cache_path(project_root)
    if not cache_path.exists():
        return []
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload.get("datasets") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    datasets: list[CohortDatasetSpec] = []
    for row in rows:
        parsed = _parse_dataset(row)
        if parsed is not None:
            datasets.append(parsed)
    return _sort_gtex_datasets(datasets)


def _write_gtex_cache(datasets: list[CohortDatasetSpec], project_root: Path | None = None) -> None:
    cache_path = gtex_catalog_cache_path(project_root)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider_id": "gtex",
        "fetched_at": float(__import__("time").time()),
        "source": "gtex_api_v2",
        "datasets": [dataset.to_dict() for dataset in datasets],
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cohort_provider_specs(project_root: Path | None = None) -> list[CohortProviderSpec]:
    defaults = _build_default_provider_specs()
    merged = list(defaults)
    registry = _load_registry_specs(cohort_providers_path(project_root))
    if registry:
        merged = _merge_provider_specs(merged, registry)
    cached_gtex = _load_cached_gtex_datasets(project_root)
    if cached_gtex:
        merged_by_id = {spec.provider_id: spec for spec in merged}
        base = merged_by_id.get("gtex")
        if base is not None:
            merged_by_id["gtex"] = CohortProviderSpec(
                provider_id=base.provider_id,
                name=base.name,
                description=base.description,
                supports_tissue=base.supports_tissue,
                supports_remote=base.supports_remote,
                connectors=base.connectors,
                datasets=cached_gtex,
                metadata=dict(base.metadata),
            )
            ordered = []
            seen = set()
            for spec in merged:
                if spec.provider_id == "gtex":
                    ordered.append(merged_by_id["gtex"])
                else:
                    ordered.append(spec)
                seen.add(spec.provider_id)
            return ordered
    return merged


def list_cohort_providers(project_root: Path | None = None) -> list[dict[str, Any]]:
    return [provider.summary() for provider in load_cohort_provider_specs(project_root)]


def get_cohort_provider(provider_id: str, project_root: Path | None = None) -> CohortProviderSpec:
    token = str(provider_id or "").strip().lower()
    for provider in load_cohort_provider_specs(project_root):
        if provider.provider_id.lower() == token:
            return provider
    raise FileNotFoundError(f"Unknown cohort provider: {provider_id}")


def refresh_cohort_provider_catalog(provider_id: str, project_root: Path | None = None) -> list[dict[str, Any]]:
    token = str(provider_id or "").strip().lower()
    if token == "gtex":
        datasets = _refresh_gtex_catalog_via_r(project_root)
        _write_gtex_cache(datasets, project_root)
        return [dataset.to_dict() for dataset in datasets]
    return list_cohort_catalog(provider_id, project_root=project_root)


def list_cohort_catalog(provider_id: str, project_root: Path | None = None) -> list[dict[str, Any]]:
    token = str(provider_id or "").strip().lower()
    if token == "gtex":
        cached = _load_cached_gtex_datasets(project_root)
        if cached:
            return [dataset.to_dict() for dataset in cached]
        try:
            refreshed = _refresh_gtex_catalog_via_r(project_root)
        except Exception:
            refreshed = _sort_gtex_datasets(next((spec.datasets for spec in load_cohort_provider_specs(project_root) if spec.provider_id == "gtex"), []))
        else:
            _write_gtex_cache(refreshed, project_root)
        return [dataset.to_dict() for dataset in refreshed]
    provider = get_cohort_provider(provider_id, project_root)
    return [dataset.to_dict() for dataset in provider.datasets]


def get_cohort_dataset(provider_id: str, dataset_id: str, project_root: Path | None = None) -> CohortDatasetSpec:
    provider = get_cohort_provider(provider_id, project_root=project_root)
    token = str(dataset_id or "").strip().lower()
    for dataset in provider.datasets:
        if dataset.dataset_id.lower() == token:
            return dataset
    raise FileNotFoundError(f"Unknown cohort dataset: {provider_id}/{dataset_id}")
