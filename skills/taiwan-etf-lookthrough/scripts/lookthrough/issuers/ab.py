"""Parse AllianceBernstein actual holdings with same-date fund AUM."""

import base64
from datetime import date, datetime
from decimal import Decimal
import json
import re
from typing import Any, TYPE_CHECKING

from ._assets import (
    check_percentage, check_stock, nav_number, number, read_json,
)
from ..source_quality import discrepancy
from ..models import Holding, Snapshot, snapshot_from_dict, snapshot_to_dict

if TYPE_CHECKING:
    from .registry import SupportedEtf


SOURCE_URL = ("https://webapi.alliancebernstein.com/v2/funds/tw/zh-tw/"
              "investor/TW00000404A5/holdings")
NAV_URL = SOURCE_URL.rsplit("/", 1)[0] + "/basket"
_CATEGORIES = {"holdings-section-equity", "holdings-section-futures",
               "holdings-section-options"}


def capture(holdings: bytes, basket: bytes) -> bytes:
    """Keep two original HTTP bodies in an explicit local capture."""
    if any(not isinstance(raw, bytes) or not 0 < len(raw) <= 10 * 1024 * 1024
           for raw in (holdings, basket)):
        raise ValueError("Invalid AB capture size")
    return json.dumps({
        "format": "ab-assets-v1",
        "holdings_base64": base64.b64encode(holdings).decode("ascii"),
        "basket_base64": base64.b64encode(basket).decode("ascii"),
    }).encode()


def holdings_day(raw: bytes) -> date:
    """Require all actual holdings groups to share the same as-of day."""
    payload = read_json(raw)
    groups = payload.get("domesticHoldings") if isinstance(
        payload, dict) else None
    if not isinstance(groups, list) or len(groups) != 3:
        raise ValueError("Missing AB complete asset groups")
    try:
        days = {datetime.strptime(row["asOfDate"], "%m/%d/%Y").date()
                for row in groups}
    except (TypeError, KeyError) as error:
        raise ValueError("Invalid AB source date") from error
    if len(days) != 1:
        raise ValueError("AB source group dates disagree")
    return days.pop()


