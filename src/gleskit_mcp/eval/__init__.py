"""Evaluation framework for controlled report-generation benchmarks."""

from gleskit_mcp.eval.config import (
    EvalCase,
    EvalRunConfig,
    EvalWorkflowStep,
    ReportTypeSpec,
    expand_episode_matrix,
    load_report_type_specs,
    load_run_config,
)
from gleskit_mcp.eval.results import EvaluationResults, RunComparison, RunRef

__all__ = [
    "EvalCase",
    "EvalRunConfig",
    "EvalWorkflowStep",
    "ReportTypeSpec",
    "expand_episode_matrix",
    "load_report_type_specs",
    "load_run_config",
    "EvaluationResults",
    "RunComparison",
    "RunRef",
]
