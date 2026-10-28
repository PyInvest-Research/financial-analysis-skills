"""Provide synthetic ETF fixtures and imports for standalone skill tests."""

from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lookthrough.models import Holding, Snapshot  # noqa: E402


def make_snapshot() -> Snapshot:
    """Return a synthetic snapshot; it is not actual ETF holdings data."""
    return Snapshot(
        schema_version="1",
        etf_symbol="ETFA",
        issuer="Synthetic Issuer",
        is_active=False,
        holdings_date=date(2026, 10, 22),
        fetched_at="2026-10-23T00:00:00+00:00",
        source_url="https://example.com/etfa/holdings",
        source_kind="official",
        holdings=(
            Holding("2330", "台積電", "stock", 0.6),
            Holding("2317", "鴻海", "stock", 0.2),
            Holding("2454", "聯發科", "stock", 0.15),
            Holding("TXFK6", "臺股期貨", "futures", None, 0.03),
            Holding("TWD", "現金", "cash", 0.04),
        ),
    )