def _percent(value: Any, places: int = 6) -> Decimal:
    """Read native signed percentages without assigning economic meaning."""
    if not isinstance(value, str) or not re.fullmatch(
            rf"-?\d+(?:\.\d{{1,{places}}})?", value):
        raise ValueError("Invalid AB disclosed percentage")
    result = Decimal(value)
    if not -100 <= result <= 100:
        raise ValueError("AB percentage outside range")
    return result


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Use actual holdings; use basket only for same-day NAV, never stocks.

    Independently disclosed category aggregates guard complete collections.
    Stocks use market value/AUM, checked against six-decimal printed NAV
    percentages. Futures holdingValue is not interpreted as NAV or risk.
    """
    if (etf.symbol != "00404A" or etf.issuer != "ab" or not etf.is_active
            or etf.source_url != SOURCE_URL):
        raise ValueError("Unqualified AB request identity")
    wrapper = read_json(raw, 30 * 1024 * 1024)
    if (not isinstance(wrapper, dict) or set(wrapper)
            != {"format", "holdings_base64", "basket_base64"}
            or wrapper["format"] != "ab-assets-v1"):
        raise ValueError("Invalid AB paired capture")
    try:
        source = base64.b64decode(wrapper["holdings_base64"], validate=True)
        basket = read_json(base64.b64decode(wrapper["basket_base64"],
                                            validate=True))
    except (TypeError, ValueError) as error:
        raise ValueError("Malformed AB capture") from error
    payload = read_json(source)
    if (not isinstance(payload, dict) or set(payload) != {
            "domesticHoldings", "portfolioStatistics",
            "portfolioCharacteristics",
            "zesHoldings", "bufferCharacteristics", "disclaimer", "otherAsset",
            "fundAssetTotal"} or payload["zesHoldings"] is not None):
        raise ValueError("Unknown AB portfolio source structure")
    day = holdings_day(source)
    if (not isinstance(basket, dict) or basket.get("navAsOfDate")
            != day.isoformat() or basket.get("asOfDate") != day.isoformat()
            or basket.get("currency") != "TWD"):
        raise ValueError("AB basket NAV date/currency mismatch")
    nav = nav_number(basket.get("aum"), native=True)
    aggregate = payload.get("fundAssetTotal")
    other = payload.get("otherAsset")
    if (not isinstance(aggregate, dict) or aggregate.get("secID") != "00404A"
            or not isinstance(other, dict) or other.get("secID") != "00404A"):
        raise ValueError("AB actual portfolio identity mismatch")
    totals = aggregate.get("allocationObjSecType")
    if not isinstance(totals, list) or len(totals) != 3:
        raise ValueError("Missing AB independent totals")
    by_category = {row.get("allocationObjSecType"): row for row in totals}
    if set(by_category) != _CATEGORIES:
        raise ValueError("Unknown AB asset summary")
    holdings: list[Holding] = []
    seen_groups: set[str] = set()
    for group in payload["domesticHoldings"]:
        category = group.get("holdingCategory")
        if category not in _CATEGORIES or category in seen_groups:
            raise ValueError("Unknown/duplicate AB holdings category")
        seen_groups.add(category)
        places = 8 if category == "holdings-section-futures" else 6
        rows = group.get("holdings")
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError("Invalid AB security collection")
        value_total, percent_total = Decimal(0), Decimal(0)
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid AB source row")
            label, raw_code = row.get("holding"), row.get("holdingCode")
            if (not isinstance(label, str) or not label
                    or not isinstance(raw_code, str)):
                raise ValueError("Missing AB native identity")
            code = raw_code or label
            if code in seen:
                raise ValueError("Duplicate AB security identity")
            seen.add(code)
            amount = number(row.get("holdingValue"), native=True)
            quantity = number(row.get("holdingShares"), native=True)
            percent = _percent(row.get("holdingPerc"), places)
            value_total += amount
            percent_total += percent
            if category == "holdings-section-equity":
                # Native API has 1294499.9999999998 for an integer share count.
                # This affects only quantity validation, never NAV weights.
                nearest = quantity.to_integral()
                if (quantity < 0
                        or abs(quantity - nearest) > Decimal("0.00000001")):
                    raise ValueError("Invalid AB stock share precision")
                check_stock(code, label, nearest, amount, nav)
                check_percentage(f"{percent:.6f}%", amount, nav, 6)
                holdings.append(Holding(code, label, "stock",
                                        float(amount / nav)))
            else:
                if quantity != quantity.to_integral():
                    raise ValueError("Invalid AB derivative quantity")
                # The source does not establish book-NAV/notional meaning
                # for these native percentages. Preserve them as evidence,
                # with both canonical meanings unknown.
                native = f"{label}（原生權重 {row['holdingPerc']}%，口徑未確認）"
                kind = ("futures" if category == "holdings-section-futures"
                        else "other")
                holdings.append(Holding(code, native, kind, None))
        summary = by_category[category]
        if (abs(value_total - number(summary.get("underlyingSecuritiesValue")))
                > Decimal("0.01") or percent_total
                != _percent(summary.get("percentageUnderlyingSecurities"),
                            places)):
            discrepancy("Complete AB category total does not reconcile")
        if category == "holdings-section-equity" and not rows:
            raise ValueError("Empty AB equity disclosure")
    assets = other.get("allocationObjSecType")
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError("Missing AB other asset rows")
    seen: set[str] = set()
    rules = {"holdings-section-cash": "cash",
             "holdings-section-margin": "other"}
    for row in assets:
        key = row.get("allocationObjSecType")
        if (key not in rules or key in seen or row.get("allocationObjCCY")
                != "fundinfo-basecurrency-twd"):
            raise ValueError("Unknown AB other asset/currency")
        seen.add(key)
        amount = number(row.get("allocationObjValue"))
        if not -nav <= amount <= nav:
            raise ValueError("AB asset NAV outside range")
        holdings.append(Holding(key, key, rules[key], float(amount / nav)))
    snapshot = Snapshot("1", etf.symbol, etf.issuer, True, day, fetched_at,
                        SOURCE_URL, "agent_official", tuple(holdings))
    return snapshot_from_dict(snapshot_to_dict(snapshot))
