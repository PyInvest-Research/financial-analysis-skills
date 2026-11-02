"""Qualify refreshed official inventories independently of fixed research."""

from datetime import date
from functools import lru_cache
import json
from pathlib import Path

from .directory import CompanyDirectory
from .models import Snapshot, snapshot_sha256


DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def products() -> dict[str, dict]:
    """Read the bounded reviewed product and overseas-identity allowlist."""
    return json.loads((DATA / "refresh-products.json").read_text())["products"]


@lru_cache(maxsize=1)
def candidate_products() -> dict[str, dict]:
    """Read dated offline-reviewed candidates, never daily acquisition routes."""
    rows = json.loads((DATA / "refresh-candidates.json").read_text())["products"]
    if (set(rows).intersection(products())
            or any(row.get("automatic_update") is not False
                   or row.get("runner_accepted") is not False
                   for row in rows.values())):
        raise ValueError("Candidate products must remain outside daily routes")
    return rows


def reviewed_products() -> dict[str, dict]:
    """Share identity validation while keeping acquisition opt-in separate."""
    return {**products(), **candidate_products()}


@lru_cache(maxsize=1)
def directory() -> CompanyDirectory:
    """Reuse the bundled official Taiwan company directory per process."""
    return CompanyDirectory.from_file(DATA / "company-directory.json")


def resolve_security(symbol: str, code: str, name: str) -> tuple[str, str]:
    """Resolve known source identities or exact Taiwan company identities.

    An unfamiliar overseas security fails qualification rather than being
    inferred from a numeric ticker or silently counted as zero Taiwan.
    The Taishin TT suffix is a reviewed source convention, not a global rule.
    """
    product = reviewed_products()[symbol]
    if product["issuer"] == "tsit" and code.endswith(" TT"):
        code = code[:-3]
    known = product["markets"].get(code)
    if known and known["name"] == name:
        return code, known["market"]
    company = directory().get(code)
    resolution = directory().resolve(code, name)
    if (company is not None and resolution.issuer_code == company.code
            and company.market in ("TWSE", "TPEx")):
        # Name agreement prevents a foreign numeric ticker collision.
        if name == company.name:
            return code, company.market
    raise ValueError(f"Unqualified actual security identity: {code}")


def qualifies_refresh(snapshot: Snapshot) -> bool:
    """Require an exact product route and independently known equity markets.

    Raw content hashes are provenance references, not an assertion that
    arbitrary caller-provided JSON was downloaded by this process.
    Numeric completeness and NAV consistency are checked by the validator.
    """
    from .native_daily import qualifies as qualifies_native

    if qualifies_native(snapshot):
        return True
    product = reviewed_products().get(snapshot.etf_symbol)
    if (product is None or snapshot.schema_version != "2"
            or snapshot.issuer != product["issuer"]
            or snapshot.is_active != product["is_active"]
            or snapshot.source_url != product["source_url"]
            or type(snapshot.holdings_date) is not date):
        return False
    if snapshot.etf_symbol in candidate_products():
        # Only the reviewed dated content qualifies before runner acceptance.
        try:
            if snapshot_sha256(snapshot) != product.get("snapshot_sha256"):
                return False
        except (ValueError, TypeError, OverflowError):
            return False
    equities = [h for h in snapshot.holdings
                if h.kind in ("stock", "foreign_stock")]
    if not equities:
        return False
    try:
        for holding in equities:
            code, market = resolve_security(
                snapshot.etf_symbol, holding.code, holding.name)
            expected_kind = ("stock" if market in ("TWSE", "TPEx")
                             else "foreign_stock")
            if (code != holding.code or market != holding.market
                    or expected_kind != holding.kind):
                return False
    except (KeyError, ValueError):
        return False
    return True


def qualifies_candidate(snapshot: Snapshot) -> bool:
    """Bind historical candidate claims to their exact reviewed content."""
    product = candidate_products().get(snapshot.etf_symbol)
    if product is None:
        return False
    try:
        return (snapshot_sha256(snapshot) == product.get("snapshot_sha256")
                and qualifies_refresh(snapshot))
    except (ValueError, TypeError, OverflowError):
        return False
