"""Load locally acquired official CSV and XLSX holdings disclosures."""

from collections.abc import Callable, Generator, Iterator
import csv
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
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
Label = str | tuple[str, ...]
MISSING_MARKERS = frozenset(("-", "--", "—", "N/A", "NA"))


@dataclass(frozen=True)
class ColumnMap:
    """Map exact trimmed column labels and declare the source weight unit.

    header_row is one-based. Search 20 rows in strict, 60 in annotated. A
    kind column takes precedence over kind_rules and default_kind.
    """

    code: Label
    name: Label
    weight: Label
    kind: Label | None = None
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
    policy: str = "strict",
) -> Snapshot:
    """Load an official-format file using explicit source metadata.

    Read CSV as strict UTF-8 with optional BOM, then strict cp950. Read the
    active XLSX sheet in strict mode. Annotated searches all sheets and up
    to 60 header rows, accepts aliases and BIFF/HTML XLS, and records
    skipped rows in quality_notes. Strict searches the first 20 rows.
    Preserve blank weights as None and never
    infer futures notional exposure from NAV weights. Financial/provenance
    validation remains the caller's responsibility.

    Raise ValueError for unsupported formats, configuration, malformed
    tables or invalid cells; file access errors propagate as OSError.
    """
    if policy not in ("strict", "annotated"):
        raise ValueError("policy must be strict or annotated")
    annotated = policy == "annotated"
    labels = _validate_columns(columns)
    if source_kind not in ("official", "agent_official"):
        raise ValueError("Invalid official source_kind")
    if path.suffix.lower() not in (
            (".csv", ".xlsx", ".xls") if annotated else (".csv", ".xlsx")):
        raise ValueError(f"Unsupported file extension: {path.suffix}")
    notes: list[str] = []
    rows = _file_rows(path, columns, labels, notes, annotated)
    holdings: list[Holding] = []
    suspicious = skipped = 0
    try:
        indices = _find_header(rows, columns, labels, 60 if annotated else 20)
        for row_number, row in rows:
            if not any(value is not None and str(value).strip()
                       for value in row):
                continue
            try:
                if annotated:
                    category = _row_category(row, indices, columns)
                    if category in ("header", "section"):
                        continue
                    if category in ("total", "footer"):
                        weight = (_cell_text(row[indices[columns.weight]])
                                  if len(row) > indices[columns.weight]
                                  else "")
                        notes.append(f"第 {row_number} 列：已略過 {category}；"
                                     f"揭露值 {weight[:80]}")
                        continue
                    code = _cell_text(row[indices[columns.code]])
                    suspicious += bool(re.fullmatch(r"\d{4,6}[A-Z]?", code))
                holdings.append(_parse_holding(
                    row, indices, columns, kind_rules or default_kind,
                    annotated=annotated,
                ))
            except _UnsafeWeight:
                raise
            except (ValueError, TypeError, IndexError) as error:
                if annotated:
                    _check_rejected_weight(row, indices, columns)
                    code = (_cell_text(row[indices[columns.code]])
                            if len(row) > indices[columns.code] else "")
                    skipped += bool(re.fullmatch(r"\d{4,6}[A-Z]?", code))
                    notes.append(f"第 {row_number} 列：無法解析，已略過")
                    continue
                raise ValueError(
                    f"Invalid row {row_number}: {error}") from error
    finally:
        rows.close()
    if skipped > 5 or skipped > suspicious * .05:
        raise ValueError("Skipped suspicious data rows exceed 5 rows or 5%")
    if len(notes) > 1000:
        raise ValueError("Too many quality notes")
    return Snapshot(
        schema_version="1", etf_symbol=etf_symbol, issuer=issuer,
        is_active=is_active, holdings_date=holdings_date,
        fetched_at=fetched_at, source_url=source_url,
        source_kind=source_kind, holdings=tuple(holdings),
        quality_notes=tuple(notes),
    )


