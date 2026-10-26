from __future__ import annotations

from dataclasses import dataclass
import csv
from io import StringIO
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterator
import zipfile

import pandas as pd

from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.utils import clean_text


def _raise_csv_field_size_limit() -> None:
    """Allow Python's CSV parser to consume wide matrix cells safely."""
    limit = sys.maxsize
    while limit > 131072:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


_raise_csv_field_size_limit()


_OLE_COMPOUND_DOCUMENT_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def is_excel_workbook(path: str | Path) -> bool:
    """Return whether file content, rather than its suffix, is an Excel workbook.

    BixBench capsules occasionally use an ``.xls`` suffix for a delimited text
    matrix.  Treating every such suffix as a workbook sends those files through
    an Excel reader before any header inspection can happen.  Legacy ``.xls``
    workbooks have the OLE compound-document signature; ``.xlsx`` workbooks are
    ZIP archives containing the Excel ``xl/`` package.
    """
    candidate = Path(path).expanduser()
    try:
        with candidate.open("rb") as handle:
            signature = handle.read(8)
    except OSError:
        return False
    if signature == _OLE_COMPOUND_DOCUMENT_MAGIC:
        return True
    if not signature.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(candidate) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return "[Content_Types].xml" in names and any(name.startswith("xl/") for name in names)


def excel_workbook_engine(path: str | Path) -> str | None:
    """Select the pandas engine from workbook bytes after format validation."""
    candidate = Path(path).expanduser()
    try:
        with candidate.open("rb") as handle:
            signature = handle.read(8)
    except OSError:
        return None
    return "openpyxl" if signature.startswith(b"PK") else None


@dataclass(slots=True)
class LoadedTable:
    source_kind: str
    path: str
    delimiter: str
    text: str
    frame: pd.DataFrame
    file_format: str
    sheet_name: str | None = None
    available_sheets: list[str] | None = None
    header_row: int | None = None
    skip_rows: int | None = None


