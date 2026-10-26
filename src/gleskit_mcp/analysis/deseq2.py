from __future__ import annotations

import os
import subprocess
from typing import Any, Callable

from gleskit_mcp.analysis.r_runner import RJsonScriptRunner
from gleskit_mcp.config import Settings
from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.utils import clean_text


class DESeq2AnalysisBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.settings = settings
        self.rscript_bin = settings.rscript_bin
        self.command_runner = command_runner or subprocess.run
        self.runner = RJsonScriptRunner(
            settings,
            command_runner=self.command_runner,
            rscript_bin=self.rscript_bin,
        )

    def analyze_counts(
        self,
        *,
        counts_path: str,
        sample_metadata_path: str = "",
        sample_metadata_text: str = "",
        label: str = "",
        comparison: str = "",
        gene_column: str = "",
        sample_column: str = "sample",
        condition_column: str = "condition",
        condition_a: str = "",
        condition_b: str = "",
        design_formula: str = "",
        min_count: int = 10,
        top_n: int = 25,
        padj_threshold: float = 0.05,
        p_value_threshold: float = 0.05,
        base_mean_threshold: float = 0.0,
        significance_mode: str = "padj",
        log2_fold_change_threshold: float = 0.0,
        lfc_shrinkage: str = "none",
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        cleaned_counts_path = clean_text(counts_path)
        if not cleaned_counts_path:
            raise ProviderError("DESeq2 requires a counts file path.")
        normalized_significance_mode = clean_text(significance_mode).lower() or "padj"
        if normalized_significance_mode not in {"padj", "p_value"}:
            raise ProviderError("DESeq2 significance_mode must be 'padj' or 'p_value'.")
        if float(base_mean_threshold) < 0:
            raise ProviderError("DESeq2 base_mean_threshold must be non-negative.")

        configured_timeout = max(300, int(getattr(self.settings, "local_r_timeout_seconds", 45) or 45))
        requested_timeout = max(0, int(timeout_seconds or 0))
        eval_timeout = max(0, int(os.environ.get("GLESKIT_EVAL_TOOL_TIMEOUT_SECONDS", "0") or 0))
        effective_timeout = requested_timeout or eval_timeout or configured_timeout

        payload = self.runner.run(
            "gleskit_deseq2.R",
            {
                "counts_path": cleaned_counts_path,
                "sample_metadata_path": clean_text(sample_metadata_path),
                "sample_metadata_text": sample_metadata_text,
                "label": clean_text(label),
                "comparison": clean_text(comparison),
                "gene_column": clean_text(gene_column),
                "sample_column": clean_text(sample_column) or "sample",
                "condition_column": clean_text(condition_column) or "condition",
                "condition_a": clean_text(condition_a),
                "condition_b": clean_text(condition_b),
                "design_formula": clean_text(design_formula),
                "min_count": max(0, int(min_count)),
                "top_n": max(1, min(int(top_n), 100)),
                "padj_threshold": float(padj_threshold),
                "p_value_threshold": float(p_value_threshold),
                "base_mean_threshold": float(base_mean_threshold),
                "significance_mode": normalized_significance_mode,
                "log2_fold_change_threshold": float(log2_fold_change_threshold),
                "lfc_shrinkage": clean_text(lfc_shrinkage).lower() or "none",
            },
            capability="DESeq2 analysis",
            timeout_seconds=effective_timeout,
        )

        if not isinstance(payload, dict):
            raise ProviderError("DESeq2 analysis returned a non-object response.")
        return payload
