"""Keep the partial supported universe limited to verified source routes."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..models import Snapshot
from .capital import SOURCE_URL as CAPITAL_SOURCE_URL, parse as parse_capital
from .cathay import parse as parse_cathay
from .fubon import parse as parse_fubon
from .fhtrust import SOURCE_URL as FH_SOURCE_URL, parse as parse_fhtrust
from .ftft import SOURCE_URL as FTFT_SOURCE_URL, parse as parse_ftft
from .yuanta import parse as parse_yuanta
from . import (
    ab, esun, fsitc, jpmorgan, kgi, mega, mixed, nomura, sinopac, tsit, union,
)


@dataclass(frozen=True)
class SupportedEtf:
    """Describe a verified route; is_active means active management."""

    symbol: str
    issuer: Literal[
        "yuanta", "fubon", "cathay", "capital", "fhtrust", "ftft", "sinopac",
        "tsit", "mega", "nomura", "esun", "fsitc", "jpmorgan", "ab",
        "kgi", "union"]
    is_active: bool
    route: Literal["http", "playwright", "official_only"]
    source_url: str


SUPPORTED_ETFS: tuple[SupportedEtf, ...] = tuple(
    SupportedEtf(
        symbol, "fubon", active, "http",
        "https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx"
        f"?lan=TW&stkId={symbol}",
    )
    for symbol, active in (
        ("0052", False), ("00405A", True), ("006208", False),
        ("0057", False), ("00692", False), ("00730", False),
        ("00733", False), ("00892", False), ("00900", False),
        ("009802", False), ("009809", False),
    )
)
SUPPORTED_ETFS += (
    SupportedEtf("0050", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F0050%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName=E"
                 "TF&Device=3&Platform=ETF&ticker=0050"),
    SupportedEtf("0051", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F0051%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName=E"
                 "TF&Device=3&Platform=ETF&ticker=0051"),
    SupportedEtf("0053", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F0053%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName=E"
                 "TF&Device=3&Platform=ETF&ticker=0053"),
    SupportedEtf("0055", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F0055%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName=E"
                 "TF&Device=3&Platform=ETF&ticker=0055"),
    SupportedEtf("0056", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F0056%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName=E"
                 "TF&Device=3&Platform=ETF&ticker=0056"),
    SupportedEtf("006201", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F006201%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName"
                 "=ETF&Device=3&Platform=ETF&ticker=006201"),
    SupportedEtf("006203", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F006203%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName"
                 "=ETF&Device=3&Platform=ETF&ticker=006203"),
    SupportedEtf("00713", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F00713%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName="
                 "ETF&Device=3&Platform=ETF&ticker=00713"),
    SupportedEtf("00850", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F00850%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName="
                 "ETF&Device=3&Platform=ETF&ticker=00850"),
    SupportedEtf("00940", "yuanta", False, "http",
                 "https://etfapi.yuantaetfs.com/ectranslation/api/bridge?APITy"
                 "pe=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2Fproduct%2Fdeta"
                 "il%2F00940%2Fratio&DeviceId=null&FuncId=PCF%2FDaily&AppName="
                 "ETF&Device=3&Platform=ETF&ticker=00940"),
)

SUPPORTED_ETFS += tuple(
    SupportedEtf(symbol, issuer, False, "http", url)
    for symbol, (issuer, url) in mixed.PRODUCTS.items()
)

SUPPORTED_ETFS += tuple(
    SupportedEtf(symbol, issuer, active, route, module.SOURCE_URL)
    for symbol, issuer, active, route, module in (
        ("00888", "sinopac", False, "official_only", sinopac),
        ("00936", "tsit", False, "http", tsit),
        ("00932", "mega", False, "http", mega),
        ("00935", "nomura", False, "official_only", nomura),
        ("009803", "esun", False, "http", esun),
        ("00728", "fsitc", False, "http", fsitc),
        ("00401A", "jpmorgan", True, "http", jpmorgan),
        ("00404A", "ab", True, "http", ab),
        ("00915", "kgi", False, "official_only", kgi),
        ("009804", "union", False, "official_only", union),
    )
)


SUPPORTED_ETFS += tuple(
    SupportedEtf(symbol, "capital", active, "http", CAPITAL_SOURCE_URL)
    for symbol, active in (
        ("00919", False), ("00982A", True), ("00415A", True),
        ("00923", False), ("00927", False), ("00946", False),
        ("00992A", True),
    )
)


SUPPORTED_ETFS += tuple(
    SupportedEtf(
        symbol, "cathay", active, "official_only",
        f"https://www.cathaysite.com.tw/fund-details/{route_id}?tab=portfolio",
    )
    for symbol, route_id, active in (
        ("00701", "EAD", False), ("00881", "ECR", False),
        ("00922", "EDO", False), ("00878", "ECN", False),
        ("00400A", "EEA", True),
    )
)

SUPPORTED_ETFS += (
    SupportedEtf("00929", "fhtrust", False, "http", FH_SOURCE_URL),
    SupportedEtf("00905", "ftft", False, "http", FTFT_SOURCE_URL),
)

Parser = Callable[[bytes, SupportedEtf, str], Snapshot]
PARSERS: dict[str, Parser] = {
    "fubon": parse_fubon, "cathay": parse_cathay, "yuanta": parse_yuanta,
    "capital": parse_capital,
    "fhtrust": parse_fhtrust, "ftft": parse_ftft,
    **{name: module.parse for name, module in (
        ("sinopac", sinopac), ("tsit", tsit), ("mega", mega),
        ("nomura", nomura), ("esun", esun), ("fsitc", fsitc),
        ("jpmorgan", jpmorgan), ("ab", ab), ("kgi", kgi), ("union", union))},
}


def _mixed_dispatch(legacy: Parser) -> Parser:
    """Qualify only the four reviewed symbols before legacy issuer parsing."""
    def parse(raw: bytes, etf: SupportedEtf, fetched_at: str) -> Snapshot:
        """Preserve every existing source contract and parser unchanged."""
        if etf.symbol in mixed.PRODUCTS:
            return mixed.parse(raw, etf, fetched_at)
        return legacy(raw, etf, fetched_at)
    return parse


for _issuer in ("mega", "kgi", "cathay"):
    PARSERS[_issuer] = _mixed_dispatch(PARSERS[_issuer])


def get_supported(symbol: str) -> SupportedEtf | None:
    """Return a verified seed entry or None; this is not a full universe."""
    return next((row for row in SUPPORTED_ETFS if row.symbol == symbol), None)
