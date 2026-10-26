"""Parse official company rosters offline and explicitly refresh online."""

import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Literal
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


# TWSE industry table; TPEx-only categories use TPEx's official table.
# See references/methodology.md for the primary sources and unmapped codes.
INDUSTRY_NAMES: dict[str, str] = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療業",
    "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "29": "電子通路業", "30": "資訊服務業", "31": "其他電子",
    "32": "文化創意業", "33": "農業科技業",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}
ROSTER_URLS = {
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "TPEx": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
}
_OFFICIAL_HOSTS = frozenset({
    "openapi.twse.com.tw", "www.tpex.org.tw", "mopsfin.twse.com.tw",
})


@dataclass(frozen=True)
class Company:
    """Store a roster company using its official Chinese abbreviation."""

    code: str
    name: str
    market: Literal["TWSE", "TPEx"]
    industry_code: str
    industry_name: str


@dataclass(frozen=True)
class Resolution:
    """Keep the original security identity and any resolved issuer code."""

    security_code: str
    issuer_code: str | None
    security_type: Literal["common", "preferred", "unresolved"]


class CompanyDirectory:
    """Index company identities without performing any network operations."""

    def __init__(self, companies: Iterable[Company], as_of: str,
                 sources: Iterable[dict[str, Any]] = ()) -> None:
        """Validate identities and reject ambiguous conflicting codes.

        ``as_of`` is the ISO retrieval date, not a claim that both exchanges
        published their rosters on that date. Source dates stay in provenance.
        """
        if not isinstance(as_of, str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of):
            raise ValueError("Directory as_of must be an ISO date")
        date.fromisoformat(as_of)
        self.as_of = as_of
        self.sources = tuple(dict(source) for source in sources)
        self._companies: dict[str, Company] = {}
        for company in companies:
            if (not isinstance(company.code, str)
                    or not re.fullmatch(r"[0-9]{4,6}", company.code)
                    or not isinstance(company.name, str)
                    or not company.name.strip()
                    or company.market not in ROSTER_URLS
                    or not isinstance(company.industry_code, str)
                    or not isinstance(company.industry_name, str)):
                raise ValueError("Invalid company identity")
            previous = self._companies.get(company.code)
            if previous is not None and previous != company:
                raise ValueError(f"Conflicting company code: {company.code}")
            self._companies[company.code] = company

    @classmethod
    def from_file(cls, path: Path | str) -> "CompanyDirectory":
        """Read a normalized saved directory offline; reject invalid schema."""
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        try:
            if (not isinstance(payload, dict)
                    or payload.get("schema_version") != "1"
                    or not isinstance(payload.get("companies"), list)):
                raise ValueError("Unsupported directory schema")
            companies = [Company(**row) for row in payload["companies"]]
            return cls(companies, payload["as_of"], payload.get("sources", []))
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid saved company directory") from error

    def get(self, code: str) -> Company | None:
        """Return an exact company-code match, or None when absent."""
        return self._companies.get(code)

    def resolve(self, security_code: str, security_name: str) -> Resolution:
        """Resolve direct matches or name-backed four-digit preferred shares.

        Codes and names are not normalized here. A preferred security must
        have one uppercase ASCII suffix and start with the issuer abbreviation.
        No ETF lookthrough or fuzzy name matching is attempted.
        """
        if self.get(security_code) is not None:
            return Resolution(security_code, security_code, "common")
        if re.fullmatch(r"[0-9]{4}[A-Z]", security_code):
            issuer = self.get(security_code[:4])
            if issuer is not None and security_name.startswith(issuer.name):
                return Resolution(security_code, issuer.code, "preferred")
        return Resolution(security_code, None, "unresolved")

    def _payload(self) -> dict[str, Any]:
        """Serialize in deterministic company-code order for local storage."""
        return {
            "schema_version": "1", "as_of": self.as_of,
            "sources": list(self.sources),
            "companies": [asdict(self._companies[code])
                          for code in sorted(self._companies)],
        }


def _field(row: dict[str, Any], chinese: str, english: str) -> str:
    """Read a required official field without inventing missing identities."""
    value = row.get(chinese, row.get(english))
    if not isinstance(value, str):
        raise ValueError(f"Missing or invalid roster field: {english}")
    return value.strip()


