"""Measure issuer concentration and disclosed ETF overlap offline."""


from dataclasses import dataclass
from itertools import combinations
from math import fsum

from .calculate import LookthroughResult
from .directory import CompanyDirectory
from .models import Snapshot, merge_duplicate_holdings


@dataclass(frozen=True)
class IndustryShare:
    """Store an industry's TWD amount and full-portfolio share."""

    industry_code: str
    industry_name: str
    amount: float
    share: float


@dataclass(frozen=True)
class Metrics:
    """Keep concentration and ETF overlaps without rescaling NAV weights.

    Only effective_holdings describes normalized identified equity exposure;
    it is not a risk or correlation measure. Overlaps use disclosed weights,
    excluding unresolved securities without normalizing the remainder.
    """

    industries: tuple[IndustryShare, ...]
    top5_share: float
    top10_share: float
    effective_holdings: float | None
    overlaps: tuple[tuple[str, str, float], ...]


def _issuer_weights(snapshot: Snapshot,
                    directory: CompanyDirectory, *,
                    allow_partial: bool = False) -> dict[str, float]:
    """Combine validated stock rows by name-backed issuer, excluding unknowns.

    A missing stock weight raises ValueError instead of filling it with zero.
    Snapshots must otherwise have passed the shared snapshot validator.
    """
    weights: dict[str, list[float]] = {}
    holdings = (merge_duplicate_holdings(snapshot.holdings)
                if allow_partial else snapshot.holdings)
    for holding in holdings:
        if holding.kind != "stock":
            continue
        if holding.weight is None:
            if allow_partial:
                continue
            raise ValueError(f"Missing stock weight: {holding.code}")
        issuer = directory.resolve(holding.code, holding.name).issuer_code
        if issuer is not None:
            weights.setdefault(issuer, []).append(holding.weight)
    return {issuer: fsum(values) for issuer, values in weights.items()}


def _overlap(a: dict[str, float], b: dict[str, float]) -> float:
    """Sum common issuer minima in deterministic code order."""
    return fsum(min(a[code], b[code]) for code in sorted(a.keys() & b.keys()))


def pairwise_overlap(a: Snapshot, b: Snapshot,
                     directory: CompanyDirectory) -> float:
    """Return issuer-merged stock overlap of two validated ETF snapshots.

    Common and verified preferred securities merge before taking minima.
    Unknown stocks and nonstocks contribute no overlap; the identified
    portion is never rescaled to a full ETF portfolio. No inputs are mutated.
    """
    return _overlap(_issuer_weights(a, directory),
                    _issuer_weights(b, directory))


def compute_metrics(result: LookthroughResult,
                    directory: CompanyDirectory) -> Metrics:
    """Compute metrics from a validated lookthrough result without I/O.

    Industries and top shares use total portfolio value, including gaps and
    unprocessed positions. Missing directory entries or blank sector names
    map to unknown. Industry order is descending amount, then sector code.
    Only processed ETF pairs appear, ordered lexicographically by symbol.
    Effective count scales amounts internally to avoid squaring overflow or
    underflow; absent or all-zero identified amounts return None.
    """
    sectors: dict[tuple[str, str], list[float]] = {}
    for exposure in result.companies:
        company = directory.get(exposure.issuer_code)
        sector = ((company.industry_code, company.industry_name)
                  if company and company.industry_name.strip()
                  else ("unknown", "未對應"))
        sectors.setdefault(sector, []).append(exposure.amount)
    industries = []
    for (code, name), values in sectors.items():
        amount = fsum(values)
        industries.append(IndustryShare(
            code, name, amount,
            amount / result.total_value if result.total_value else 0.0))

    amounts = sorted((row.amount for row in result.companies), reverse=True)
    effective = None
    if amounts and amounts[0] > 0:
        scaled = [amount / amounts[0] for amount in amounts]
        effective = fsum(scaled) ** 2 / fsum(value ** 2 for value in scaled)

    weights = {
        position.symbol: _issuer_weights(position.snapshot, directory,
                                         allow_partial=True)
        for position in result.positions
        if position.kind == "etf" and position.status == "looked_through"
        and position.snapshot is not None
    }
    overlaps = tuple((a, b, _overlap(weights[a], weights[b]))
                     for a, b in combinations(sorted(weights), 2))
    total = result.total_value
    return Metrics(
        tuple(sorted(industries, key=lambda row: (-row.amount,
                                                  row.industry_code))),
        fsum(amounts[:5]) / total if total else 0.0,
        fsum(amounts[:10]) / total if total else 0.0,
        effective, overlaps,
    )
