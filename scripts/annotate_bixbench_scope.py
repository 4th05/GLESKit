#!/usr/bin/env python3
"""Annotate BixBench rows with GLESKit scope and numeric flags.

`scope=1` means the question is answerable within GLESKit's intended gene-level
analysis surface for bulk RNA-seq or single-cell RNA-seq. This includes
expression-matrix statistics, differential expression, enrichment, clustering,
supervised modeling on expression matrices, and gene-centric reporting.

`scope=0` means the row depends on capabilities outside that scope, such as
variant analysis, WGS/phylogenomics, imaging, proteomics-first workflows,
epigenomics-first workflows, or non-RNA clinical modeling.

`numeric=1` means the expected answer is primarily quantitative, even if the
question is phrased semantically. This includes ranges, scientific notation,
fold changes, percentages, and similar answer forms.

`numeric=0` means the answer is mostly semantic / categorical / open-text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gleskit_mcp.eval.bixbench import numeric_for_row


DEFAULT_INPUT = Path("var/bixbench/hf_repo/BixBench.jsonl")
DEFAULT_OUTPUT = Path("var/bixbench/hf_repo/BixBench.gleskit_scope.jsonl")

# Entire benchmark cases that fit GLESKit's bulk/scRNA gene-expression scope.
IN_SCOPE_SHORT_IDS = {
    "bix-1",
    "bix-3",
    "bix-5",
    "bix-9",
    "bix-13",
    "bix-22",
    "bix-24",
    "bix-26",
    "bix-27",
    "bix-30",
    "bix-31",
    "bix-32",
    "bix-33",
    "bix-36",
    "bix-43",
    "bix-46",
    "bix-49",
    "bix-53",
}

# Question-level inclusions for otherwise out-of-scope cases. These rows only
# require expression-matrix QC/statistics and do not depend on the paired
# non-RNA modality used elsewhere in the same benchmark case.
IN_SCOPE_QUESTION_IDS = {
    "bix-16-q2",
    "bix-42-q2",
}


def scope_for_row(row: dict[str, object]) -> int:
    question_id = str(row.get("question_id", ""))
    short_id = str(row.get("short_id", ""))
    if question_id in IN_SCOPE_QUESTION_IDS or short_id in IN_SCOPE_SHORT_IDS:
        return 1
    return 0


def annotate_dataset(input_path: Path, output_path: Path) -> tuple[int, int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    in_scope = 0
    numeric = 0
    with input_path.open("r", encoding="utf-8") as src, output_path.open(
        "w", encoding="utf-8"
    ) as dst:
        for line in src:
            row = json.loads(line)
            scope = scope_for_row(row)
            numeric_flag = numeric_for_row(row)
            row["scope"] = scope
            row["numeric"] = numeric_flag
            dst.write(json.dumps(row) + "\n")
            total += 1
            in_scope += scope
            numeric += numeric_flag
    return total, in_scope, numeric


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a BixBench JSONL copy with a GLESKit scope column."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Source JSONL file. Defaults to {DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Destination JSONL file. Defaults to {DEFAULT_OUTPUT}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    total, in_scope, numeric = annotate_dataset(args.input, args.output)
    print(
        json.dumps(
            {
                "input": str(args.input),
                "output": str(args.output),
                "total_rows": total,
                "in_scope_rows": in_scope,
                "numeric_rows": numeric,
                "out_of_scope_rows": total - in_scope,
                "non_numeric_rows": total - numeric,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