def parse_directory(twse_rows: list[dict], tpex_rows: list[dict],
                    as_of: str) -> CompanyDirectory:
    """Parse Chinese/English official roster fields entirely offline.

    Missing identity fields or conflicting codes raise ValueError. Unknown
    industry codes remain intact with an empty industry_name; they are never
    silently reassigned to another industry.
    """
    companies: list[Company] = []
    for market, rows in (("TWSE", twse_rows), ("TPEx", tpex_rows)):
        if not isinstance(rows, list):
            raise ValueError("Roster must be a list")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Roster row must be an object")
            industry = _field(row, "產業別", "SecuritiesIndustryCode")
            companies.append(Company(
                _field(row, "公司代號", "SecuritiesCompanyCode"),
                _field(row, "公司簡稱", "CompanyAbbreviation"),
                market, industry, INDUSTRY_NAMES.get(industry, ""),
            ))
    return CompanyDirectory(companies, as_of)


def _check_official_url(url: str) -> None:
    """Reject non-HTTPS destinations, credentials and unapproved hosts."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in _OFFICIAL_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError("Directory URL is not an approved official endpoint")


class _OfficialRedirect(HTTPRedirectHandler):
    """Validate each redirect before urllib makes the next request."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        """Allow redirects only within the explicit official HTTPS hosts."""
        _check_official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str) -> tuple[bytes, str]:
    """Fetch official bytes with default verified TLS and a bounded timeout.

    Operators must first follow references/runtime-safety.md for their actual
    interpreter and CA store. No account, cookies or TLS bypass is used.
    """
    _check_official_url(url)
    request = Request(url, headers={
        "User-Agent": "PyInvest-ETF-Lookthrough/1.0.0",
    })
    opener = build_opener(_OfficialRedirect())
    with opener.open(request, timeout=45) as response:
        final_url = response.geturl()
        _check_official_url(final_url)
        return response.read(), final_url


def _decode(raw: bytes) -> str:
    """Decode official UTF-8/BOM or cp950 CSV without ignoring bad bytes."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp950")


def _read_roster(market: str) -> tuple[list[dict], dict[str, Any]]:
    """Try the official API, then the corresponding official MOPS CSV."""
    url = ROSTER_URLS[market]
    try:
        raw, final_url = _fetch(url)
        rows = json.loads(_decode(raw))
        parsed = parse_directory(rows if market == "TWSE" else [],
                                 rows if market == "TPEx" else [],
                                 date.today().isoformat())
        if not parsed._companies:
            raise ValueError("Official roster is empty")
    except (URLError, TimeoutError, ValueError, UnicodeError):
        suffix = "L" if market == "TWSE" else "O"
        url = f"https://mopsfin.twse.com.tw/opendata/t187ap03_{suffix}.csv"
        raw, final_url = _fetch(url)
        rows = list(csv.DictReader(io.StringIO(_decode(raw))))
        parsed = parse_directory(rows if market == "TWSE" else [],
                                 rows if market == "TPEx" else [],
                                 date.today().isoformat())
        if not parsed._companies:
            raise ValueError("Official fallback roster is empty")
    return rows, {
        "market": market, "url": final_url,
        "sha256": hashlib.sha256(raw).hexdigest(), "row_count": len(rows),
        "source_dates": sorted({str(row.get("出表日期", row.get("Date")))
                                for row in rows
                                if row.get("出表日期", row.get("Date"))}),
    }


def refresh_directory(out_path: Path) -> CompanyDirectory:
    """Fetch both official rosters and atomically save a normalized snapshot.

    Network, decode, schema or I/O errors propagate; an incomplete refresh
    never overwrites the previous snapshot. Raw contact/person fields are
    discarded. Provenance retains URLs, raw response hashes and source dates.
    """
    twse_rows, twse_source = _read_roster("TWSE")
    tpex_rows, tpex_source = _read_roster("TPEx")
    fetched_at = datetime.now(timezone.utc).isoformat()
    directory = parse_directory(twse_rows, tpex_rows, fetched_at[:10])
    for source in (twse_source, tpex_source):
        source["fetched_at"] = fetched_at
    directory.sources = (twse_source, tpex_source)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=out_path.parent,
                prefix=f".{out_path.name}.", suffix=".tmp",
                delete=False) as file:
            temporary = Path(file.name)
            json.dump(directory._payload(), file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, out_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return directory
