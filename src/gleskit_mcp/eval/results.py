"""Notebook-friendly access to persisted BixBench and RepoBench run results.

The evaluation runners already write compact summary CSV files.  This module
normalizes those files for analysis without re-running agents or judges.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


_BENCHMARKS = ("bixbench", "repobench")
_SUMMARY_FILENAMES = {
    "bixbench": Path("summary/questions.csv"),
    "repobench": Path("summary/tasks.csv"),
}
_ITEM_ID_COLUMNS = {"bixbench": "question_id", "repobench": "task_id"}


def _normalize_benchmark(benchmark: str) -> str:
    normalized = str(benchmark or "").strip().lower()
    aliases = {
        "bix": "bixbench",
        "bixbench": "bixbench",
        "repo": "repobench",
        "repobench": "repobench",
    }
    if normalized not in aliases:
        options = ", ".join(_BENCHMARKS)
        raise ValueError(f"Unknown benchmark `{benchmark}`. Expected one of: {options}.")
    return aliases[normalized]


def _find_project_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_file():
        resolved = resolved.parent
    for candidate in (resolved, *resolved.parents):
        if (candidate / "pyproject.toml").is_file() or (candidate / "var").is_dir():
            return candidate
    return resolved


@dataclass(frozen=True, slots=True)
class RunRef:
    """A benchmark-qualified run name used by :class:`EvaluationResults`."""

    benchmark: str
    run_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "benchmark", _normalize_benchmark(self.benchmark))
        name = str(self.run_name or "").strip()
        if not name:
            raise ValueError("run_name must not be empty.")
        object.__setattr__(self, "run_name", name)

    @property
    def source_run(self) -> str:
        """Stable provenance label stored in merged study data."""
        return f"{self.benchmark}:{self.run_name}"


@dataclass(slots=True)
class RunComparison:
    """Prepared per-item comparison of two runs from the same benchmark."""

    left: RunRef
    right: RunRef
    left_data: pd.DataFrame
    right_data: pd.DataFrame
    paired: pd.DataFrame
    run_statistics: pd.DataFrame
    paired_statistics: pd.DataFrame


class EvaluationResults:
    """Load, compare, and merge saved evaluation run summaries.

    Parameters
    ----------
    project_root:
        Repository root. When omitted, the current directory and its parents
        are searched for the project root.
    """

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = _find_project_root(Path(project_root) if project_root is not None else Path.cwd())

    def list_runs(self, benchmark: str | None = None) -> pd.DataFrame:
        """Return available run folders and whether their summary is ready to load."""
        benchmarks = (_normalize_benchmark(benchmark),) if benchmark else _BENCHMARKS
        rows: list[dict[str, object]] = []
        for benchmark_name in benchmarks:
            runs_root = self._runs_root(benchmark_name)
            if not runs_root.is_dir():
                continue
            for run_path in sorted((path for path in runs_root.iterdir() if path.is_dir()), key=lambda path: path.name):
                summary_path = run_path / _SUMMARY_FILENAMES[benchmark_name]
                counts = _empty_run_counts()
                if summary_path.is_file():
                    try:
                        counts = _summary_counts(pd.read_csv(summary_path), benchmark_name)
                    except (OSError, pd.errors.EmptyDataError, pd.errors.ParserError):
                        pass
                rows.append(
                    {
                        "benchmark": benchmark_name,
                        "run_name": run_path.name,
                        "source_run": f"{benchmark_name}:{run_path.name}",
                        "run_path": str(run_path),
                        "summary_path": str(summary_path),
                        "summary_available": summary_path.is_file(),
                        **counts,
                    }
                )
        return pd.DataFrame(
            rows,
            columns=[
                "benchmark",
                "run_name",
                "source_run",
                "run_path",
                "summary_path",
                "summary_available",
                "item_count",
                "answered_count",
                "rejected_count",
                "unanswered_count",
                "judge_scored_count",
                "judge_missing_count",
            ],
        )

    def load_run(
        self,
        run: str | RunRef,
        *,
        benchmark: str | None = None,
        include_usage: bool = True,
        include_bixbench_metadata: bool = False,
        include_judge_details: bool = False,
    ) -> pd.DataFrame:
        """Load one run's saved summary into a normalized dataframe.

        Native summary columns are retained. The returned dataframe additionally
        includes ``item_id``, score fields, and run provenance columns shared by
        both benchmarks. BixBench can additionally load saved session usage,
        benchmark-source metadata, and judge-detail artifacts when requested.
        """
        reference = self._resolve_run_ref(run, benchmark=benchmark)
        run_path = self._run_path(reference)
        summary_path = run_path / _SUMMARY_FILENAMES[reference.benchmark]
        if not summary_path.is_file():
            raise FileNotFoundError(
                f"Run `{reference.source_run}` has no saved summary at {summary_path}."
            )

        frame = pd.read_csv(summary_path)
        item_id_column = _ITEM_ID_COLUMNS[reference.benchmark]
        if item_id_column not in frame.columns:
            raise ValueError(f"Summary for `{reference.source_run}` is missing `{item_id_column}`.")

        result = frame.copy()
        score_0_4, normalized_score = self._score_columns(result)
        result["benchmark"] = reference.benchmark
        result["run_name"] = reference.run_name
        result["source_run"] = reference.source_run
        result["run_path"] = str(run_path)
        result["summary_path"] = str(summary_path)
        result["item_type"] = "question" if reference.benchmark == "bixbench" else "task"
        result["item_id"] = result[item_id_column].astype("string")
        result["score_0_4"] = score_0_4
        result["normalized_score"] = normalized_score
        result["score_missing"] = score_0_4.isna()
        result["has_positive_score"] = score_0_4.gt(0)

        if include_usage and reference.benchmark == "bixbench":
            usage = self._load_bixbench_usage(run_path)
            if not usage.empty:
                result = result.merge(usage, on="item_id", how="left", validate="one_to_one")
                result["usage_available"] = result["usage_available"].eq(True)

        if include_bixbench_metadata and reference.benchmark == "bixbench":
            metadata = self.load_bixbench_metadata()
            overlapping = [
                column
                for column in metadata.columns
                if column != "item_id" and column in result.columns
            ]
            result = result.merge(
                metadata,
                on="item_id",
                how="left",
                validate="one_to_one",
                suffixes=("", "__metadata"),
            )
            for column in overlapping:
                metadata_column = f"{column}__metadata"
                result[column] = result[column].where(result[column].notna(), result[metadata_column])
                result = result.drop(columns=metadata_column)

        if include_judge_details and reference.benchmark == "bixbench":
            judge_details = self._load_bixbench_judge_details(run_path)
            if not judge_details.empty:
                result = result.merge(judge_details, on="item_id", how="left", validate="one_to_one")
        elif include_judge_details and reference.benchmark == "repobench":
            judge_details = self._load_repobench_judge_details(run_path)
            if not judge_details.empty:
                result = result.merge(judge_details, on="item_id", how="left", validate="one_to_one")

        shared_columns = [
            "benchmark",
            "run_name",
            "source_run",
            "run_path",
            "summary_path",
            "item_type",
            "item_id",
            "score_0_4",
            "normalized_score",
            "score_missing",
            "has_positive_score",
        ]
        native_columns = [column for column in result.columns if column not in shared_columns]
        return result[shared_columns + native_columns]

    def load_bixbench_metadata(self) -> pd.DataFrame:
        """Load question-level BixBench source metadata for the local dataset.

        The BixBench source records expose stable case identifiers, biological
        species, benchmark category labels, and source-paper metadata. The
        upstream dataset does not provide a human-readable case-title field.
        """
        candidates = (
            self.project_root / "var" / "bixbench" / "hf_repo" / "BixBench.gleskit_scope.jsonl",
            self.project_root / "var" / "bixbench" / "hf_repo" / "BixBench.jsonl",
        )
        source_path = next((path for path in candidates if path.is_file()), None)
        if source_path is None:
            options = ", ".join(str(path) for path in candidates)
            raise FileNotFoundError(f"Could not find local BixBench source metadata. Looked in: {options}.")

        rows: list[dict[str, object]] = []
        for line in source_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            item_id = str(record.get("question_id") or "").strip()
            if not item_id:
                continue
            rows.append(
                {
                    "item_id": item_id,
                    "source_case_uuid": _text_or_none(record.get("id")),
                    "species": _text_or_none(record.get("species")),
                    "task_categories": _categories_text(record.get("categories")),
                    "source_paper": _text_or_none(record.get("paper")),
                    "source_hypothesis": _text_or_none(record.get("hypothesis")),
                    "source_tag": _text_or_none(record.get("tag")),
                    "source_version": _text_or_none(record.get("version")),
                    "source_data_folder": _text_or_none(record.get("data_folder")),
                }
            )

        metadata = pd.DataFrame(rows)
        if metadata.empty:
            return pd.DataFrame(columns=["item_id", "task_categories"])
        duplicate_ids = metadata.loc[metadata["item_id"].duplicated(), "item_id"].tolist()
        if duplicate_ids:
            examples = ", ".join(duplicate_ids[:5])
            raise ValueError(f"BixBench source metadata has duplicate question_id values: {examples}.")
        return metadata

    @staticmethod
    def explode_bixbench_categories(
        data: pd.DataFrame,
        *,
        category_column: str = "task_categories",
        output_column: str = "task_category",
    ) -> pd.DataFrame:
        """Return one row per BixBench category label without mutating ``data``."""
        _require_columns(data, {category_column})
        result = data.copy()
        result[output_column] = result[category_column].map(_parse_categories)
        result = result.explode(output_column, ignore_index=True)
        return result.loc[result[output_column].notna() & result[output_column].ne("")].copy()

    def compare_runs(
        self,
        left: str | RunRef,
        right: str | RunRef,
        *,
        benchmark: str | None = None,
        include_usage: bool = True,
        include_bixbench_metadata: bool = False,
        include_judge_details: bool = False,
    ) -> RunComparison:
        """Pair two same-benchmark runs by question/task and calculate deltas."""
        left_ref = self._resolve_run_ref(left, benchmark=benchmark)
        right_ref = self._resolve_run_ref(right, benchmark=benchmark)
        if left_ref.benchmark != right_ref.benchmark:
            raise ValueError("Runs can only be compared when they belong to the same benchmark.")

        left_data = self.load_run(
            left_ref,
            include_usage=include_usage,
            include_bixbench_metadata=include_bixbench_metadata,
            include_judge_details=include_judge_details,
        )
        right_data = self.load_run(
            right_ref,
            include_usage=include_usage,
            include_bixbench_metadata=include_bixbench_metadata,
            include_judge_details=include_judge_details,
        )
        paired = self._pair_runs(left_data, right_data)

        run_statistics = pd.concat(
            [
                self.score_summary(left_data).assign(source_run=left_ref.source_run),
                self.score_summary(right_data).assign(source_run=right_ref.source_run),
            ],
            ignore_index=True,
        )
        paired_statistics = self._paired_statistics(paired)
        return RunComparison(
            left=left_ref,
            right=right_ref,
            left_data=left_data,
            right_data=right_data,
            paired=paired,
            run_statistics=run_statistics,
            paired_statistics=paired_statistics,
        )

    def merge_runs(
        self,
        runs: Sequence[str | RunRef],
        *,
        benchmark: str | None = None,
        include_usage: bool = True,
        include_bixbench_metadata: bool = False,
        include_judge_details: bool = False,
    ) -> pd.DataFrame:
        """Concatenate two or more run datasets, retaining source provenance."""
        if isinstance(runs, str) or not runs:
            raise ValueError("runs must contain at least two run names or RunRef values.")
        if len(runs) < 2:
            raise ValueError("merge_runs requires at least two runs.")
        frames = [
            self.load_run(
                run,
                benchmark=benchmark,
                include_usage=include_usage,
                include_bixbench_metadata=include_bixbench_metadata,
                include_judge_details=include_judge_details,
            )
            for run in runs
        ]
        columns_with_values = {
            column
            for frame in frames
            for column in frame.columns
            if not frame[column].isna().all()
        }
        prepared_frames = [
            frame[[column for column in frame.columns if column in columns_with_values]]
            for frame in frames
        ]
        return pd.concat(prepared_frames, ignore_index=True, sort=False)

    @staticmethod
    def scored_rows(data: pd.DataFrame, *, positive_only: bool = False) -> pd.DataFrame:
        """Return rows with a committee score, optionally excluding true zero scores."""
        _require_columns(data, {"score_0_4", "score_missing"})
        mask = ~data["score_missing"].astype(bool)
        if positive_only:
            mask &= data["score_0_4"].gt(0)
        return data.loc[mask].copy()

    @staticmethod
    def score_summary(
        data: pd.DataFrame,
        *,
        positive_only: bool = False,
    ) -> pd.DataFrame:
        """Calculate score counts, mean, standard deviation, and standard error."""
        _require_columns(data, {"score_0_4", "normalized_score", "score_missing"})
        summary_data = EvaluationResults.scored_rows(data, positive_only=positive_only)
        raw_scores = pd.to_numeric(summary_data["score_0_4"], errors="coerce").dropna()
        normalized_scores = pd.to_numeric(summary_data["normalized_score"], errors="coerce").dropna()
        return pd.DataFrame(
            [
                {
                    "items": len(summary_data),
                    "scored_items": len(raw_scores),
                    "zero_score_items": int(raw_scores.eq(0).sum()),
                    "mean_score_0_4": raw_scores.mean() if not raw_scores.empty else np.nan,
                    "std_score_0_4": raw_scores.std(ddof=1) if len(raw_scores) > 1 else np.nan,
                    "se_score_0_4": _standard_error(raw_scores),
                    "mean_normalized_score": normalized_scores.mean() if not normalized_scores.empty else np.nan,
                    "std_normalized_score": normalized_scores.std(ddof=1) if len(normalized_scores) > 1 else np.nan,
                    "se_normalized_score": _standard_error(normalized_scores),
                }
            ]
        )

    @staticmethod
    def add_token_costs(
        data: pd.DataFrame,
        pricing: Mapping[str, float],
        *,
        output_column: str = "computed_cost",
    ) -> pd.DataFrame:
        """Add a token-priced cost column using input/output/cache per-million rates."""
        required = {"tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read"}
        _require_columns(data, required)
        required_pricing = {"input_per_mtok", "output_per_mtok", "cache_read_per_mtok"}
        missing_pricing = required_pricing - set(pricing)
        if missing_pricing:
            missing = ", ".join(sorted(missing_pricing))
            raise ValueError(f"pricing is missing: {missing}.")

        result = data.copy()
        input_tokens = pd.to_numeric(result["tokens_input"], errors="coerce").fillna(0.0)
        output_tokens = pd.to_numeric(result["tokens_output"], errors="coerce").fillna(0.0)
        reasoning_tokens = pd.to_numeric(result["tokens_reasoning"], errors="coerce").fillna(0.0)
        cache_tokens = pd.to_numeric(result["tokens_cache_read"], errors="coerce").fillna(0.0)
        result[output_column] = (
            input_tokens * float(pricing["input_per_mtok"])
            + (output_tokens + reasoning_tokens) * float(pricing["output_per_mtok"])
            + cache_tokens * float(pricing["cache_read_per_mtok"])
        ) / 1_000_000
        return result

    def _runs_root(self, benchmark: str) -> Path:
        return self.project_root / "var" / benchmark / "runs"

    def _run_path(self, reference: RunRef) -> Path:
        path = self._runs_root(reference.benchmark) / reference.run_name
        if not path.is_dir():
            raise FileNotFoundError(f"Unknown run `{reference.source_run}` at {path}.")
        return path

    def _resolve_run_ref(self, run: str | RunRef, *, benchmark: str | None) -> RunRef:
        if isinstance(run, RunRef):
            if benchmark is not None and run.benchmark != _normalize_benchmark(benchmark):
                raise ValueError("RunRef benchmark does not match the benchmark argument.")
            return run

        name = str(run or "").strip()
        if not name:
            raise ValueError("run must be a non-empty run name or RunRef.")
        if benchmark is not None:
            return RunRef(benchmark, name)

        candidates = [
            RunRef(benchmark_name, name)
            for benchmark_name in _BENCHMARKS
            if (self._runs_root(benchmark_name) / name).is_dir()
        ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise FileNotFoundError(f"Could not find a BixBench or RepoBench run named `{name}`.")
        raise ValueError(
            f"Run name `{name}` exists in more than one benchmark. Pass benchmark= or use RunRef."
        )

    @staticmethod
    def _score_columns(data: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        raw = _numeric_column(data, "committee_level_mean_final")
        legacy_normalized = _numeric_column(data, "judge_score")
        if raw.isna().all() and not legacy_normalized.isna().all():
            raw = legacy_normalized * 4.0
        normalized = _numeric_column(data, "committee_level_mean_normalized")
        if normalized.isna().all():
            normalized = raw / 4.0
        normalized = normalized.where(normalized.notna(), legacy_normalized)
        return raw, normalized

    def _load_bixbench_usage(self, run_path: Path) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for episode_path in sorted(run_path.glob("episodes/*/episode.json")):
            try:
                episode = json.loads(episode_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            item_id = str(episode.get("episode_id") or episode_path.parent.name).strip()
            if not item_id:
                continue
            session = self._load_session_export(run_path, episode_path, episode)
            rows.append(
                {
                    "item_id": item_id,
                    "usage_available": bool(session),
                    "tokens_input": _usage_value(session, "tokens_input"),
                    "tokens_output": _usage_value(session, "tokens_output"),
                    "tokens_cache_read": _usage_value(session, "tokens_cache_read"),
                    "tokens_reasoning": _usage_value(session, "tokens_reasoning"),
                    "reported_cost": _usage_value(session, "cost"),
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _load_repobench_judge_details(run_path: Path) -> pd.DataFrame:
        """Flatten saved RepoBench specialist verdicts into task-level columns."""
        rows: list[dict[str, object]] = []
        specialist_names = (
            "biomedical_content_specialist",
            "evidence_support_specialist",
            "provenance_quality_specialist",
            "template_compliance_specialist",
        )
        for judge_path in sorted(run_path.glob("episodes/*/judge.json")):
            try:
                payload = json.loads(judge_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            item_id = str(payload.get("task_id") or judge_path.parent.name).strip()
            if not item_id:
                continue
            specialists = payload.get("specialists") if isinstance(payload.get("specialists"), dict) else {}
            row: dict[str, object] = {"item_id": item_id}
            for specialist_name in specialist_names:
                specialist = specialists.get(specialist_name)
                if not isinstance(specialist, dict):
                    continue
                prefix = specialist_name.removesuffix("_specialist")
                row[f"{prefix}_category"] = _text_or_none(specialist.get("category"))
                row[f"{prefix}_summary"] = _text_or_none(specialist.get("summary"))
                row[f"{prefix}_failure_codes"] = _joined_text(specialist.get("failure_codes"))
            rows.append(row)
        return pd.DataFrame(rows)

    @staticmethod
    def _load_bixbench_judge_details(run_path: Path) -> pd.DataFrame:
        """Flatten saved arbiter and specialist findings into question-level columns."""
        rows: list[dict[str, object]] = []
        for judge_path in sorted(run_path.glob("episodes/*/judge.json")):
            try:
                payload = json.loads(judge_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            item_id = str(payload.get("question_id") or judge_path.parent.name).strip()
            if not item_id:
                continue
            judge = payload.get("judge") if isinstance(payload.get("judge"), dict) else {}
            specialists = payload.get("specialists") if isinstance(payload.get("specialists"), dict) else {}
            row: dict[str, object] = {
                "item_id": item_id,
                "judge_disagreement_span": _numeric_or_none(payload.get("specialist_disagreement_span")),
                "judge_review_required": _boolean_or_none(payload.get("review_required")),
                "judge_review_reasons": _joined_text(payload.get("review_reasons")),
                "judge_quality_flags": _joined_text(payload.get("quality_flags")),
                # The compact summary already exposes judge_disagreement_flags.
                # Keep the artifact-derived value separately to avoid merge suffixes.
                "judge_detail_disagreement_flags": _joined_text(judge.get("disagreement_flags")),
                "judge_major_issues": _joined_text(judge.get("major_issues")),
                "judge_major_strengths": _joined_text(judge.get("major_strengths")),
            }
            for prefix, specialist_name in (
                ("judge_stats", "stats_specialist"),
                ("judge_biology", "biology_specialist"),
                ("judge_evidence", "evidence_checker"),
            ):
                specialist = specialists.get(specialist_name)
                if not isinstance(specialist, dict):
                    continue
                row[f"{prefix}_summary"] = _text_or_none(specialist.get("summary"))
                row[f"{prefix}_failure_codes"] = _joined_text(specialist.get("failure_codes"))
                row[f"{prefix}_failed_criteria"] = _joined_text(specialist.get("failed_criteria"))
                row[f"{prefix}_key_observations"] = _joined_text(specialist.get("key_observations"))
            evidence = specialists.get("evidence_checker")
            if isinstance(evidence, dict):
                row["judge_evidence_gaps"] = _joined_text(evidence.get("evidence_gaps"))
                row["judge_evidence_unsupported_claims"] = _joined_text(evidence.get("unsupported_claims"))
            rows.append(row)
        return pd.DataFrame(rows)

    @staticmethod
    def _load_session_export(run_path: Path, episode_path: Path, episode: Mapping[str, object]) -> Mapping[str, object]:
        raw_path = str(episode.get("private_session_export_path") or "").strip()
        candidates: list[Path] = []
        if raw_path:
            exported_path = Path(raw_path)
            candidates.append(exported_path)
            if not exported_path.is_absolute():
                candidates.extend([run_path / exported_path, episode_path.parent / exported_path])
        candidates.append(episode_path.parent / "private/attempts/attempt_01/session_export.json")

        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session = payload.get("session") if isinstance(payload, dict) else None
            if isinstance(session, dict):
                return session
        return {}

    @staticmethod
    def _pair_runs(left_data: pd.DataFrame, right_data: pd.DataFrame) -> pd.DataFrame:
        _require_columns(left_data, {"item_id", "score_0_4", "normalized_score"})
        _require_columns(right_data, {"item_id", "score_0_4", "normalized_score"})
        for label, frame in (("left", left_data), ("right", right_data)):
            duplicate_ids = frame.loc[frame["item_id"].duplicated(), "item_id"].tolist()
            if duplicate_ids:
                examples = ", ".join(str(item) for item in duplicate_ids[:5])
                raise ValueError(f"{label} run has duplicate item_id values: {examples}.")

        left_renamed = left_data.rename(columns={column: f"left_{column}" for column in left_data.columns if column != "item_id"})
        right_renamed = right_data.rename(columns={column: f"right_{column}" for column in right_data.columns if column != "item_id"})
        paired = left_renamed.merge(right_renamed, on="item_id", how="inner", validate="one_to_one")
        paired["delta_score_0_4"] = paired["left_score_0_4"] - paired["right_score_0_4"]
        paired["delta_normalized_score"] = (
            paired["left_normalized_score"] - paired["right_normalized_score"]
        )
        return paired.sort_values("item_id").reset_index(drop=True)

    @staticmethod
    def _paired_statistics(paired: pd.DataFrame) -> pd.DataFrame:
        _require_columns(paired, {"delta_score_0_4", "delta_normalized_score"})
        score_deltas = pd.to_numeric(paired["delta_score_0_4"], errors="coerce").dropna()
        normalized_deltas = pd.to_numeric(paired["delta_normalized_score"], errors="coerce").dropna()
        return pd.DataFrame(
            [
                {
                    "paired_items": len(paired),
                    "scored_pairs": len(score_deltas),
                    "mean_delta_score_0_4": score_deltas.mean() if not score_deltas.empty else np.nan,
                    "se_delta_score_0_4": _standard_error(score_deltas),
                    "mean_delta_normalized_score": normalized_deltas.mean() if not normalized_deltas.empty else np.nan,
                    "se_delta_normalized_score": _standard_error(normalized_deltas),
                    # Direction uses the observed continuous score. Exact
                    # equality is reported separately; no tolerance or
                    # practical-equivalence band is imposed.
                    "left_higher": int(score_deltas.gt(0.0).sum()),
                    "right_higher": int(score_deltas.lt(0.0).sum()),
                    "exact_ties": int(score_deltas.eq(0.0).sum()),
                }
            ]
        )


def _numeric_column(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(np.nan, index=data.index, dtype="float64")
    return pd.to_numeric(data[column], errors="coerce")


def _text_or_none(value: object) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip()
    return text or None


def _categories_text(value: object) -> str | None:
    categories = _parse_categories(value)
    return ", ".join(categories) if categories else None


def _parse_categories(value: object) -> list[str]:
    """Normalize comma-delimited and legacy list-style BixBench category fields."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    if isinstance(value, (list, tuple, set)):
        raw_categories = value
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith(("[", "(")):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, (list, tuple, set)):
                raw_categories = parsed
            else:
                raw_categories = text.split(",")
        else:
            raw_categories = text.split(",")
    return [category for item in raw_categories if (category := str(item).strip())]


