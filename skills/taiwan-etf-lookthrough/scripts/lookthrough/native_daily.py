"""Qualify native daily routes while preserving fixed historical hashes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
from typing import TYPE_CHECKING

from . import allianz_acquire
from .issuers import allianz, fubon_global
from .models import Snapshot
from .transport_evidence import TransportEvidence

if TYPE_CHECKING:
    from .issuers.registry import SupportedEtf
    from .fubon_global_acquire import SourceSession


DATA = Path(__file__).parent / 'data/native-daily-products.json'
SYMBOLS = frozenset({'00984A', '00993A', '00903', '00920'})


@lru_cache(maxsize=1)
def products() -> dict[str, dict]:
    """Read enabled routes while preserving historical candidate catalogs."""
    rows = json.loads(DATA.read_text())['products']
    if not set(rows) <= SYMBOLS:
        raise ValueError('Unreviewed native daily product')
    for symbol, row in rows.items():
        issuer = 'allianz' if symbol.endswith('A') else 'fubon'
        expected = (allianz.SOURCE_URL if issuer == 'allianz'
                    else fubon_global.FUNDS[symbol][1])
        if (row['issuer'] != issuer or row['source_url'] != expected
                or row['is_active'] is not (issuer == 'allianz')
                or type(row['enabled']) is not bool
                or type(row['source_acceptance']['run_id']) is not int):
            raise ValueError('Invalid native daily source qualification')
        date.fromisoformat(row['first_holdings_date'])
        stamp = datetime.fromisoformat(row['accepted_after'])
        if stamp.utcoffset() != timedelta(0):
            raise ValueError('Native acceptance timestamp must be UTC')
    return {symbol: row for symbol, row in rows.items() if row['enabled']}


def qualifies(snapshot: Snapshot) -> bool:
    """Accept qualified current data; preserve fixed historical hash checks."""
    from .refresh_contract import resolve_security

    row = products().get(snapshot.etf_symbol)
    if row is None:
        return False
    try:
        acquired = datetime.fromisoformat(snapshot.fetched_at)
        if (snapshot.schema_version != '2'
                or snapshot.issuer != row['issuer']
                or snapshot.is_active is not row['is_active']
                or snapshot.source_url != row['source_url']
                or type(snapshot.holdings_date) is not date
                or snapshot.holdings_date < date.fromisoformat(
                    row['first_holdings_date'])
                or acquired.utcoffset() != timedelta(0)
                or acquired < datetime.fromisoformat(row['accepted_after'])):
            return False
        equities = [h for h in snapshot.holdings
                    if h.kind in ('stock', 'foreign_stock')]
        if not equities or snapshot.equity_evidence is None:
            return False
        for holding in equities:
            code, market = resolve_security(snapshot.etf_symbol,
                                            holding.code, holding.name)
            kind = ('stock' if market in ('TWSE', 'TPEx')
                    else 'foreign_stock')
            if (code != holding.code or market != holding.market
                    or kind != holding.kind):
                return False
    except (KeyError, ValueError, TypeError):
        return False
    return True


def parse(raw: bytes, etf: 'SupportedEtf', fetched_at: str) -> Snapshot:
    """Parse only qualified current responses and reject stale source dates."""
    row = products().get(etf.symbol)
    if (row is None or (etf.issuer, etf.is_active, etf.route, etf.source_url)
            != (row['issuer'], row['is_active'], 'http', row['source_url'])):
        raise ValueError('Unqualified native daily descriptor')
    parser = allianz.parse if etf.issuer == 'allianz' else fubon_global.parse
    snapshot = (parser(raw, etf, fetched_at) if etf.issuer == 'allianz'
                else parser(raw, etf, fetched_at, daily=True))
    stamp = datetime.fromisoformat(fetched_at)
    age = (stamp.astimezone(timezone(timedelta(hours=8))).date()
           - snapshot.holdings_date).days
    if not 0 <= age <= 3 or not qualifies(snapshot):
        raise ValueError('Native daily source date or market is unqualified')
    return snapshot


class NativeSession:
    """Share issuer failure stops and bounded acquisition windows."""

    def __init__(self, transport: TransportEvidence | None = None) -> None:
        """Start without any source request or change to process trust."""
        self.blocked: set[str] = set()
        self.transport = transport or TransportEvidence()
        self.fubon: SourceSession | None = None
        self.allianz_deadline: str | None = None
        self.events: list[dict] = []

    def fetch(self, etf: 'SupportedEtf') -> bytes:
        """Fetch a reviewed native route; any failure stops that issuer."""
        from .fubon_global_acquire import SourceSession

        row = products().get(etf.symbol)
        identity = (etf.issuer, etf.is_active, etf.route, etf.source_url)
        if (row is None or identity != (row['issuer'], row['is_active'],
                                        'http', row['source_url'])):
            raise ValueError('Unqualified native daily acquisition')
        if etf.issuer in self.blocked:
            raise ValueError('Native issuer stopped after source failure')
        try:
            allianz_acquire.checked_context()
            if etf.issuer == 'allianz':
                if self.allianz_deadline is None:
                    self.allianz_deadline = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=200)).isoformat()
                raw = allianz_acquire.acquire(
                    etf, deadline_utc=self.allianz_deadline,
                    events=self.events)
            else:
                if self.fubon is None:
                    self.fubon = SourceSession(self.transport)
                raw = self.fubon.get(etf.symbol)
            return raw
        except Exception:
            self.blocked.add(etf.issuer)
            if etf.issuer == 'allianz':
                for event in self.events:
                    status = event.get('http_status')
                    path = event.get('path')
                    allowed = {allianz_acquire.PREFIX + value
                               for value in allianz_acquire.PATHS}
                    if status in (401, 403, 429) and path in allowed:
                        url = 'https://' + allianz_acquire.HOST + path
                        self.transport.denials.record(url, status)
            raise
