"""Refresh qualified portfolios with bounded shared official price reads."""

import base64
from datetime import date, datetime
import json
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)
from zoneinfo import ZoneInfo

from .issuers import ftft, jpmorgan, mixed
from .issuers._assets import read_json
from .models import Snapshot
from .refresh_contract import products
from .refresh_inventory import (
    Inventory, read_cathay, read_kgi, read_mega, read_sinopac, read_tsit,
    read_union,
)
from .refresh_prices import price_url, read_prices
from .refresh_snapshot import build_snapshot, inventory_markets
from .refresh_workbooks import (
    fh_download, read_fhtrust, read_ftft, read_jpmorgan,
)

if TYPE_CHECKING:
    from .issuers.registry import SupportedEtf


MAX_BYTES = 10 * 1024 * 1024
HOSTS = frozenset({
    "cwapi.cathaysite.com.tw", "www.kgifund.com.tw", "www.megafunds.com.tw",
    "sitc.sinopac.com", "www.tsit.com.tw", "www.usitc.com.tw",
    "www.fhtrust.com.tw", "www.ftft.com.tw", "am.jpmorgan.com",
    "www.twse.com.tw", "www.tpex.org.tw",
})


class NoRedirect(HTTPRedirectHandler):
    """Reject redirects before sending any follow-up request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Keep all requests on their exact qualified official endpoints."""
        raise ValueError("Official refresh redirects are forbidden")


class RefreshSession:
    """Share prices and failure stops for one sequential publication run.

    Trust uses the job's preverified default CA context. No cookies, auth,
    proxy environment, scripts or automatic retry are used. At most 100
    requests, 80 MiB and 240 seconds are spent on the additional products.
    """

    def __init__(self) -> None:
        """Start an empty bounded source cache and observation ledger."""
        self.started = monotonic()
        self.requests = 0
        self.bytes = 0
        self.blocked: set[str] = set()
        self.quotes: dict[tuple[str, date], bytes | None] = {}
        self.events: list[dict] = []

    def get(self, url: str, form: dict | None = None) -> bytes:
        """Fetch an exact allowlisted HTTPS URL or a public query form."""
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme != "https" or host not in HOSTS
                or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)
                or any(ord(c) < 32 for c in url)):
            raise ValueError("Unqualified official HTTPS refresh endpoint")
        if host in self.blocked:
            raise ValueError("Official host stopped after transport failure")
        if (self.requests >= 100 or self.bytes >= 80 * 1024**2
                or monotonic() - self.started >= 240):
            raise ValueError("Official refresh resource budget exhausted")
        self.requests += 1
        body = urlencode(form).encode() if form is not None else None
        request = Request(url, data=body, headers={
            "User-Agent": "PyInvest-ETF-Lookthrough/1.5.0",
            **({"Content-Type": "application/x-www-form-urlencoded"}
               if body is not None else {}),
        })
        event = dict(host=host, status="failed")
        self.events.append(event)
        try:
            opener = build_opener(ProxyHandler({}), NoRedirect())
            remaining = 240 - (monotonic() - self.started)
            with opener.open(request, timeout=min(20, max(1, remaining))) as r:
                if r.geturl() != url or r.getcode() != 200:
                    raise ValueError("Changed official URL/status")
                raw = r.read(MAX_BYTES + 1)
                self.bytes += len(raw)
                if len(raw) > MAX_BYTES or self.bytes > 80 * 1024**2:
                    raise ValueError("Official source response byte limit")
            event.update(status="ok", bytes=len(raw))
            return raw
        except Exception as error:
            self.blocked.add(host)
            event.update(error=type(error).__name__,
                         http_status=getattr(error, "code", None))
            raise

    def price(self, market: str, day: date) -> bytes | None:
        """Cache one date/market request, including failure; never retry it."""
        key = (market, day)
        if key not in self.quotes:
            self.quotes[key] = None
            try:
                raw = self.get(price_url(market, day))
                read_prices(raw, market, day)
                self.quotes[key] = raw
            except (OSError, ValueError):
                # Missing prices produce unknown row weights, not zeroes.
                pass
        return self.quotes[key]


