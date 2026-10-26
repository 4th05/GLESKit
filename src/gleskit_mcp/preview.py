from __future__ import annotations

from pathlib import Path
from typing import Any, Literal


PreviewSourceKind = Literal["upload", "artifact"]

DEFAULT_PREVIEW_LINE_CHARS = 1000
DEFAULT_PREVIEW_MAX_BYTES = 8192


def _truncate_text(value: str, *, limit: int) -> str:
    text = str(value or "")
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def read_preview_lines(
    path: Path,
    *,
    max_lines: int,
    max_bytes: int = DEFAULT_PREVIEW_MAX_BYTES,
    max_line_chars: int = DEFAULT_PREVIEW_LINE_CHARS,
) -> list[str]:
    resolved_max_lines = max(1, int(max_lines))
    resolved_max_bytes = max(1, int(max_bytes))
    resolved_max_line_chars = max(1, int(max_line_chars))

    lines: list[str] = []
    remaining_bytes = resolved_max_bytes
    with path.open("rb") as handle:
        for raw_line in handle:
            if remaining_bytes <= 0 or len(lines) >= resolved_max_lines:
                break
            chunk = raw_line[:remaining_bytes]
            if b"\0" in chunk:
                return []
            remaining_bytes -= len(chunk)
            text = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
            lines.append(_truncate_text(text, limit=resolved_max_line_chars))
            if len(raw_line) > len(chunk):
                break
    return lines


def _sanitize_value(value: Any, *, max_lines: int, max_line_chars: int) -> tuple[Any, bool]:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            sanitized, child_truncated = _sanitize_value(item, max_lines=max_lines, max_line_chars=max_line_chars)
            cleaned[str(key)] = sanitized
            truncated = truncated or child_truncated
        return cleaned, truncated

    if isinstance(value, list):
        cleaned_list: list[Any] = []
        truncated = len(value) > max_lines
        for item in value[:max_lines]:
            sanitized, child_truncated = _sanitize_value(item, max_lines=max_lines, max_line_chars=max_line_chars)
            cleaned_list.append(sanitized)
            truncated = truncated or child_truncated
        return cleaned_list, truncated

    if isinstance(value, tuple):
        cleaned_tuple: list[Any] = []
        truncated = len(value) > max_lines
        for item in value[:max_lines]:
            sanitized, child_truncated = _sanitize_value(item, max_lines=max_lines, max_line_chars=max_line_chars)
            cleaned_tuple.append(sanitized)
            truncated = truncated or child_truncated
        return cleaned_tuple, truncated

    if isinstance(value, str):
        cleaned = _truncate_text(value, limit=max_line_chars)
        return cleaned, cleaned != value

    if isinstance(value, Path):
        text = str(value)
        cleaned = _truncate_text(text, limit=max_line_chars)
        return cleaned, cleaned != text

    return value, False


def preview_model_response(
    payload: dict[str, Any] | Any,
    *,
    source_kind: PreviewSourceKind,
    max_lines: int,
    max_line_chars: int = DEFAULT_PREVIEW_LINE_CHARS,
) -> Any:
    resolved_max_lines = max(1, int(max_lines))
    resolved_max_line_chars = max(1, int(max_line_chars))
    sanitized, truncated = _sanitize_value(payload, max_lines=resolved_max_lines, max_line_chars=resolved_max_line_chars)
    if isinstance(sanitized, dict):
        if truncated:
            sanitized = dict(sanitized)
            sanitized["preview"] = {
                "source_kind": source_kind,
                "max_lines": resolved_max_lines,
                "max_line_chars": resolved_max_line_chars,
                "truncated": True,
            }
        return sanitized
    return sanitized
