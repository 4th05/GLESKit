from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment
from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    roc_auc_score,
)
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from gleskit_mcp.analysis.expression_matrix import ExpressionMatrixAnalysisBackend, LoadedMatrix
from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.tabular import load_table
from gleskit_mcp.utils import clean_text


AnalysisAxis = Literal["rows", "columns"]
TaskType = Literal["classification", "regression"]
ModelFamily = Literal["logistic_regression", "linear_regression", "ridge"]
SplitStrategy = Literal["train_test", "stratified_train_test", "group_train_test"]


class SupervisedExpressionBackend:
    def __init__(self, expression_matrix_backend: ExpressionMatrixAnalysisBackend | None = None) -> None:
        self.expression_matrix_backend = expression_matrix_backend or ExpressionMatrixAnalysisBackend()

    def run_supervised_model(
        self,
        matrix: LoadedMatrix,
        *,
        metadata_path: str = "",
        metadata_table_text: str = "",
        metadata_delimiter: str = "",
        metadata_sheet_name: str = "",
        metadata_header_row: int = 1,
        metadata_skip_rows: int = 0,
        analysis_axis: AnalysisAxis = "columns",
        sample_column: str = "sample",
        target_column: str,
        task: TaskType = "classification",
        model_family: ModelFamily = "logistic_regression",
        split_strategy: SplitStrategy = "train_test",
        group_column: str = "",
        test_size: float = 0.3,
        random_seed: int = 0,
        top_n_features: int = 25,
    ) -> dict[str, Any]:
        dataset = self._prepare_supervised_dataset(
            matrix,
            metadata_path=metadata_path,
            metadata_table_text=metadata_table_text,
            metadata_delimiter=metadata_delimiter,
            metadata_sheet_name=metadata_sheet_name,
            metadata_header_row=metadata_header_row,
            metadata_skip_rows=metadata_skip_rows,
            analysis_axis=analysis_axis,
            sample_column=sample_column,
            target_column=target_column,
            group_column=group_column,
        )
        self._validate_model_family(task=task, model_family=model_family)
        split = self._split_dataset(
            dataset,
            task=task,
            split_strategy=split_strategy,
            test_size=test_size,
            random_seed=random_seed,
        )
        estimator = self._build_estimator(task=task, model_family=model_family)
        estimator.fit(split["X_train"], split["y_train"])

        predictions = estimator.predict(split["X_test"])
        metrics = (
            self._classification_metrics(split["y_test"], predictions, estimator, split["X_test"])
            if task == "classification"
            else self._regression_metrics(split["y_test"], predictions)
        )
        coefficient_rows = self._coefficient_rows(
            estimator=estimator,
            feature_labels=dataset.feature_labels,
            task=task,
            top_n=max(1, int(top_n_features)),
        )
        prediction_rows = [
            {
                "sample": split["test_labels"][index],
                "observed": self._json_scalar(split["y_test"][index]),
                "predicted": self._json_scalar(predictions[index]),
            }
            for index in range(len(split["test_labels"]))
        ]
        return {
            "summary": (
                f"Ran {task} with {model_family} on {dataset.sample_count} sample(s) "
                f"using {split_strategy} splitting."
            ),
            "provider": "supervised_expression",
            "analysis": "supervised_expression_model",
            "input": {
                "matrix_path": matrix.path,
                "matrix_source_kind": matrix.source_kind,
                "matrix_sheet_name": matrix.sheet_name,
                "matrix_available_sheets": matrix.available_sheets or [],
                "matrix_header_row": matrix.header_row,
                "matrix_skip_rows": matrix.skip_rows,
                "metadata_path": dataset.metadata_path,
                "metadata_source_kind": dataset.metadata_source_kind,
                "metadata_sheet_name": dataset.metadata_sheet_name,
                "metadata_header_row": dataset.metadata_header_row,
                "metadata_skip_rows": dataset.metadata_skip_rows,
                "metadata_available_sheets": dataset.metadata_available_sheets,
                "analysis_axis": analysis_axis,
                "sample_column": dataset.sample_column,
                "target_column": dataset.target_column,
                "group_column": dataset.group_column,
                "task": task,
                "model_family": model_family,
                "split_strategy": split_strategy,
                "test_size": float(test_size),
                "random_seed": int(random_seed),
            },
            "samples": {
                "count": dataset.sample_count,
                "labels": dataset.sample_labels,
            },
            "features": {
                "count": len(dataset.feature_labels),
                "labels": dataset.feature_labels,
            },
            "split": {
                "train_count": int(split["X_train"].shape[0]),
                "test_count": int(split["X_test"].shape[0]),
                "train_labels": split["train_labels"],
                "test_labels": split["test_labels"],
            },
            "metrics": metrics,
            "predictions": prediction_rows,
            "top_features": coefficient_rows,
            "target_distribution": dataset.target_distribution,
            "alignment": {
                "matched_sample_ids": dataset.matched_sample_ids,
                "dropped_sample_ids": dataset.dropped_sample_ids,
                "warnings": dataset.alignment_warnings,
            },
        }

    def evaluate_cluster_agreement(
        self,
        matrix: LoadedMatrix,
        *,
        analysis_axis: AnalysisAxis = "columns",
        cluster_count: int = 3,
        iterations: int = 50,
        train_fraction: float = 0.7,
        metric: str = "correlation",
        linkage: str = "average",
        random_seed: int = 0,
    ) -> dict[str, Any]:
        prepared = self.expression_matrix_backend._observations(matrix, analysis_axis=analysis_axis)
        sample_count = int(prepared.values.shape[0])
        if sample_count < 4:
            raise ProviderError("Held-out cluster agreement requires at least 4 observations.")
        if cluster_count < 2:
            raise ProviderError("Held-out cluster agreement requires cluster_count >= 2.")
        normalized_metric = self.expression_matrix_backend.normalize_clustering_metric(metric)
        train_size = int(round(sample_count * float(train_fraction)))
        train_size = max(cluster_count, min(sample_count - 1, train_size))
        test_size = sample_count - train_size
        if test_size < cluster_count:
            raise ProviderError("Held-out cluster agreement requires at least cluster_count samples in the test split.")
        indices = np.arange(sample_count)
        train_idx, test_idx = train_test_split(indices, train_size=train_size, random_state=int(random_seed), shuffle=True)
        train_values = prepared.values[train_idx]
        test_values = prepared.values[test_idx]
        train_labels = [prepared.labels[index] for index in train_idx]
        test_labels = [prepared.labels[index] for index in test_idx]

        train_matrix = self._matrix_from_observations(matrix, analysis_axis=analysis_axis, values=train_values, labels=train_labels, feature_labels=prepared.feature_labels)
        test_matrix = self._matrix_from_observations(matrix, analysis_axis=analysis_axis, values=test_values, labels=test_labels, feature_labels=prepared.feature_labels)

        train_consensus = self.expression_matrix_backend.run_consensus_clustering(
            train_matrix,
            analysis_axis=analysis_axis,
            cluster_count=cluster_count,
            iterations=iterations,
            sample_fraction=1.0,
            feature_fraction=1.0,
            random_seed=random_seed,
            metric=normalized_metric,
            linkage=linkage,
        )
        test_consensus = self.expression_matrix_backend.run_consensus_clustering(
            test_matrix,
            analysis_axis=analysis_axis,
            cluster_count=cluster_count,
            iterations=iterations,
            sample_fraction=1.0,
            feature_fraction=1.0,
            random_seed=random_seed + 1,
            metric=normalized_metric,
            linkage=linkage,
        )

        train_target = np.asarray([train_consensus["cluster_labels"][label] for label in train_labels], dtype=int)
        classifier = Pipeline(
            [
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=1000)),
            ]
        )
        classifier.fit(train_values, train_target)
        predicted = np.asarray(classifier.predict(test_values), dtype=int)
        independent = np.asarray([test_consensus["cluster_labels"][label] for label in test_labels], dtype=int)
        mapping = self._best_label_alignment(predicted, independent)
        aligned = np.asarray([mapping.get(int(label), int(label)) for label in independent], dtype=int)
        agreement = int(np.sum(predicted == aligned))
        agreement_rate = float(agreement / len(test_labels))
        confusion_labels = self._confusion_labels(
            predicted,
            aligned,
            candidate_labels=np.concatenate([train_target, independent]),
        )
        confusion = confusion_matrix(predicted, aligned, labels=confusion_labels)

        return {
            "summary": (
                f"Evaluated held-out test agreement on {sample_count} observation(s) "
                f"with {train_size} train and {test_size} test sample(s)."
            ),
            "provider": "supervised_expression",
            "analysis": "cluster_agreement",
            "input": {
                "matrix_path": matrix.path,
                "matrix_source_kind": matrix.source_kind,
                "analysis_axis": analysis_axis,
                "cluster_count": int(cluster_count),
                "iterations": int(iterations),
                "train_fraction": float(train_fraction),
                "metric": normalized_metric,
                "requested_metric": clean_text(metric) or normalized_metric,
                "linkage": linkage,
                "random_seed": int(random_seed),
            },
            "evaluation_scope": {
                "kind": "held_out_cluster_agreement",
                "agreement_population": "test_only",
                "split_count": 1,
                "label_transfer_model": "logistic_regression",
                "observation_count": int(sample_count),
                "train_count": int(train_size),
                "test_count": int(test_size),
            },
            "train": {
                "labels": [{"sample": label, "cluster": int(train_consensus["cluster_labels"][label])} for label in train_labels],
                "stability": train_consensus.get("stability", []),
                "consistency_summary": train_consensus.get("consistency_summary"),
            },
            "test": {
                "predicted_labels": [{"sample": label, "cluster": int(predicted[index])} for index, label in enumerate(test_labels)],
                "independent_labels": [{"sample": label, "cluster": int(independent[index])} for index, label in enumerate(test_labels)],
                "aligned_labels": [{"sample": label, "cluster": int(aligned[index])} for index, label in enumerate(test_labels)],
                "stability": test_consensus.get("stability", []),
                "consistency_summary": test_consensus.get("consistency_summary"),
            },
            "agreement": {
                "count": agreement,
                "rate": agreement_rate,
                "count_test_only": agreement,
                "rate_test_only": agreement_rate,
                "test_sample_count": len(test_labels),
                "label_mapping": [{"from_test_cluster": int(key), "to_predicted_cluster": int(value)} for key, value in sorted(mapping.items())],
                "confusion_matrix": confusion.tolist(),
            },
        }

    def _prepare_supervised_dataset(
        self,
        matrix: LoadedMatrix,
        *,
        metadata_path: str,
        metadata_table_text: str,
        metadata_delimiter: str,
        metadata_sheet_name: str,
        metadata_header_row: int,
        metadata_skip_rows: int,
        analysis_axis: AnalysisAxis,
        sample_column: str,
        target_column: str,
        group_column: str,
    ) -> "_PreparedSupervisedDataset":
        prepared = self.expression_matrix_backend._observations(matrix, analysis_axis=analysis_axis)
        metadata = load_table(
            path=metadata_path,
            text=metadata_table_text,
            delimiter=metadata_delimiter,
            sheet_name=metadata_sheet_name,
            header_row=metadata_header_row,
            skip_rows=metadata_skip_rows,
            dtype=str,
            keep_default_na=False,
        )
        resolved_sample_column = self._resolve_column(metadata.frame, sample_column, required=True, label="sample")
        resolved_target_column = self._resolve_column(metadata.frame, target_column, required=True, label="target")
        resolved_group_column = self._resolve_column(metadata.frame, group_column, required=False, label="group")
        frame = metadata.frame.copy()
        frame[resolved_sample_column] = frame[resolved_sample_column].map(clean_text)
        frame[resolved_target_column] = frame[resolved_target_column].map(clean_text)
        if resolved_group_column:
            frame[resolved_group_column] = frame[resolved_group_column].map(clean_text)
        frame = frame[(frame[resolved_sample_column].str.len() > 0) & (frame[resolved_target_column].str.len() > 0)]
        sample_index = {label: index for index, label in enumerate(prepared.labels)}
        requested_samples = [label for label in frame[resolved_sample_column].tolist() if clean_text(label)]
        requested_sample_set = set(requested_samples)
        aligned_samples = [label for label in prepared.labels if label in requested_sample_set]
        if len(aligned_samples) < 3:
            raise ProviderError("Supervised modeling requires at least 3 samples shared between the matrix and metadata.")
        aligned_frame = frame.set_index(resolved_sample_column).loc[aligned_samples].reset_index()
        X = np.asarray([prepared.values[sample_index[label]] for label in aligned_samples], dtype=float)
        y_raw = aligned_frame[resolved_target_column].to_numpy(dtype=object)
        groups = aligned_frame[resolved_group_column].to_numpy(dtype=object) if resolved_group_column else None
        dropped_samples = [label for label in requested_samples if label not in set(aligned_samples)]
        alignment_warnings = []
        if dropped_samples:
            alignment_warnings.append(
                f"Dropped {len(dropped_samples)} metadata sample(s) that did not match matrix labels."
            )
        target_distribution = [
            {"label": str(label), "count": int(count)}
            for label, count in sorted(pd.Series(y_raw).value_counts().items(), key=lambda item: (-item[1], str(item[0])))
        ]
        return _PreparedSupervisedDataset(
            X=X,
            y_raw=y_raw,
            groups=groups,
            sample_labels=aligned_samples,
            feature_labels=list(prepared.feature_labels),
            metadata_path=metadata.path,
            metadata_source_kind=metadata.source_kind,
            metadata_sheet_name=metadata.sheet_name or None,
            metadata_header_row=metadata.header_row,
            metadata_skip_rows=metadata.skip_rows,
            metadata_available_sheets=list(metadata.available_sheets or []),
            sample_column=resolved_sample_column,
            target_column=resolved_target_column,
            group_column=resolved_group_column,
            matched_sample_ids=list(aligned_samples),
            dropped_sample_ids=dropped_samples,
            alignment_warnings=alignment_warnings,
            target_distribution=target_distribution,
        )

    @staticmethod
    def _resolve_column(frame: pd.DataFrame, requested: str, *, required: bool, label: str) -> str:
        token = clean_text(requested)
        if token and token in frame.columns:
            return token
        if required:
            raise ProviderError(f"Supervised expression modeling requires a valid {label} column.")
        return ""

    @staticmethod
    def _validate_model_family(*, task: TaskType, model_family: ModelFamily) -> None:
        if task == "classification" and model_family not in {"logistic_regression", "ridge"}:
            raise ProviderError("Classification supports model_family='logistic_regression' or 'ridge'.")
        if task == "regression" and model_family not in {"linear_regression", "ridge"}:
            raise ProviderError("Regression supports model_family='linear_regression' or 'ridge'.")

    @staticmethod
    def _build_estimator(*, task: TaskType, model_family: ModelFamily):
        if task == "classification":
            if model_family == "ridge":
                model = LogisticRegression(max_iter=1000)
            else:
                model = LogisticRegression(max_iter=1000)
            return Pipeline([("scale", StandardScaler()), ("model", model)])
        if model_family == "ridge":
            model = Ridge(alpha=1.0)
        else:
            model = LinearRegression()
        return Pipeline([("scale", StandardScaler()), ("model", model)])

    def _split_dataset(
        self,
        dataset: "_PreparedSupervisedDataset",
        *,
        task: TaskType,
        split_strategy: SplitStrategy,
        test_size: float,
        random_seed: int,
    ) -> dict[str, Any]:
        y = dataset.y_raw
        if task == "regression":
            try:
                y = np.asarray([float(value) for value in y], dtype=float)
            except (TypeError, ValueError) as exc:
                raise ProviderError("Regression requires a numeric target column.") from exc
        if split_strategy == "group_train_test":
            if dataset.groups is None:
                raise ProviderError("group_train_test requires a group column.")
            splitter = GroupShuffleSplit(n_splits=1, test_size=float(test_size), random_state=int(random_seed))
            train_idx, test_idx = next(splitter.split(dataset.X, y, groups=dataset.groups))
        else:
            stratify = None
            if split_strategy == "stratified_train_test":
                if task != "classification":
                    raise ProviderError("stratified_train_test is only valid for classification.")
                counts = pd.Series(y).value_counts()
                if len(counts) < 2 or int(counts.min()) < 2:
                    raise ProviderError("Stratified splitting requires at least 2 classes with at least 2 samples each.")
                stratify = y
            train_idx, test_idx = train_test_split(
                np.arange(dataset.sample_count),
                test_size=float(test_size),
                random_state=int(random_seed),
                shuffle=True,
                stratify=stratify,
            )
        if len(train_idx) < 2 or len(test_idx) < 1:
            raise ProviderError("Supervised expression modeling requires non-empty train and test splits.")
        return {
            "X_train": dataset.X[train_idx],
            "X_test": dataset.X[test_idx],
            "y_train": y[train_idx],
            "y_test": y[test_idx],
            "train_labels": [dataset.sample_labels[index] for index in train_idx],
            "test_labels": [dataset.sample_labels[index] for index in test_idx],
        }

    @staticmethod
    def _classification_metrics(y_true: np.ndarray, y_pred: np.ndarray, estimator: Pipeline, X_test: np.ndarray) -> dict[str, Any]:
        model = estimator.named_steps.get("model")
        confusion_labels = SupervisedExpressionBackend._confusion_labels(
            y_true,
            y_pred,
            candidate_labels=getattr(model, "classes_", None),
        )
        rows = {
            "accuracy": float(accuracy_score(y_true, y_pred)),
            "balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
            "macro_f1": float(f1_score(y_true, y_pred, average="macro")),
            "weighted_f1": float(f1_score(y_true, y_pred, average="weighted")),
            "confusion_matrix": confusion_matrix(y_true, y_pred, labels=confusion_labels).tolist(),
        }
        if len(np.unique(y_true)) == 2 and hasattr(estimator, "predict_proba"):
            probabilities = estimator.predict_proba(X_test)
            if probabilities.shape[1] == 2:
                try:
                    rows["roc_auc"] = float(roc_auc_score(y_true, probabilities[:, 1]))
                except ValueError:
                    pass
        return rows

    @staticmethod
    def _regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, Any]:
        return {
            "r2": float(r2_score(y_true, y_pred)),
            "mae": float(mean_absolute_error(y_true, y_pred)),
            "rmse": float(math.sqrt(mean_squared_error(y_true, y_pred))),
        }

    @staticmethod
    def _confusion_labels(
        y_true: np.ndarray,
        y_pred: np.ndarray,
        *,
        candidate_labels: Any | None = None,
    ) -> list[Any]:
        labels: list[Any] = []
        seen: set[Any] = set()
        combined: list[Any] = []
        if candidate_labels is not None:
            combined.extend(np.asarray(candidate_labels, dtype=object).tolist())
        combined.extend(np.asarray(y_true, dtype=object).tolist())
        combined.extend(np.asarray(y_pred, dtype=object).tolist())

        for raw_value in combined:
            value = raw_value.item() if isinstance(raw_value, np.generic) else raw_value
            if value in seen:
                continue
            seen.add(value)
            labels.append(value)

        if len(labels) == 1:
            only = labels[0]
            if isinstance(only, (int, float, np.integer, np.floating)):
                extra = only + 1
            else:
                extra = f"{only}__other"
            if extra not in seen:
                labels.append(extra)
        return labels

    @staticmethod
    def _coefficient_rows(
        *,
        estimator: Pipeline,
        feature_labels: list[str],
        task: TaskType,
        top_n: int,
    ) -> list[dict[str, Any]]:
        model = estimator.named_steps["model"]
        coef = getattr(model, "coef_", None)
        if coef is None:
            return []
        if task == "classification" and np.ndim(coef) == 2:
            rows: list[dict[str, Any]] = []
            for class_index in range(coef.shape[0]):
                class_coef = np.asarray(coef[class_index], dtype=float)
                ranking = np.argsort(np.abs(class_coef))[::-1][:top_n]
                for rank, feature_index in enumerate(ranking, start=1):
                    rows.append(
                        {
                            "class_index": int(class_index),
                            "feature": feature_labels[feature_index],
                            "weight": float(class_coef[feature_index]),
                            "absolute_weight": float(abs(class_coef[feature_index])),
                            "rank": int(rank),
                        }
                    )
            return rows
        vector = np.asarray(coef, dtype=float).reshape(-1)
        ranking = np.argsort(np.abs(vector))[::-1][:top_n]
        return [
            {
                "feature": feature_labels[feature_index],
                "weight": float(vector[feature_index]),
                "absolute_weight": float(abs(vector[feature_index])),
                "rank": int(rank),
            }
            for rank, feature_index in enumerate(ranking, start=1)
        ]

    @staticmethod
    def _json_scalar(value: Any) -> Any:
        if isinstance(value, (np.integer, np.floating)):
            return value.item()
        return value

    @staticmethod
    def _best_label_alignment(predicted: np.ndarray, independent: np.ndarray) -> dict[int, int]:
        predicted_labels = sorted(int(value) for value in np.unique(predicted))
        independent_labels = sorted(int(value) for value in np.unique(independent))
        size = max(len(predicted_labels), len(independent_labels))
        cost = np.zeros((size, size), dtype=float)
        pred_index = {label: index for index, label in enumerate(predicted_labels)}
        indep_index = {label: index for index, label in enumerate(independent_labels)}
        for pred, indep in zip(predicted, independent, strict=False):
            cost[pred_index[int(pred)], indep_index[int(indep)]] -= 1.0
        row_idx, col_idx = linear_sum_assignment(cost)
        mapping: dict[int, int] = {}
        for row, col in zip(row_idx, col_idx, strict=False):
            if row < len(predicted_labels) and col < len(independent_labels):
                mapping[independent_labels[col]] = predicted_labels[row]
        return mapping

    @staticmethod
    def _matrix_from_observations(
        matrix: LoadedMatrix,
        *,
        analysis_axis: AnalysisAxis,
        values: np.ndarray,
        labels: list[str],
        feature_labels: list[str],
    ) -> LoadedMatrix:
        if analysis_axis == "columns":
            return LoadedMatrix(
                source_kind=matrix.source_kind,
                path=matrix.path,
                source_text=matrix.source_text,
                delimiter=matrix.delimiter,
                row_labels=list(feature_labels),
                column_labels=list(labels),
                values=np.asarray(values, dtype=float).T,
                row_id_column=matrix.row_id_column,
                analysis_axis_default=analysis_axis,
                sheet_name=matrix.sheet_name,
                available_sheets=matrix.available_sheets,
                header_row=matrix.header_row,
                skip_rows=matrix.skip_rows,
            )
        return LoadedMatrix(
            source_kind=matrix.source_kind,
            path=matrix.path,
            source_text=matrix.source_text,
            delimiter=matrix.delimiter,
            row_labels=list(labels),
            column_labels=list(feature_labels),
            values=np.asarray(values, dtype=float),
            row_id_column=matrix.row_id_column,
            analysis_axis_default=analysis_axis,
            sheet_name=matrix.sheet_name,
            available_sheets=matrix.available_sheets,
            header_row=matrix.header_row,
            skip_rows=matrix.skip_rows,
        )


@dataclass(slots=True)
class _PreparedSupervisedDataset:
    X: np.ndarray
    y_raw: np.ndarray
    groups: np.ndarray | None
    sample_labels: list[str]
    feature_labels: list[str]
    metadata_path: str
    metadata_source_kind: str
    metadata_sheet_name: str | None
    metadata_header_row: int | None
    metadata_skip_rows: int | None
    metadata_available_sheets: list[str]
    sample_column: str
    target_column: str
    group_column: str
    matched_sample_ids: list[str]
    dropped_sample_ids: list[str]
    alignment_warnings: list[str]
    target_distribution: list[dict[str, Any]]

    @property
    def sample_count(self) -> int:
        return int(self.X.shape[0])
