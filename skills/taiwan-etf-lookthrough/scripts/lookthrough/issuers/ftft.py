"""Parse qualified SinoAm 00905 full assets using disclosed TWD amounts."""

from datetime import date
from decimal import Decimal
import re
from typing import Any, TYPE_CHECKING

from ._assets import (
    asset_day, check_percentage, check_stock, nav_number, number, read_json,
)
from ..source_quality import discrepancy
from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = "https://www.ftft.com.tw/official/api/etf/share-dates/131"
SHARES_URL = "https://www.ftft.com.tw/official/api/etf/shares/131"
_KEYS = {"FundID", "StockCode", "FundName", "AssetDate",
         "FundNetAssetValue", "TotalUnitsOutstanding", "NetAssetValuePerUnit",
         "NetAssetValuePrecision", "Categories", "Equities", "Futures",
         "ETFs", "RePurchases", "Bonds", "BondsDiffs", "Secs", "Futs",
         "AreaSec"}
_EMPTY = ("Equities", "Futures", "ETFs", "RePurchases", "Bonds",
          "BondsDiffs", "AreaSec")
_ASSETS = {"股票": "stock", "現金": "cash", "保證金": "other"}


def latest_day(raw: bytes) -> date:
    """Select the latest disclosed date without inventing a query date."""
    dates = read_json(raw)
    if not isinstance(dates, list) or not dates or len(dates) > 10000:
        raise ValueError("Invalid FTFT share-date list")
    return max(asset_day(value) for value in dates)


def _context(row: Any, timestamp: str) -> None:
    """Require every source row to belong to the same fund and asset day."""
    if (not isinstance(row, dict) or row.get("FundID") != "131"
            or row.get("StockCode") != "00905"
            or row.get("AssetDate") != timestamp):
        raise ValueError("FTFT nested identity/date mismatch")


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse native full-asset JSON; reject unqualified new asset types.

    Weights use source market value divided by the same response's NAV.
    Printed weights are checked at two decimal percentage precision.
    Cash and margin remain disclosed contributions; missing liabilities
    remain a gap. Futures notional exposure is deliberately unknown.
    No network access, source mutation or weight normalization occurs.
    """
    if (etf.symbol != "00905" or etf.issuer != "ftft" or etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified FTFT request identity")
    payload = read_json(raw)
    if not isinstance(payload, dict) or payload.keys() != _KEYS:
        raise ValueError("Unknown FTFT asset structure")
    timestamp = payload["AssetDate"]
    day = asset_day(timestamp)
    _context(payload, timestamp)
    if (not isinstance(payload["FundName"], str)
            or not payload["FundName"].startswith("富蘭克林華美臺灣Smart ETF基金")
            or any(payload[key] != [] for key in _EMPTY)):
        raise ValueError("Unknown FTFT fund or unqualified asset collection")
    nav = nav_number(payload["FundNetAssetValue"], native=True)
    categories = payload["Categories"]
    if not isinstance(categories, list) or not categories:
        raise ValueError("Missing FTFT asset totals")
    totals: dict[str, Decimal] = {}
    holdings: list[Holding] = []
    for row in categories:
        _context(row, timestamp)
        label = row.get("AssetCategory")
        if (label not in _ASSETS or label in totals
                or row.get("AssetCurrency") != "TWD"):
            raise ValueError("Unknown/duplicate FTFT asset or currency")
        amount = number(row.get("AssetValue"), native=True)
        if not -nav <= amount <= nav:
            raise ValueError("FTFT asset outside NAV range")
        totals[label] = amount
        if label != "股票":
            holdings.append(Holding(label, label, _ASSETS[label],
                                    float(amount / nav)))
    if set(totals) != set(_ASSETS):
        raise ValueError("Missing qualified FTFT asset category")
    stocks = payload["Secs"]
    if not isinstance(stocks, list) or not 1 <= len(stocks) <= 5000:
        raise ValueError("Missing/oversized FTFT complete stocks")
    seen: set[str] = set()
    total = Decimal(0)
    for row in stocks:
        _context(row, timestamp)
        if row.get("SecuritiesType") != "S" or row.get("Currency") != "TWD":
            raise ValueError("Unqualified FTFT security type/currency")
        code, name = row.get("SecuritiesCode"), row.get("SecuritiesName")
        amount = number(row.get("MarketValue"), native=True)
        check_stock(code, name, number(row.get("Shares"), native=True),
                    amount, nav)
        if (code in seen or row.get("Weighting") is not None
                or number(row.get("MarketValueOrigin"), native=True)
                != amount):
            raise ValueError("Duplicate or changed FTFT stock convention")
        check_percentage(row.get("WeightingPercentage"), amount, nav, 2)
        seen.add(code)
        total += amount
        holdings.append(Holding(code, name, "stock", float(amount / nav)))
    if total != totals["股票"]:
        discrepancy("Complete FTFT stock total does not reconcile")
    futures = payload["Futs"]
    if not isinstance(futures, list) or len(futures) > 100:
        raise ValueError("Invalid FTFT futures collection")
    seen.clear()
    for row in futures:
        _context(row, timestamp)
        code, name = row.get("FutCode"), row.get("FutName")
        quantity = number(row.get("Lot"), native=True)
        if (not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]+", code)
                or code in seen or not isinstance(name, str) or not name
                or row.get("Currency") != "TWD"
                or quantity < 0 or quantity != quantity.to_integral()
                or not re.fullmatch(r"\d{4}/\d{2}",
                                    str(row.get("ContractDate")))):
            raise ValueError("Invalid/ambiguous FTFT futures identity")
        seen.add(code)
        holdings.append(Holding(
            code, f'{name} {row["ContractDate"]}', "futures", None))
    snapshot = Snapshot(
        "1", etf.symbol, etf.issuer, False, day, fetched_at,
        f"{SHARES_URL}?date={day:%Y%m%d}", "official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
