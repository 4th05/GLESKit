# Run RepoBench evaluations

Use a native development environment for these commands; the Docker web-app
launcher does not run benchmark jobs. Solve tasks first, then judge their saved
reports independently.

## Contents

- [Prepare the environment](#prerequisites)
- [Configure standalone OpenCode](#one-time-setup)
- [Generate a report](#solve-phase)
- [Resume, append or retry](#continuation-and-recovery-patterns)
- [Judge saved reports](#judge-phase)
- [Find outputs](#output-layout)
- [Interpret scores](#score-semantics)
- [Keep evaluation inputs isolated](#evaluation-isolation)
- [Compare models](#recommended-workflow)

## Prerequisites

1. Create `.venv` with Python 3.11+ and install evaluation extras:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -e '.[mcp,eval]'
   ```

2. Install standalone OpenCode and the host R packages needed for the task; see
   the [runtime baseline](runtime-baseline.md).
3. Configure the solve provider in OpenCode. For `openai/<model>`, use native
   OpenCode OAuth. The solve process strips direct OpenAI API environment
   variables; do not source `OPENAI_API_KEY` for those solves.
4. For direct-API judging, provide `OPENAI_API_KEY` or `OPENROUTER_API_KEY` in the
   environment or ignored project `.env`. Keep credentials out of Git.

## One-time setup

Back up existing global OpenCode configuration before replacing managed links;
see [what setup changes](opencode/README.md).

```sh
make opencode_setup
```

Get the full option lists without calling a model:

```sh
PYTHONPATH=src .venv/bin/python scripts/eval/run_repobench.py --help
PYTHONPATH=src .venv/bin/python scripts/eval/judge_repobench.py --help
```

## Solve Phase

Generate one report in a named run. Model IDs are examples; check availability
and access with the configured provider.

```sh
make eval_repobench REPOBENCH_RUN_ID=report_smoke REPOBENCH_TASK_ID=repo-1-q1 REPOBENCH_MODEL=openai/gpt-5.4-mini
```

`repo-1-q1` and `biomarker-epithelial_tumor_panel` identify the same task.
Both compact public IDs and descriptive slugs are accepted.

| To do this | Set these Make variables |
| --- | --- |
| Generate two mechanism reports | `REPOBENCH_REPORT_TYPE=mechanism REPOBENCH_LIMIT=2` |
| Fix the task set | `REPOBENCH_TASK_IDS="biomarker-epithelial_tumor_panel mechanism-emt_hypoxia_program"` |
| Select a report family | `REPOBENCH_REPORT_TYPE=biomarker`, `mechanism` or `disease_context` |
| Sample reproducibly | `REPOBENCH_RANDOM_COUNT=1 REPOBENCH_RANDOM_SEED=11` |
| Run a skills ablation | `REPOBENCH_SKILLS_MODE=off` with a distinct run ID |
| Limit a task/tool | `REPOBENCH_TIMEOUT_SECONDS=900 REPOBENCH_OPENCODE_TOOL_TIMEOUT_MS=300000` |
| Stop loops sooner | `REPOBENCH_MAX_TURNS`, `REPOBENCH_MAX_TOOL_CALLS`, `REPOBENCH_NO_PROGRESS_CYCLES` |
| Plan without launching OpenCode | `REPOBENCH_DRY_RUN=1` |

Agent overrides, polling, retries and verbosity are listed by `--help`.

## Continuation And Recovery Patterns

Continue the named run without repeating completed episodes:

```sh
make eval_repobench REPOBENCH_RUN_ID=report_smoke REPOBENCH_CONTINUE_MODE=resume
```

| Mode | Use it to |
| --- | --- |
| `fresh` | Create a new run; fail if the ID exists |
| `resume` | Continue the same planned matrix, skipping completed episodes |
| `append` | Add newly selected tasks to the existing run |
| `retry_failed` | Retry only previously failed episodes |

To add a reproducible sample:

```sh
make eval_repobench REPOBENCH_RUN_ID=report_smoke REPOBENCH_CONTINUE_MODE=append REPOBENCH_SCOPE=1 REPOBENCH_RANDOM_COUNT=1 REPOBENCH_RANDOM_SEED=11
```

## Judge Phase

Judge an existing saved run:

```sh
make judge_repobench REPOBENCH_RUN_ROOT=var/repobench/runs/report_smoke REPOBENCH_JUDGE_PROVIDER=openrouter REPOBENCH_JUDGE_MODEL=deepseek/deepseek-v4-flash
```

| To do this | Set these Make variables |
| --- | --- |
| Use OpenAI instead | `REPOBENCH_JUDGE_PROVIDER=openai REPOBENCH_JUDGE_MODEL=gpt-4.1-mini` |
| Judge one task | `REPOBENCH_TASK_ID=repo-1-q1` |
| Judge one report family | `REPOBENCH_JUDGE_REPORT_TYPE=mechanism` |
| Limit the batch | `REPOBENCH_JUDGE_LIMIT=2` |
| Skip existing `judge.json` files | `REPOBENCH_JUDGE_RESUME=1` |
| Judge all staged tasks | `REPOBENCH_JUDGE_SCOPE=all` |
| Override a specialist | `REPOBENCH_JUDGE_CONTENT_MODEL`, `REPOBENCH_JUDGE_EVIDENCE_MODEL`, `REPOBENCH_JUDGE_TEMPLATE_MODEL`, `REPOBENCH_JUDGE_PROVENANCE_MODEL`, `REPOBENCH_JUDGE_ARBITER_MODEL` |

Use the judge's `--help` for custom base URLs and committee timeout.

## Output Layout

Look under `var/repobench/runs/<run_id>/`:

| File | Use it to |
| --- | --- |
| `portal/index.html`, `summary/report.html` | Review the run |
| `summary/tasks.json` / `.csv` | Analyze task-level results |
| `summary/judge/progress.json` | Monitor committee progress |
| `run_manifest.json`, `planned_matrix.json` | Reproduce configuration and task selection |
| `episodes/<task_id>/episode.json` | Inspect the raw episode and report SHA-256 capture fingerprint |
| `episodes/<task_id>/report.json`, `report.md` | Read the captured report |
| `episodes/<task_id>/deterministic_metrics.json` | Inspect workflow metrics |
| `episodes/<task_id>/judge_progress.json`, `judge.json` | Inspect committee progress, scores, provenance audit and capture verification |
| `summary/human_audit/` | Review blinded report copies and the non-scoring citation worksheet |

Use the [results API](evaluation_results.md) to compare saved runs.

## Score Semantics

Keep deterministic workflow/report metrics separate from committee judgment.
Deterministic metrics inspect saved-report presence, template use, visible
provenance and expected-nugget overlap. Grounding must appear in the report itself,
ideally in section-local `Sources:` lines; a tool trace alone is insufficient.

The committee assesses biological usefulness, evidence, template compliance and
auditable provenance. It computes a weighted 0–4 mean, applies deterministic caps,
then assigns a category by midpoint:

| Category | Ordinal level | Final mean |
| --- | --- | --- |
| `unsupported` | 0 | `< 0.5` |
| `weak` | 1 | `0.5` to `< 1.5` |
| `partial` | 2 | `1.5` to `< 2.5` |
| `mostly_supported` | 3 | `2.5` to `< 3.5` |
| `accept` | 4 | `>= 3.5` |

Use `committee_level_mean_final` for the final 0–4 mean and
`committee_level_mean_normalized` for the 0–1 normalization. `judge_score` remains
the legacy normalized alias.

The offline provenance audit compares saved report text with saved artifacts:
URLs, PMIDs, DOIs and paper titles. It needs no internet lookup. Major identity
faults, including title/link mismatches and missing artifacts, trigger review and
can cap the final level. Dashboard quality means include only episodes with a
saved report artifact; broken episodes remain listed but are excluded.

## Evaluation Isolation

The solving agent receives the task, kickoff context, staged inputs, report
requirements and normal tool contract. Evaluator-only `expected_content`, nuggets
and forbidden-overclaim criteria are not included in its prompts, session
description or attempt inputs.

After judging, `summary/human_audit/` provides blinded copies and a citation
worksheet. It does not rerun the agent, change saved reports or alter scores.

## Recommended Workflow

1. Generate a small batch and inspect the portal or summary report.
2. Run the judge separately and check its outputs.
3. For comparisons, fix task IDs, report-type filter and judge provider/model;
   use a distinct run ID per solve model.
4. Compare completed-report episodes only, then scale up.
