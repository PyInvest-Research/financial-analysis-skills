"""Share exact mixed product and equity qualification across all entries."""

from datetime import date
import json
from pathlib import Path

from .models import Snapshot


PRODUCTS = {
    '00982T': ('mega', 'https://www.megafunds.com.tw/MEGA/etf/'
               'etf_product.aspx?id=22'),
    '00981T': ('kgi', 'https://www.kgifund.com.tw/Fund/Detail?fundID=J022'),
    '00980T': ('kgi', 'https://www.kgifund.com.tw/Fund/Detail?fundID=J021'),
    '00735': ('cathay', 'https://www.cathaysite.com.tw/fund-details/EAW'),
}
MARKETS = json.loads((Path(__file__).parent / 'data/mixed-markets.json')
                     .read_text())['products']


def qualifies_mixed(snapshot: Snapshot) -> bool:
    """Require qualified product, exact stock set and official market proof.

    Raw parsers and normalized local/remote snapshots share this contract.
    Self-declared completeness and matching aggregate sums are insufficient.
    """
    if (snapshot.schema_version != '2' or snapshot.is_active
            or snapshot.etf_symbol not in PRODUCTS
            or (snapshot.issuer, snapshot.source_url)
            != PRODUCTS[snapshot.etf_symbol]
            or type(snapshot.holdings_date) is not date):
        return False
    expected = MARKETS[snapshot.etf_symbol]
    equities = [h for h in snapshot.holdings
                if h.kind in ('stock', 'foreign_stock')]
    if (len(equities) != len(expected)
            or {h.code for h in equities} != set(expected)):
        return False
    for holding in equities:
        row = expected[holding.code]
        kind = ('stock' if row['market'] in ('TWSE', 'TPEx')
                else 'foreign_stock')
        if (holding.name != row['name'] or holding.market != row['market']
                or holding.kind != kind):
            return False
        listed = row['listing_date']
        if listed is not None and date.fromisoformat(listed) > (
                snapshot.holdings_date):
            return False
    return True
