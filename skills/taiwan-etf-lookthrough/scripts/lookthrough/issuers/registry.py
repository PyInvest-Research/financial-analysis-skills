"""Keep the partial supported universe limited to verified source routes."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..models import Snapshot
from .capital import SOURCE_URL as CAPITAL_SOURCE_URL, parse as parse_capital
from .cathay import parse as parse_cathay
from .fubon import parse as parse_fubon
from .yuanta import parse as parse_yuanta


@dataclass(frozen=True)
class SupportedEtf:
    """Describe a verified route; is_active means active management."""

    symbol: str
    issuer: Literal["yuanta", "fubon", "cathay", "capital"]
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

Parser = Callable[[bytes, SupportedEtf, str], Snapshot]
PARSERS: dict[str, Parser] = {
    "fubon": parse_fubon, "cathay": parse_cathay, "yuanta": parse_yuanta,
    "capital": parse_capital,
}


def get_supported(symbol: str) -> SupportedEtf | None:
    """Return a verified seed entry or None; this is not a full universe."""
    return next((row for row in SUPPORTED_ETFS if row.symbol == symbol), None)
