# Run BixBench evaluations

Run these commands in a native development environment. The Docker web-app
launcher does not run benchmark jobs. Solving and judging are separate: rejudge
saved answers without paying to solve them again.

## Contents

- [Prepare the environment](#prerequisites)
- [Configure standalone OpenCode](#one-time-setup)
- [Solve a small batch](#solve-phase)
- [Resume, append or retry](#continuation-and-recovery-patterns)
- [Judge saved answers](#judge-phase)
- [Find outputs](#output-layout)
- [Interpret scores](#score-semantics)
- [Compare models](#recommended-workflow)
- [Handle provider failures](#provider-notes)

## Prerequisites

1. Create `.venv` with Python 3.11+ and install the evaluation extras:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -e '.[mcp,eval]'
   ```

2. Install OpenCode and the host R packages needed for the task; see the
   [runtime baseline](runtime-baseline.md).
3. Provide the BixBench dataset under `var/bixbench/`.
4. Configure the solve provider in standalone OpenCode. For `openai/<model>`,
   use OpenCode's native OAuth login. Solve processes strip direct OpenAI API
   environment variables; do not source `OPENAI_API_KEY` for those solves.
5. For direct-API judging, provide `OPENAI_API_KEY` or `OPENROUTER_API_KEY` through
   the environment or ignored project `.env`. Never commit keys.

## One-time setup

Back up any existing global OpenCode configuration first: setup replaces managed
links. See [what setup changes](opencode/README.md).

```sh
make opencode_setup
```

Get all available flags without starting a model call:

```sh
PYTHONPATH=src .venv/bin/python scripts/eval/run_bixbench.py --help
PYTHONPATH=src .venv/bin/python scripts/eval/judge_bixbench.py --help
```

## Solve Phase

Start with a named small batch; model IDs below are examples, not account-access
or availability guarantees:

```sh
make eval_bixbench BIXBENCH_RUN_ID=bix_smoke BIXBENCH_SCOPE=1 BIXBENCH_LIMIT=4 BIXBENCH_MODEL=openai/gpt-5.4-mini
```

| To do this | Set these Make variables |
| --- | --- |
| Run one question | `BIXBENCH_QUESTION_ID=bix-36-q3` |
| Fix the compared question set | `BIXBENCH_QUESTION_IDS="bix-13-q2 bix-22-q2 bix-36-q3"` |
| Sample reproducibly | `BIXBENCH_RANDOM_COUNT=2 BIXBENCH_RANDOM_SEED=7` |
| Disable skills for an ablation | `BIXBENCH_SKILLS_MODE=off` with a distinct run ID |
| Limit a slow question/tool | `BIXBENCH_TIMEOUT_SECONDS=600 BIXBENCH_OPENCODE_TOOL_TIMEOUT_MS=300000` |
| Stop loops sooner | `BIXBENCH_MAX_TURNS`, `BIXBENCH_MAX_TOOL_CALLS`, `BIXBENCH_NO_PROGRESS_CYCLES` |
| Plan without launching OpenCode | `BIXBENCH_DRY_RUN=1` |

Use `--help` for agent overrides, polling, retries and verbosity.

## Continuation And Recovery Patterns

Keep the original run ID when continuing:

```sh
make eval_bixbench BIXBENCH_RUN_ID=bix_smoke BIXBENCH_CONTINUE_MODE=resume
```

| Mode | Use it to |
| --- | --- |
| `fresh` | Create a new run; fail if the ID already exists |
| `resume` | Keep the planned matrix and skip completed episodes |
| `append` | Add newly selected rows to the same run |
| `retry_failed` | Retry only previously failed episodes |

For example, append another reproducible sample:

```sh
make eval_bixbench BIXBENCH_RUN_ID=bix_smoke BIXBENCH_CONTINUE_MODE=append BIXBENCH_SCOPE=1 BIXBENCH_RANDOM_COUNT=2 BIXBENCH_RANDOM_SEED=7
```

## Judge Phase

Judge the saved run with a separately selected API model:

```sh
make judge_bixbench BIXBENCH_RUN_ROOT=var/bixbench/runs/bix_smoke BIXBENCH_JUDGE_PROVIDER=openrouter BIXBENCH_JUDGE_MODEL=deepseek/deepseek-v4-flash
```

| To do this | Set these Make variables |
| --- | --- |
| Use OpenAI instead | `BIXBENCH_JUDGE_PROVIDER=openai BIXBENCH_JUDGE_MODEL=gpt-4.1-mini` |
| Judge selected questions | `BIXBENCH_QUESTION_IDS="bix-22-q2 bix-36-q3"` |
| Limit the judged batch | `BIXBENCH_JUDGE_LIMIT=2` |
| Skip existing `judge.json` files | `BIXBENCH_JUDGE_RESUME=1` |
| Judge all staged rows | `BIXBENCH_JUDGE_SCOPE=all` |
| Override a specialist | `BIXBENCH_JUDGE_STATS_MODEL`, `BIXBENCH_JUDGE_BIOLOGY_MODEL`, `BIXBENCH_JUDGE_EVIDENCE_MODEL`, `BIXBENCH_JUDGE_ARBITER_MODEL` |

Base-URL and committee-timeout options are listed by the judge's `--help`.

## Output Layout

Look under `var/bixbench/runs/<run_id>/`:

| File | Use it to |
| --- | --- |
| `portal/index.html` | Review the run |
| `summary/report.html` | Inspect solve results |
| `summary/judge/report.html` | Inspect committee results |
| `summary/questions.json` / `.csv` | Analyze question-level results |
| `summary/judge/progress.json` | Monitor committee progress |
| `run_manifest.json`, `planned_matrix.json` | Reproduce the run configuration and selection |
| `episodes/<question_id>/episode.json` | Inspect the canonical raw episode |
| `episodes/<question_id>/judge_progress.json`, `judge.json` | Inspect one committee evaluation |

Load saved runs programmatically with the [results API](evaluation_results.md).

## Score Semantics

Keep the **official BixBench binary score** separate from committee judgment.
The committee checks tool support, statistical/biological reasoning and grounding.
It computes a weighted mean on a 0–4 ladder, applies deterministic caps, then
assigns the category by midpoint:

| Category | Ordinal level | Final mean |
| --- | --- | --- |
| `unsupported` | 0 | `< 0.5` |
| `weak` | 1 | `0.5` to `< 1.5` |
| `partial` | 2 | `1.5` to `< 2.5` |
| `mostly_supported` | 3 | `2.5` to `< 3.5` |
| `accept` | 4 | `>= 3.5` |

Use `committee_level_mean_final` for the capped 0–4 mean and
`committee_level_mean_normalized` for its 0–1 normalization. `judge_score` is the
legacy normalized alias. These do not replace the official binary score.

Dashboard aggregates include only episodes with a nonempty final answer.
Abstentions and broken episodes remain listed but are excluded from answered-only
statistics. State the inclusion rule when reporting comparisons.

## Recommended Workflow

1. Solve a small batch and inspect `portal/index.html`.
2. Judge it separately and check committee outputs.
3. For model comparisons, fix question IDs and judge provider/model; use a distinct
   run ID per solve model.
4. Compare answered-only episodes, then scale up.

## Provider Notes

Solve models are routed through OpenCode; judge models use direct APIs selected
by `BIXBENCH_JUDGE_PROVIDER`. A judge setting does not change the solve provider.
If routing fails, fix the standalone OpenCode provider/model configuration first.

Recorded runs found the full committee prompt unstable with
`nvidia/nemotron-3-ultra-550b-a55b:free` and more stable with
`deepseek/deepseek-v4-flash`. These are historical observations, not guarantees of
current service availability. Test a small batch before committing a large budget.