def _joined_text(value: object) -> str | None:
    if isinstance(value, (list, tuple, set)):
        values = [_text_or_none(item) for item in value]
        joined = "; ".join(item for item in values if item)
        return joined or None
    return _text_or_none(value)


def _numeric_or_none(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def _boolean_or_none(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    return None


def _empty_run_counts() -> dict[str, int | None]:
    return {
        "item_count": None,
        "answered_count": None,
        "rejected_count": None,
        "unanswered_count": None,
        "judge_scored_count": None,
        "judge_missing_count": None,
    }


def _summary_counts(data: pd.DataFrame, benchmark: str) -> dict[str, int]:
    item_count = len(data)
    if benchmark == "bixbench":
        answered = _truthy_column(data, "answer_present") | _nonempty_text_column(data, "effective_answer")
        rejected = _truthy_column(data, "abstain")
    else:
        answered = _truthy_column(data, "report_saved")
        if "report_saved" not in data.columns:
            answered = _text_equals(data, "status", "completed")
        rejected = pd.Series(False, index=data.index)

    rejected |= _text_equals(data, "status", "rejected")
    score_0_4, _ = EvaluationResults._score_columns(data)
    judge_scored = score_0_4.notna()
    return {
        "item_count": item_count,
        "answered_count": int(answered.sum()),
        "rejected_count": int(rejected.sum()),
        "unanswered_count": int((~answered).sum()),
        "judge_scored_count": int(judge_scored.sum()),
        "judge_missing_count": int((~judge_scored).sum()),
    }


def _truthy_column(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(False, index=data.index)
    return data[column].map(
        lambda value: value is True or str(value).strip().lower() in {"1", "true", "yes", "y"}
    )


def _nonempty_text_column(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(False, index=data.index)
    values = data[column].astype("string")
    return values.notna() & values.str.strip().ne("")


def _text_equals(data: pd.DataFrame, column: str, expected: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(False, index=data.index)
    return data[column].astype("string").str.strip().str.lower().eq(expected.lower())


def _usage_value(session: Mapping[str, object], key: str) -> float | None:
    value = session.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _standard_error(values: pd.Series) -> float:
    return values.std(ddof=1) / np.sqrt(len(values)) if len(values) > 1 else np.nan


def _require_columns(data: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = set(columns) - set(data.columns)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"Dataframe is missing required result columns: {names}.")