def _aliases(label: Label) -> tuple[str, ...]:
    """Return ordered, explicit alternative labels without guessing units."""
    return (label,) if isinstance(label, str) else label


def _validate_columns(columns: ColumnMap) -> list[Label]:
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
    if any(not isinstance(label, (str, tuple)) or not label
           or any(not isinstance(alias, str) or not alias.strip()
                  or alias != alias.strip() for alias in _aliases(label))
           for label in labels):
        raise ValueError("Column labels must be nonblank trimmed strings")
    flat = [alias for label in labels for alias in _aliases(label)]
    if len(set(flat)) != len(flat):
        raise ValueError("Column labels must be distinct")
    return labels


def _file_rows(
    path: Path,
    columns: ColumnMap,
    labels: list[Label],
    notes: list[str],
    annotated: bool,
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
    if path.suffix.lower() == ".xls":
        yield from _xls_rows(path, columns, labels, notes)
        return
    workbook = None
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        if workbook.active is None:
            raise ValueError("XLSX has no active sheet")
        selected = workbook.active
        if annotated:
            for sheet in [workbook.active, *(s for s in workbook.worksheets
                                             if s != workbook.active)]:
                probe = enumerate(sheet.iter_rows(values_only=True), start=1)
                try:
                    _find_header(probe, columns, labels, 60)
                except ValueError:
                    continue
                selected = sheet
                if sheet != workbook.active:
                    notes.append(f"採用工作表：{sheet.title}")
                break
        for number, row in enumerate(
            selected.iter_rows(values_only=True), start=1
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
    labels: list[Label],
    search_rows: int = 20,
) -> dict[Label, int]:
    """Find the first complete header and reject duplicate mapped columns."""
    required = [columns.code, columns.name, columns.weight]
    for number, row in rows:
        if columns.header_row is not None:
            if number < columns.header_row:
                continue
            if number > columns.header_row:
                break
        elif number > search_rows:
            break
        header = [_cell_text(value) for value in row]
        selected = {label: next((alias for alias in _aliases(label)
                                 if alias in header), None)
                    for label in labels}
        if all(selected[label] is not None for label in required):
            if any(selected[label] is None
                   or header.count(selected[label]) != 1 for label in labels):
                raise ValueError(
                    f"Missing or ambiguous header at row {number}")
            return {label: header.index(selected[label]) for label in labels}
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
    indices: dict[Label, int],
    columns: ColumnMap,
    kind_rules: Callable[[str, str], str],
    *, annotated: bool = False,
) -> Holding:
    """Parse a complete row without zero-filling or inferring exposure."""
    if len(row) <= max(indices.values()):
        raise ValueError("Missing mapped cells")
    code = _cell_text(row[indices[columns.code]])
    name = _cell_text(row[indices[columns.name]])
    if not code and not name:
        raise ValueError("Holding code and name are both blank")
    text = _cell_text(row[indices[columns.weight]])
    if annotated and text.upper() in MISSING_MARKERS:
        text = ""
    try:
        weight = parse_number(text) if text else None
    except ValueError as error:
        if annotated:
            try:
                numeric = float(text.replace(",", "").replace("%", "").strip())
            except ValueError:
                pass
            else:
                if not math.isfinite(numeric):
                    raise _UnsafeWeight(
                        "Nonfinite weight invalidates source") from error
        raise
    if weight is not None and columns.weight_unit == "percent":
        weight /= 100
    kind = (_cell_text(row[indices[columns.kind]])
            if columns.kind is not None else kind_rules(code, name))
    if kind not in KINDS:
        raise ValueError(f"Unknown holding kind: {kind!r}")
    if annotated and weight is not None and not (
            (-1 if kind in ("cash", "other") else 0) <= weight <= 1):
        raise _UnsafeWeight("Out-of-range weight or incorrect unit")
    return Holding(code, name, cast(Kind, kind), weight)


