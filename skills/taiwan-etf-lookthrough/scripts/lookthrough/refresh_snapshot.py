"""Value reviewed inventories with same-day prices and preserve unknowns."""

from decimal import Decimal
import hashlib

from .models import EquityEvidence, Holding, Snapshot
from .refresh_contract import products, resolve_security
from .refresh_inventory import Inventory
from .refresh_prices import read_prices


def inventory_markets(symbol: str, inventory: Inventory) -> set[str]:
    """Return only the Taiwan quote markets required for this inventory."""
    markets = set()
    for row in inventory.stocks:
        if row.nav_weight is not None:
            continue
        try:
            markets.add(resolve_security(symbol, row.code, row.name)[1])
        except ValueError:
            continue
    return markets.intersection({"TWSE", "TPEx"})


def build_snapshot(
    symbol: str, inventory: Inventory, raw_sources: list[bytes],
    quote_sources: dict[str, bytes], fetched_at: str,
) -> Snapshot:
    """Keep the whole-fund denominator and never reweight missing assets.

    Raw quote dates must equal the actual holdings date. Unknown overseas
    weights remain None. A market is never inferred from a ticker suffix
    except the reviewed issuer-specific TT normalization.
    """
    product = products()[symbol]
    prices = {market: read_prices(raw, market, inventory.day)
              for market, raw in quote_sources.items()}
    holdings = list(inventory.assets)
    taiwan_value = Decimal(0)
    complete = True
    valued_local = True
    equity_count = 0
    seen: set[tuple[str, str]] = set()
    for row in inventory.stocks:
        if (not row.shares.is_finite() or row.shares < 0
                or row.shares != row.shares.to_integral()):
            raise ValueError("Invalid actual stock quantity")
        try:
            code, market = resolve_security(symbol, row.code, row.name)
        except ValueError:
            complete = False
            holdings.append(Holding(row.code, row.name + " [market unknown]",
                                    "other", None))
            continue
        identity = (market, code)
        if identity in seen:
            raise ValueError("Duplicate normalized refreshed security")
        seen.add(identity)
        equity_count += 1
        local = market in ("TWSE", "TPEx")
        weight = row.nav_weight
        if local and weight is None:
            close = prices.get(market, {}).get(code)
            if close is None or inventory.nav is None:
                valued_local = False
            else:
                amount = row.shares * close
                weight = amount / inventory.nav
                taiwan_value += amount
        elif local:
            if inventory.nav is None:
                valued_local = False
            else:
                taiwan_value += weight * inventory.nav
        holdings.append(Holding(
            code, row.name, "stock" if local else "foreign_stock",
            float(weight) if weight is not None else None, market=market))
    sources = raw_sources + list(quote_sources.values())
    if (complete and valued_local and inventory.stock_total is not None
            and all(h.kind != "foreign_stock" for h in holdings)):
        # Preserve the independent official total, including native rounding.
        taiwan_value = inventory.stock_total
    hashes = tuple(dict.fromkeys(hashlib.sha256(raw).hexdigest()
                                 for raw in sources))
    if not 1 <= len(hashes) <= 10:
        raise ValueError("Invalid refreshed source evidence count")
    evidence = EquityEvidence(
        complete=complete,
        total_nav_twd=(float(inventory.nav)
                       if inventory.nav is not None else None),
        stock_value_twd=(float(inventory.stock_total)
                         if inventory.stock_total is not None else None),
        stock_rows=equity_count,
        taiwan_stock_value_twd=(float(taiwan_value)
                                if complete and valued_local else None),
        source_hashes=hashes)
    return Snapshot("2", symbol, product["issuer"], product["is_active"],
                    inventory.day, fetched_at, product["source_url"],
                    "official", tuple(holdings), evidence)
