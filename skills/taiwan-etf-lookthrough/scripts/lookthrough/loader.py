"""Load locally acquired official CSV and XLSX holdings disclosures."""

from collections.abc import Callable, Generator, Iterator
import csv
from dataclasses import dataclass
from datetime import date
import io
import math
from pathlib import Path
import re
from typing import Literal, cast
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .models import Holding, Snapshot


Kind = Literal["stock", "futures", "cash", "other"]
KINDS = ("stock", "futures", "cash", "other")


@dataclass(frozen=True)
class ColumnMap:
    """Map exact trimmed column labels and declare the source weight unit.

    header_row is one-based. None searches the first 20 rows. A configured
    kind column takes precedence over kind_rules and default_kind.
    """

    code: str
    name: str
    weight: str
    kind: str | None = None
    weight_unit: Literal["percent", "fraction"] = "percent"
    header_row: int | None = None


def parse_number(text: str) -> float:
    """Parse a finite formatted number without interpreting its unit.

    Remove commas, percent markers and fullwidth spaces. Raise ValueError
    for blank text, non-string input or nonfinite numeric values.
    """
    if not isinstance(text, str):
        raise ValueError("Number must be a string")
    cleaned = text.replace(",", "").replace("%", "").replace("\u3000", "")
    cleaned = cleaned.strip()
    if not cleaned:
        raise ValueError("Number must not be blank")
    number = float(cleaned)
    if not math.isfinite(number):
        raise ValueError("Number must be finite")
    return number


def default_kind(code: str, name: str) -> str:
    """Classify source labels with futures and cash taking precedence."""
    if "期貨" in name or re.match(r"^(TX|MTX|TE|TF)", code):
        return "futures"
    if any(word in name for word in ("現金", "存款", "應收", "應付")):
        return "cash"
    if re.fullmatch(r"\d{4}[A-Z]?", code):
        return "stock"
    return "other"


def load_official_file(
    path: Path,
    columns: ColumnMap,
    *,
    etf_symbol: str,
    issuer: str,
    is_active: bool,
    holdings_date: date,
    source_url: str,
    fetched_at: str,
    source_kind: Literal["official", "agent_official"] = "agent_official",
    kind_rules: Callable[[str, str], str] | None = None,
) -> Snapshot:
    """Load an official-format file using explicit source metadata.

    Read CSV as strict UTF-8 with optional BOM, then strict cp950. Read the
    active XLSX sheet. Header rows are one-based, automatically searched
    only within the first 20 rows. Preserve blank weights as None and never
    infer futures notional exposure from NAV weights. Financial/provenance
    validation remains the caller's responsibility.

    Raise ValueError for unsupported formats, configuration, malformed
    tables or invalid cells; file access errors propagate as OSError.
    """
    labels = _validate_columns(columns)
    if source_kind not in ("official", "agent_official"):
        raise ValueError("Invalid official source_kind")
    if path.suffix.lower() not in (".csv", ".xlsx"):
        raise ValueError(f"Unsupported file extension: {path.suffix}")
    rows = _file_rows(path)
    holdings: list[Holding] = []
    try:
        indices = _find_header(rows, columns, labels)
        for row_number, row in rows:
            if not any(value is not None and str(value).strip()
                       for value in row):
                continue
            try:
                holdings.append(_parse_holding(
                    row, indices, columns, kind_rules or default_kind
                ))
            except (ValueError, TypeError, IndexError) as error:
                raise ValueError(
                    f"Invalid row {row_number}: {error}") from error
    finally:
        rows.close()
    return Snapshot(
        schema_version="1", etf_symbol=etf_symbol, issuer=issuer,
        is_active=is_active, holdings_date=holdings_date,
        fetched_at=fetched_at, source_url=source_url,
        source_kind=source_kind, holdings=tuple(holdings),
    )


def _validate_columns(columns: ColumnMap) -> list[str]:
    """Reject invalid units, row numbers and ambiguous mapped labels."""
    if columns.weight_unit not in ("percent", "fraction"):
        raise ValueError("weight_unit must be percent or fraction")
    if columns.header_row is not None and (
        type(columns.header_row) is not int or columns.header_row < 1
    ):
        raise ValueError("header_row must be a positive one-based integer")
    labels = [columns.code, columns.name, columns.weight]
    if columns.kind is not None:
        labels.append(columns.kind)
    if any(not isinstance(label, str) or not label.strip()
           or label != label.strip() for label in labels):
        raise ValueError("Column labels must be nonblank trimmed strings")
    if len(set(labels)) != len(labels):
        raise ValueError("Column labels must be distinct")
    return labels


def _file_rows(
    path: Path,
) -> Generator[tuple[int, tuple[object, ...]], None, None]:
    """Yield numbered CSV rows or active-sheet XLSX rows, closing resources."""
    if path.suffix.lower() == ".csv":
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "cp950"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Cannot decode CSV as utf-8-sig or cp950")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        try:
            for row in reader:
                yield reader.line_num, tuple(row)
        except csv.Error as error:
            raise ValueError(
                f"Malformed CSV near row {reader.line_num}: {error}"
            ) from error
        return
    workbook = None
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        if workbook.active is None:
            raise ValueError("XLSX has no active sheet")
        for number, row in enumerate(
            workbook.active.iter_rows(values_only=True), start=1
        ):
            yield number, row
    except (BadZipFile, InvalidFileException, ParseError, KeyError,
            ValueError, EOFError) as error:
        raise ValueError(f"Malformed XLSX: {error}") from error
    finally:
        if workbook is not None:
            workbook.close()


def _find_header(
    rows: Iterator[tuple[int, tuple[object, ...]]],
    columns: ColumnMap,
    labels: list[str],
) -> dict[str, int]:
    """Find the first complete header and reject duplicate mapped columns."""
    required = [columns.code, columns.name, columns.weight]
    for number, row in rows:
        if columns.header_row is not None:
            if number < columns.header_row:
                continue
            if number > columns.header_row:
                break
        elif number > 20:
            break
        header = [_cell_text(value) for value in row]
        if all(label in header for label in required):
            if any(header.count(label) != 1 for label in labels):
                raise ValueError(
                    f"Missing or ambiguous header at row {number}")
            return {label: header.index(label) for label in labels}
        if columns.header_row is not None:
            break
    raise ValueError("Required header not found in configured search rows")


def _cell_text(value: object) -> str:
    """Convert source cells to trimmed text without guessing missing codes."""
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("Boolean cell is not a holding label or weight")
    return str(value).strip()


def _parse_holding(
    row: tuple[object, ...],
    indices: dict[str, int],
    columns: ColumnMap,
    kind_rules: Callable[[str, str], str],
) -> Holding:
    """Parse a complete row without zero-filling or inferring exposure."""
    if len(row) <= max(indices.values()):
        raise ValueError("Missing mapped cells")
    code = _cell_text(row[indices[columns.code]])
    name = _cell_text(row[indices[columns.name]])
    if not code and not name:
        raise ValueError("Holding code and name are both blank")
    text = _cell_text(row[indices[columns.weight]])
    weight = parse_number(text) if text else None
    if weight is not None and columns.weight_unit == "percent":
        weight /= 100
    kind = (_cell_text(row[indices[columns.kind]])
            if columns.kind is not None else kind_rules(code, name))
    if kind not in KINDS:
        raise ValueError(f"Unknown holding kind: {kind!r}")
    return Holding(code, name, cast(Kind, kind), weight)