class _UnsafeWeight(ValueError):
    """Reject a corrupt source instead of skipping its invalid numbers."""


def _check_rejected_weight(row: tuple[object, ...],
                           indices: dict[Label, int],
                           columns: ColumnMap) -> None:
    """Keep numeric source failures fatal even when another cell is invalid."""
    index = indices[columns.weight]
    if len(row) <= index or row[index] is None:
        return
    if isinstance(row[index], bool):
        raise _UnsafeWeight("Boolean weight invalidates source")
    text = str(row[index]).replace(",", "").replace("%", "")
    text = text.replace("\u3000", "").strip()
    try:
        numeric = float(text)
    except ValueError:
        return
    if columns.weight_unit == "percent":
        numeric /= 100
    if not math.isfinite(numeric) or abs(numeric) > 1:
        raise _UnsafeWeight("Invalid numeric weight invalidates source")


def _row_category(row: tuple[object, ...], indices: dict[Label, int],
                  columns: ColumnMap) -> str:
    """Recognize table artifacts conservatively before parsing data rows."""
    def cell(label: Label) -> str:
        """Read an optional cell without requiring trailing empty columns."""
        return (_cell_text(row[indices[label]])
                if len(row) > indices[label] else "")

    code, name, weight = (cell(label) for label in (
        columns.code, columns.name, columns.weight))
    if (code in _aliases(columns.code) and name in _aliases(columns.name)
            and weight in _aliases(columns.weight)):
        return "header"
    if re.search(r"合計|總計|小計|\btotal\b", code + " " + name, re.I):
        return "total"
    if not weight and (code or name) in (
            "股票", "期貨", "現金", "債券", "其他", "海外股票", "國內股票"):
        return "section"
    if code.startswith(("註", "資料", "*")):
        return "footer"
    return "data"


class _HTMLTables(HTMLParser):
    """Extract plain table cell text without executing HTML or scripts."""

    def __init__(self) -> None:
        """Start independent table and row buffers."""
        super().__init__(convert_charrefs=True)
        self.tables: list[list[tuple[object, ...]]] = []
        self.row: list[str] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        """Open table, row and cell text buffers."""
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data: str) -> None:
        """Keep visible cell text only."""
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Finish cells and rows without interpreting embedded content."""
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.tables:
            self.tables[-1].append(tuple(self.row))


def _xls_rows(path: Path, columns: ColumnMap, labels: list[Label],
              notes: list[str]) -> Iterator[tuple[int, tuple[object, ...]]]:
    """Read HTML-disguised XLS or BIFF with xlrd; never evaluate formulas."""
    raw = path.read_bytes()
    tables: list[tuple[str, list[tuple[object, ...]]]] = []
    if raw.lstrip().lower().startswith((b"<", b"\xef\xbb\xbf<")):
        for encoding in ("utf-8-sig", "cp950"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Cannot decode HTML XLS")
        parser = _HTMLTables()
        parser.feed(text)
        tables = [(f"HTML table {i + 1}", rows)
                  for i, rows in enumerate(parser.tables)]
    else:
        import xlrd

        try:
            book = xlrd.open_workbook(file_contents=raw, on_demand=True)
            try:
                for sheet in book.sheets():
                    tables.append((sheet.name, [tuple(
                        str(int(v)) if type(v) is float and v.is_integer()
                        else v for v in sheet.row_values(i))
                        for i in range(sheet.nrows)]))
            finally:
                book.release_resources()
        except (xlrd.XLRDError, ValueError, IndexError) as error:
            raise ValueError(f"Malformed BIFF XLS: {error}") from error
    for name, rows in tables:
        try:
            _find_header(iter(enumerate(rows, 1)), columns, labels, 60)
        except ValueError:
            continue
        notes.append(f"採用工作表：{name}")
        yield from enumerate(rows, 1)
        return
    raise ValueError("Required XLS header not found")