def _inventory(symbol: str, sources: dict[str, bytes]) -> Inventory:
    """Read only original captured source documents without network I/O."""
    product = products()[symbol]
    issuer, fund = product["issuer"], product["fund_id"]
    if issuer == "cathay":
        return read_cathay(sources["assets"], sources["stocks"],
                           sources["balance"], mixed.assets_day(
                               sources["assets"]),
                           native_nav=symbol == "00878")
    if issuer == "fhtrust":
        return read_fhtrust(sources["page"], sources["workbook"], symbol, fund)
    if issuer == "ftft":
        inventory = read_ftft(sources["assets"], symbol, fund)
        if inventory.day != ftft.latest_day(sources["dates"]):
            raise ValueError("FTFT returned an unexpected asset date")
        return inventory
    if issuer == "jpmorgan":
        inventory = read_jpmorgan(sources["workbook"])
        if inventory.day != jpmorgan.latest_day(sources["dates"]):
            raise ValueError("JPMorgan returned an unexpected asset date")
        return inventory
    if issuer == "mega":
        return read_mega(sources["page"], symbol, fund)
    reader = {"kgi": read_kgi, "tsit": read_tsit,
              "sinopac": read_sinopac, "union": read_union}[issuer]
    return reader(sources["page"], symbol)


def capture(symbol: str, sources: dict[str, bytes],
            quotes: dict[str, bytes]) -> bytes:
    """Bind raw source bytes and same-day quote bytes to one product."""
    def encoded(values: dict[str, bytes]) -> dict[str, str]:
        """Encode source bytes without executing embedded content."""
        return {key: base64.b64encode(value).decode()
                for key, value in values.items()}
    return json.dumps(dict(refresh_version=1, symbol=symbol,
                           sources=encoded(sources), quotes=encoded(quotes)),
                      separators=(",", ":"), allow_nan=False).encode()


def acquire(symbol: str, session: RefreshSession) -> bytes:
    """Collect the reviewed current source and reuse date-specific prices."""
    product = products()[symbol]
    issuer, fund = product["issuer"], product["fund_id"]
    sources = {}
    if issuer == "cathay":
        sources["assets"] = session.get(product["source_url"])
        day = mixed.assets_day(sources["assets"])
        for key, endpoint in (("stocks", "GetETFDetailStockList"),
                              ("balance", "GetETFDetailBalList")):
            sources[key] = session.get(mixed.API + endpoint +
                                       f"?FundCode={fund}&SearchDate={day}")
    elif issuer == "fhtrust":
        sources["page"] = session.get(product["source_url"])
        _, url = fh_download(sources["page"], fund)
        sources["workbook"] = session.get(url)
    elif issuer == "ftft":
        sources["dates"] = session.get(product["source_url"])
        day = ftft.latest_day(sources["dates"])
        sources["assets"] = session.get(
            f"https://www.ftft.com.tw/official/api/etf/shares/{fund}"
            f"?date={day:%Y%m%d}")
    elif issuer == "jpmorgan":
        url = jpmorgan.SOURCE_URL.replace("TW00000401A1", fund)
        sources["dates"] = session.get(url)
        day = jpmorgan.latest_day(sources["dates"])
        sources["workbook"] = session.get(jpmorgan.DOWNLOAD_URL.replace(
            "TW00000401A1", fund) + day.isoformat())
    else:
        form = None
        if issuer == "union":
            # Query clock is never substituted for the returned asset date.
            form = dict(FundNo=symbol, sDate=str(datetime.now(
                ZoneInfo("Asia/Taipei")).date()))
        sources["page"] = session.get(product["source_url"], form)
    inventory = _inventory(symbol, sources)
    quotes = {}
    for market in sorted(inventory_markets(symbol, inventory)):
        raw = session.price(market, inventory.day)
        if raw is not None:
            quotes[market] = raw
    return capture(symbol, sources, quotes)


def parse(raw: bytes, etf: "SupportedEtf", fetched_at: str) -> Snapshot:
    """Parse a bounded official capture; partial weights remain unknown."""
    payload = read_json(raw, limit=32 * 1024**2)
    if (payload.get("refresh_version") != 1
            or payload.get("symbol") != etf.symbol
            or etf.symbol not in products()
            or etf.source_url != products()[etf.symbol]["source_url"]):
        raise ValueError("Refresh product capture identity mismatch")

    def decoded(key: str) -> dict[str, bytes]:
        """Decode bounded raw blobs while preserving their hashes."""
        values = payload[key]
        if not isinstance(values, dict) or len(values) > 8:
            raise ValueError("Invalid refresh source collection")
        result = {k: base64.b64decode(v, validate=True)
                  for k, v in values.items()}
        if any(not 0 < len(v) <= MAX_BYTES for v in result.values()):
            raise ValueError("Invalid refresh source size")
        return result
    sources, quotes = decoded("sources"), decoded("quotes")
    return build_snapshot(etf.symbol, _inventory(etf.symbol, sources),
                          list(sources.values()), quotes, fetched_at)
