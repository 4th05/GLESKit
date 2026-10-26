from __future__ import annotations

from typing import Any, Iterable


STANDARD_JUDGE_SCORE_BANDS = (
    {
        "key": "accept",
        "min": 0.85,
        "max": 1.0,
        "range_label": "0.85-1.00",
        "label": "Accept",
        "description": "Directly supported, high-quality output with only minor caveats.",
    },
    {
        "key": "mostly_supported",
        "min": 0.70,
        "max": 0.849,
        "range_label": "0.70-0.84",
        "label": "Mostly Supported",
        "description": "Scientifically solid output with meaningful support, but still carrying notable caveats or omissions.",
    },
    {
        "key": "partial",
        "min": 0.50,
        "max": 0.699,
        "range_label": "0.50-0.69",
        "label": "Partial",
        "description": "Partially successful output with mixed quality and unresolved weaknesses.",
    },
    {
        "key": "weak",
        "min": 0.30,
        "max": 0.499,
        "range_label": "0.30-0.49",
        "label": "Weak",
        "description": "Weak output with substantial problems in support, quality, or interpretation.",
    },
    {
        "key": "unsupported",
        "min": 0.0,
        "max": 0.299,
        "range_label": "0.00-0.29",
        "label": "Unsupported",
        "description": "Unsupported or failed output that does not meet the expected quality bar.",
    },
)

JUDGE_CATEGORY_LADDER = (
    {
        "key": "unsupported",
        "level": 0,
        "label": "Unsupported",
        "description": "Not supported by the visible evidence, materially wrong, or functionally failed.",
        "rubric": "Use when the answer or report is unsupported, contradicted, or missing the core requested content.",
    },
    {
        "key": "weak",
        "level": 1,
        "label": "Weak",
        "description": "Shows limited progress, but substantial flaws keep it below a minimally reliable result.",
        "rubric": "Use when there is some signal of effort or partial correctness, but major quality, support, or interpretation problems remain.",
    },
    {
        "key": "partial",
        "level": 2,
        "label": "Partial",
        "description": "Partially successful with meaningful support, but important weaknesses or omissions remain.",
        "rubric": "Use when the result addresses the task in part, but caveats, missing support, or incomplete fulfillment prevent a strong judgment.",
    },
    {
        "key": "mostly_supported",
        "level": 3,
        "label": "Mostly Supported",
        "description": "Substantively strong and well supported, but still carrying notable caveats or omissions.",
        "rubric": "Use when the result is broadly correct and useful, with only non-fatal weaknesses.",
    },
    {
        "key": "accept",
        "level": 4,
        "label": "Accept",
        "description": "Directly supported, high-quality output with only minor caveats.",
        "rubric": "Use when the result fully answers the task with strong support and no meaningful defects.",
    },
)

_CATEGORY_BY_KEY = {
    str(item["key"]): item
    for item in JUDGE_CATEGORY_LADDER
}

_CATEGORY_BY_LEVEL = {
    int(item["level"]): item
    for item in JUDGE_CATEGORY_LADDER
}


def judge_score_band(score: float | None) -> dict[str, Any] | None:
    if score is None:
        return None
    value = max(0.0, min(1.0, float(score)))
    for band in STANDARD_JUDGE_SCORE_BANDS:
        if value >= float(band["min"]):
            return dict(band)
    return dict(STANDARD_JUDGE_SCORE_BANDS[-1])


def judge_band_kind(band_key: Any) -> str:
    normalized = str(band_key or "").strip().lower()
    if normalized in {"accept", "mostly_supported"}:
        return "good"
    if normalized in {"partial", "weak"}:
        return "warn"
    if normalized in {"unsupported", "fail", "error"}:
        return "bad"
    return "neutral"


def judge_category(category_key: Any) -> dict[str, Any] | None:
    normalized = str(category_key or "").strip().lower()
    item = _CATEGORY_BY_KEY.get(normalized)
    return dict(item) if item is not None else None


def judge_category_level(category_key: Any) -> int | None:
    item = judge_category(category_key)
    if item is None:
        return None
    return int(item["level"])


def judge_level_category(level: float | int | None) -> dict[str, Any] | None:
    if level is None:
        return None
    bounded = max(0, min(4, int(round(float(level)))))
    item = _CATEGORY_BY_LEVEL.get(bounded)
    return dict(item) if item is not None else None


def judge_mean_level_category(level_mean: float | None) -> dict[str, Any] | None:
    if level_mean is None:
        return None
    value = max(0.0, min(4.0, float(level_mean)))
    if value < 0.5:
        return judge_category("unsupported")
    if value < 1.5:
        return judge_category("weak")
    if value < 2.5:
        return judge_category("partial")
    if value < 3.5:
        return judge_category("mostly_supported")
    return judge_category("accept")


def judge_level_mean_normalized(level_mean: float | None) -> float | None:
    if level_mean is None:
        return None
    # This is an analytical score, not a display value. Preserve the full
    # weighted committee result for downstream paired analyses.
    return max(0.0, min(4.0, float(level_mean))) / 4.0


def apply_category_cap(category_key: Any, cap_key: Any) -> dict[str, Any] | None:
    category_item = judge_category(category_key)
    cap_item = judge_category(cap_key)
    if category_item is None and cap_item is None:
        return None
    if category_item is None:
        return cap_item
    if cap_item is None:
        return category_item
    if int(cap_item["level"]) <= int(category_item["level"]):
        return cap_item
    return category_item


def judge_category_disagreement_span(category_keys: Iterable[Any]) -> int | None:
    levels = [
        level
        for level in (judge_category_level(item) for item in category_keys)
        if level is not None
    ]
    if not levels:
        return None
    return max(levels) - min(levels)