def infer_delimiter(text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    counts = {
        "\t": first_line.count("\t"),
        ",": first_line.count(","),
        ";": first_line.count(";"),
    }
    if any(counts.values()):
        return max(counts, key=counts.get)
    # Some featureCounts exports use runs of spaces rather than a formal TSV
    # delimiter. Keep this marker internal and normalize the loaded table to
    # TSV before handing it to downstream CSV consumers.
    if len(re.findall(r"\S+", first_line)) > 1:
        return " "
    return "\t"


def _parse_delimiter(delimiter: str) -> str:
    return r"\s+" if delimiter == " " else delimiter


def _output_delimiter(delimiter: str) -> str:
    return "\t" if delimiter == " " else delimiter


def load_table(
    *,
    path: str = "",
    text: str = "",
    delimiter: str = "",
    sheet_name: str = "",
    header_row: int = 1,
    skip_rows: int = 0,
    max_rows: int = 0,
    dtype: str | type[Any] | None = str,
    keep_default_na: bool = False,
) -> LoadedTable:
    cleaned_path = clean_text(path)
    inline_text = str(text or "")
    cleaned_sheet_name = clean_text(sheet_name)
    normalized_header_row = max(1, int(header_row or 1))
    normalized_skip_rows = max(0, int(skip_rows or 0))
    normalized_max_rows = max(0, int(max_rows or 0))

    if cleaned_path:
        resolved = Path(cleaned_path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"Table file not found: {resolved}")
        if is_excel_workbook(resolved):
            try:
                workbook = pd.ExcelFile(resolved, engine=excel_workbook_engine(resolved))
            except ImportError as exc:
                raise ProviderError(
                    "Excel input requires 'openpyxl' for .xlsx files or 'xlrd' for .xls files."
                ) from exc
            try:
                available_sheets = list(workbook.sheet_names)
                selected_sheet = cleaned_sheet_name or (available_sheets[0] if available_sheets else "")
                if not selected_sheet:
                    raise ProviderError("Excel workbook does not contain any readable sheet.")
                if selected_sheet not in available_sheets:
                    raise ProviderError(
                        f"Unable to load Excel sheet '{selected_sheet}'. Available sheets: {', '.join(available_sheets) or 'none'}."
                    )
                try:
                    raw_frame = pd.read_excel(
                        workbook,
                        sheet_name=selected_sheet,
                        header=None,
                        dtype=dtype,
                        keep_default_na=keep_default_na,
                        nrows=normalized_max_rows or None,
                    )
                except ValueError as exc:
                    raise ProviderError(f"Unable to load Excel sheet '{selected_sheet}': {exc}") from exc
                header_index = normalized_header_row - 1
                if header_index >= len(raw_frame.index):
                    raise ProviderError(
                        f"Requested header_row={normalized_header_row} is outside the table range ({len(raw_frame.index)} row(s))."
                    )
                raw_frame = raw_frame.iloc[header_index:].reset_index(drop=True)
                normalized = _extract_frame_from_rows(
                    raw_frame,
                    header_row=1,
                    skip_rows=0,
                )
                return LoadedTable(
                    source_kind="file",
                    path=str(resolved.resolve()),
                    delimiter=delimiter or "\t",
                    text=normalized.to_csv(sep="\t", index=False),
                    frame=normalized,
                    file_format="excel",
                    sheet_name=selected_sheet or None,
                    available_sheets=available_sheets,
                    header_row=normalized_header_row,
                    skip_rows=normalized_skip_rows,
                )
            finally:
                workbook.close()

        if normalized_max_rows > 0:
            with resolved.open("r", encoding="utf-8", errors="ignore") as handle:
                sample_text = handle.read(65536)
            effective_delimiter = delimiter or infer_delimiter(sample_text)
            parse_delimiter = _parse_delimiter(effective_delimiter)
            output_delimiter = _output_delimiter(effective_delimiter)
            try:
                raw_frame = pd.read_csv(
                    resolved,
                    sep=parse_delimiter,
                    header=None,
                    dtype=dtype,
                    keep_default_na=keep_default_na,
                    engine="python",
                    skiprows=normalized_header_row - 1,
                    nrows=normalized_max_rows,
                )
            except Exception as exc:  # pragma: no cover - defensive
                raise ProviderError(f"Unable to parse the table: {exc}") from exc
            normalized = _extract_frame_from_rows(
                raw_frame,
                header_row=1,
                skip_rows=0,
            )
            return LoadedTable(
                source_kind="file",
                path=str(resolved.resolve()),
                delimiter=output_delimiter,
                text=normalized.to_csv(sep=output_delimiter, index=False),
                frame=normalized,
                file_format="delimited",
                sheet_name=None,
                available_sheets=[],
                header_row=normalized_header_row,
                skip_rows=normalized_skip_rows,
            )

        raw_text = resolved.read_text(encoding="utf-8")
        effective_delimiter = delimiter or infer_delimiter(raw_text)
        parse_delimiter = _parse_delimiter(effective_delimiter)
        output_delimiter = _output_delimiter(effective_delimiter)
        header_index = normalized_header_row - 1
        raw_lines = raw_text.splitlines()
        if header_index >= len(raw_lines):
            raise ProviderError(
                f"Requested header_row={normalized_header_row} is outside the table range ({len(raw_lines)} row(s))."
            )
        sliced_text = "\n".join(raw_lines[header_index:])
        try:
            raw_frame = pd.read_csv(
                StringIO(sliced_text),
                sep=parse_delimiter,
                header=None,
                dtype=dtype,
                keep_default_na=keep_default_na,
                engine="python",
            )
        except Exception as exc:  # pragma: no cover - defensive
            raise ProviderError(f"Unable to parse the table: {exc}") from exc
        normalized = _extract_frame_from_rows(
            raw_frame,
            header_row=1,
            skip_rows=0,
        )
        return LoadedTable(
            source_kind="file",
            path=str(resolved.resolve()),
            delimiter=output_delimiter,
            text=normalized.to_csv(sep=output_delimiter, index=False)
            if effective_delimiter == " " else raw_text,
            frame=normalized,
            file_format="delimited",
            sheet_name=None,
            available_sheets=[],
            header_row=normalized_header_row,
            skip_rows=normalized_skip_rows,
        )

    if not clean_text(inline_text):
        raise ProviderError("Table loading requires either a path or inline text input.")

    effective_delimiter = delimiter or infer_delimiter(inline_text)
    parse_delimiter = _parse_delimiter(effective_delimiter)
    output_delimiter = _output_delimiter(effective_delimiter)
    header_index = normalized_header_row - 1
    raw_lines = inline_text.splitlines()
    if header_index >= len(raw_lines):
        raise ProviderError(
            f"Requested header_row={normalized_header_row} is outside the table range ({len(raw_lines)} row(s))."
        )
    sliced_text = "\n".join(raw_lines[header_index:])
    try:
        raw_frame = pd.read_csv(
            StringIO(sliced_text),
            sep=parse_delimiter,
            header=None,
            dtype=dtype,
            keep_default_na=keep_default_na,
            engine="python",
            nrows=normalized_max_rows or None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise ProviderError(f"Unable to parse the inline table: {exc}") from exc

    normalized = _extract_frame_from_rows(
        raw_frame,
        header_row=1,
        skip_rows=0,
    )
    return LoadedTable(
        source_kind="inline",
        path="",
        delimiter=output_delimiter,
        text=normalized.to_csv(sep=output_delimiter, index=False)
        if effective_delimiter == " " else inline_text,
        frame=normalized,
        file_format="delimited",
        sheet_name=None,
        available_sheets=[],
        header_row=normalized_header_row,
        skip_rows=normalized_skip_rows,
    )


def write_table(frame: pd.DataFrame, path: Path, *, delimiter: str = "\t") -> None:
    normalized = _normalize_frame(frame)
    path.write_text(normalized.to_csv(sep=delimiter, index=False), encoding="utf-8")


class TemporaryDelimitedTable:
    def __init__(self, frame: pd.DataFrame, *, suffix: str = ".tsv", delimiter: str = "\t") -> None:
        self.frame = _normalize_frame(frame)
        self.suffix = suffix
        self.delimiter = delimiter
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    def __enter__(self) -> str:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="gleskit_table_")
        self.path = Path(self._tmpdir.name) / f"table{self.suffix}"
        write_table(self.frame, self.path, delimiter=self.delimiter)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
        self._tmpdir = None
        self.path = None


def ensure_string_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return _normalize_frame(frame.astype(str))


def table_rows_as_dicts(frame: pd.DataFrame) -> list[dict[str, Any]]:
    normalized = _normalize_frame(frame)
    return [
        {str(key): _coerce_json_value(value) for key, value in row.items()}
        for row in normalized.to_dict(orient="records")
    ]


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    normalized.columns = [str(column) for column in normalized.columns]
    return normalized


def _extract_frame_from_rows(frame: pd.DataFrame, *, header_row: int, skip_rows: int) -> pd.DataFrame:
    if frame.empty:
        return _normalize_frame(frame)

    normalized_header_row = max(1, int(header_row or 1))
    normalized_skip_rows = max(0, int(skip_rows or 0))
    header_index = normalized_header_row - 1
    if header_index >= len(frame.index):
        raise ProviderError(
            f"Requested header_row={normalized_header_row} is outside the table range ({len(frame.index)} row(s))."
        )

    header_values = frame.iloc[header_index].tolist()
    raw_columns = [
        clean_text(value) or f"column_{index + 1}"
        for index, value in enumerate(header_values)
    ]
    columns = _deduplicate_columns(raw_columns)
    # Keep skip_rows as source metadata; the header row defines where data begins.
    data_start = min(len(frame.index), header_index + 1)
    data_frame = frame.iloc[data_start:].copy()
    data_frame.columns = columns
    data_frame = data_frame.dropna(how="all")
    return _normalize_frame(data_frame)


def _coerce_json_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _deduplicate_columns(columns: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    deduplicated: list[str] = []
    for column in columns:
        token = str(column)
        count = counts.get(token, 0)
        if count == 0:
            deduplicated.append(token)
        else:
            deduplicated.append(f"{token}.{count}")
        counts[token] = count + 1
    return deduplicated
