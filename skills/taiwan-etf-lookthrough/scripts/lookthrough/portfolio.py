"""Parse local TWD portfolios while preserving raw-byte input provenance."""

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Literal


class PortfolioError(ValueError):
    """Signal malformed portfolios, identifying invalid one-based rows."""


@dataclass(frozen=True)
class Position:
    """Store one normalized symbol and its positive TWD market value."""

    symbol: str
    kind: Literal["etf", "stock", "cash", "other_asset"]
    market_value: float


@dataclass(frozen=True)
class Portfolio:
    """Store valuation date, merged positions, warnings and input hash."""

    valuation_date: date
    positions: tuple[Position, ...]
    warnings: tuple[str, ...]
    input_sha256: str
    skipped_rows: tuple[dict[str, Any], ...] = ()


def load_portfolio(path: Path, *, policy: str = "strict") -> Portfolio:
    """Read local JSON bytes and validate them, raising PortfolioError.

    File, encoding and JSON errors are wrapped without echoing private input.
    The input hash covers original bytes, including whitespace and encoding.
    """
    try:
        raw_bytes = path.read_bytes()
        data = json.loads(raw_bytes)
    except (OSError, UnicodeError, ValueError) as error:
        raise PortfolioError("無法讀取投資組合 JSON 檔案") from error
    return parse_portfolio(data, raw_bytes, policy=policy)


def parse_portfolio(data: dict[str, Any], raw_bytes: bytes, *,
                    policy: str = "strict") -> Portfolio:
    """Validate decoded JSON and hash its original bytes without mutation.

    Only TWD, ISO calendar dates, valid symbols and positive finite numeric
    values are accepted. Duplicate symbols merge in first-appearance order;
    conflicting kinds or nonfinite totals raise PortfolioError with row.
    """
    if policy not in ("strict", "annotated"):
        raise PortfolioError("policy must be strict or annotated")
    if not isinstance(data, dict):
        raise PortfolioError("投資組合必須為 JSON object")
    if data.get("currency") != "TWD":
        raise PortfolioError("currency 必須為 TWD")
    date_text = data.get("valuation_date")
    try:
        if not isinstance(date_text, str):
            raise ValueError("Date must be a string")
        valuation_date = date.fromisoformat(date_text)
        if valuation_date.isoformat() != date_text:
            raise ValueError("Date must use YYYY-MM-DD")
    except ValueError as error:
        raise PortfolioError("valuation_date 必須為有效的 YYYY-MM-DD") from error
    rows = data.get("positions")
    if not isinstance(rows, list) or not rows:
        raise PortfolioError("positions 必須為非空陣列")

    merged: dict[str, Position] = {}
    counts: dict[str, int] = {}
    total = 0.0
    notes: list[str] = []
    skipped: list[dict[str, Any]] = []
    for number, row in enumerate(rows, start=1):
        if policy == "annotated":
            if not isinstance(row, dict):
                skipped.append(dict(row=number, reason="部位格式無法解析"))
                continue
            row = dict(row)
            value = row.get("market_value")
            if isinstance(value, str):
                try:
                    value = float(value.replace(",", "").strip())
                except ValueError as error:
                    raise PortfolioError(
                        f"第 {number} 列：market_value 無法解析") from error
                row["market_value"] = value
                notes.append(f"第 {number} 列：字串市值已解析為數字")
            if (type(value) in (int, float) and value == 0):
                skipped.append(dict(row=number, reason="已略過零市值部位"))
                continue
            kind = row.get("kind")
            row["kind"] = kind.lower() if isinstance(kind, str) else kind
            symbol = row.get("symbol")
            if (not isinstance(symbol, str) or not symbol.strip()
                    or len(symbol.strip()) > 80
                    or any(ord(char) < 32 for char in symbol)):
                row["symbol"] = f"ROW-{number}"
                row["kind"] = "other_asset"
                notes.append(f"第 {number} 列：代號無法解析，市值保留於未穿透")
            elif row["kind"] not in ("etf", "stock", "cash", "other_asset"):
                row["kind"] = "other_asset"
                notes.append(f"第 {number} 列：部位類型無法解析，不穿透")
            if (isinstance(symbol, str) and symbol.strip()
                    and row["kind"] not in ("cash", "other_asset")
                    and not re.fullmatch(r"\d{4,6}[A-Z]?",
                                         symbol.strip().upper())):
                row["kind"] = "other_asset"
                notes.append(f"第 {number} 列：非台股 ETF／股票，不穿透")
        position = _parse_position(row, number, policy=policy)
        prior = merged.get(position.symbol)
        if prior is not None:
            if prior.kind != position.kind:
                if policy == "strict":
                    raise PortfolioError(f"第 {number} 列：相同 symbol 的 kind 衝突")
                notes.append(f"第 {number} 列：相同代號類型衝突，合併為未穿透部位")
                position = Position(position.symbol, "other_asset",
                                    position.market_value)
            value = prior.market_value + position.market_value
            if not math.isfinite(value):
                raise PortfolioError(f"第 {number} 列：合併金額超出有限範圍")
            merged[position.symbol] = Position(
                position.symbol, position.kind, value,
            )
        else:
            merged[position.symbol] = position
        total += position.market_value
        if not math.isfinite(total):
            raise PortfolioError(f"第 {number} 列：組合總金額超出有限範圍")
        counts[position.symbol] = counts.get(position.symbol, 0) + 1

    if not merged:
        raise PortfolioError("positions 沒有正市值的可用部位")
    warnings = tuple(notes) + tuple(
        f"第 {row['row']} 列：{row['reason']}" for row in skipped
    ) + tuple(
        f"重複輸入 {symbol}：已合併 {count} 列"
        for symbol, count in counts.items() if count > 1
    )
    return Portfolio(
        valuation_date, tuple(merged.values()), warnings,
        hashlib.sha256(raw_bytes).hexdigest(),
        tuple(skipped),
    )


def _parse_position(row: Any, number: int, *,
                    policy: str = "strict") -> Position:
    """Validate a row and return a normalized position or PortfolioError."""
    if not isinstance(row, dict):
        raise PortfolioError(f"第 {number} 列：必須為 JSON object")
    symbol = row.get("symbol")
    if isinstance(symbol, str):
        symbol = symbol.strip().upper()
    valid = (isinstance(symbol, str) and (
        bool(symbol) and len(symbol) <= 80
        and not any(ord(char) < 32 for char in symbol)
        if policy == "annotated" else
        re.fullmatch(r"[0-9A-Z]{4,6}", symbol)))
    if not valid:
        raise PortfolioError(f"第 {number} 列：symbol 必須為 4–6 位英數代號")
    kind = row.get("kind")
    kinds = (("etf", "stock", "cash", "other_asset")
             if policy == "annotated" else ("etf", "stock"))
    if kind not in kinds:
        raise PortfolioError(f"第 {number} 列：kind 必須為 etf 或 stock")
    value = row.get("market_value")
    try:
        if type(value) not in (int, float):
            raise ValueError("Value must be numeric and not boolean")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Value must be positive and finite")
    except (ValueError, OverflowError) as error:
        raise PortfolioError(
            f"第 {number} 列：market_value 必須為有限且大於 0 的數值",
        ) from error
    return Position(symbol, kind, value)
