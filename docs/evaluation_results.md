# Read and compare saved evaluations

Use `EvaluationResults` to inspect existing BixBench/RepoBench outputs. These
operations do not call an agent or judge. Install the native evaluation extras
first; replace `run_a` and `run_b` below with IDs from your run catalog.

## Contents

- [Find runs](#find-runs)
- [Load one run](#load-one-run)
- [Compare two runs](#compare-two-runs)
- [Merge batches](#merge-batches)
- [Inspect categories and citations](#inspect-categories-and-citations)
- [Filter scores and estimate cost](#filter-scores-and-estimate-cost)
- [Interpret columns](#interpret-columns)

## Find runs

```python
from gleskit_mcp.eval import EvaluationResults, RunRef

results = EvaluationResults()
results.list_runs()
```

The catalog includes finished and unfinished runs, item/answer counts,
abstentions, judge coverage and missing results.

## Load one run

```python
run = results.load_run("run_a", benchmark="bixbench")
```

Use `benchmark="repobench"` for report runs. Native fields are retained alongside
shared `item_id`, `score_0_4`, `normalized_score`, `score_missing` and `source_run`.

## Compare two runs

```python
comparison = results.compare_runs("run_a", "run_b", benchmark="bixbench")
paired_rows = comparison.paired
run_statistics = comparison.run_statistics
paired_statistics = comparison.paired_statistics
```

Compare the same benchmark and fix the task/question set and judge configuration.

## Merge batches

```python
study = results.merge_runs(["run_a", "run_b"], benchmark="bixbench")
```

`source_run` preserves each row's origin. For an explicit cross-benchmark study:

```python
mixed = results.merge_runs([
    RunRef("bixbench", "bix_run"),
    RunRef("repobench", "report_run"),
])
```

## Inspect categories and citations

```python
diagnostic = results.merge_runs(
    ["run_a", "run_b"], benchmark="bixbench", include_usage=False,
    include_bixbench_metadata=True, include_judge_details=True,
)
by_category = EvaluationResults.explode_bixbench_categories(diagnostic)
```

Optional BixBench metadata includes case IDs, species, task categories, hypotheses
and source papers. Local records do not include human-readable case titles.

## Filter scores and estimate cost

```python
positive = EvaluationResults.scored_rows(study, positive_only=True)
summary = EvaluationResults.score_summary(study, positive_only=True)
priced = EvaluationResults.add_token_costs(run, {
    "input_per_mtok": 0.077,
    "output_per_mtok": 0.154,
    "cache_read_per_mtok": 0.0154,
})
```

Prices above are illustrative inputs, not current provider prices. Supply the
rates appropriate to the run. BixBench reads saved usage when available.

## Interpret columns

- Committee scores use the 0–4 ladder and its 0–1 normalization. Missing verdicts
  stay missing; they are not silently converted to zero.
- `positive_only=True` also excludes actual judge-zero scores. State that filter
  when reporting statistics.
- Rejected items are explicit abstentions; unanswered counts include failures
  or stopped runs without a final answer.
- See [BixBench scoring](bixbench_evaluation.md#score-semantics) and
  [RepoBench scoring](repobench_evaluation.md#score-semantics) for inclusion rules.

The separate research checkout's
`notebooks/eval/ncs_manuscript_figures_and_tables.ipynb` generates the paper
figures/tables from saved runs. It is not bundled in the product source export.
