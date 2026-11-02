"""Verify reviewed dated Cathay captures before reusing the existing reader.

This module has no network entry point. Default acquisition excludes candidates.
Only the reviewed dated Snapshot digest qualifies; new bytes need fresh review.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import re
from zoneinfo import ZoneInfo

from .issuers._assets import number, read_json
from .refresh_contract import candidate_products
from .refresh_inventory import Inventory, read_cathay


SOURCE_KEYS = ('directory', 'info', 'assets', 'stocks', 'balance', 'receipts')
API = 'https://cwapi.cathaysite.com.tw/api/ETF/'


def source_keys(symbol: str) -> tuple[str, ...]:
    """Keep five-request sources; bind two exact four-request collections."""
    contract = candidate_products()[symbol].get('source_contract')
    if contract == 'cathay_info_four_originals_v1':
        if symbol not in ('00881', '00922'):
            raise ValueError('Unreviewed four-original candidate')
        return ('info', 'assets', 'stocks', 'balance', 'receipts')
    if contract is not None:
        raise ValueError('Unknown candidate source contract')
    return SOURCE_KEYS


def _response(raw: bytes):
    value = read_json(raw)
    if value.get('success') is not True or value.get('returnCode') != '2000':
        raise ValueError('Cathay candidate business response failed')
    return value


def _utc(text: str | None) -> datetime:
    if not isinstance(text, str):
        raise ValueError('Missing actual acquisition timestamp')
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError('Invalid acquisition timestamp') from error
    if stamp.utcoffset() != timedelta(0):
        raise ValueError('Acquisition timestamp must explicitly use UTC')
    return stamp


def read_inventory(sources: dict[str, bytes], fetched_at: str | None, *,
                   symbol: str = '00830') -> Inventory:
    """Require actual own-fund identity/date, full stock set and NAV basis.

    Unknown foreign-currency cash/margin do not invalidate stock NAV percentages.
    No FX, price substitution, residual fill or voting-class guess is used.
    """
    keys = source_keys(symbol)
    if set(sources) != set(keys):
        raise ValueError('Missing candidate source collection')
    product = candidate_products()[symbol]
    fund = product['fund_id']
    stamp = _utc(fetched_at)
    info = _response(sources['info'])['result']
    identities = [info]
    if 'directory' in sources:
        directory = _response(sources['directory'])
        if (directory.get('totalCount') != 1 or directory.get('totalPage') != 1
                or len(directory['result']) != 1):
            raise ValueError('Ambiguous individual fund directory')
        entry = directory['result'][0]
        if (entry.get('isListedTrade') is not True
                or entry['fundName'] != info['fundName']):
            raise ValueError('Candidate listing/name mismatch')
        identities.append(entry)
    else:
        # These exact originals already have independently reviewed exchange
        # identities. Never fabricate a fifth response or accept arbitrary
        # bytes.
        hashes = product['source_sha256']
        if (set(hashes) != set(keys) - {'receipts'}
                or any(hashlib.sha256(sources[k]).hexdigest() != hashes[k]
                       for k in hashes)
                or info['fundName'] != product['fund_name']):
            raise ValueError(
                'Reviewed four-original source hash/name mismatch')
    for obj in identities:
        if (obj.get('fundCode') != fund or obj.get('stockCode') != symbol
                or obj.get('isETF') is not True or obj.get('currency') != '新台幣'):
            raise ValueError('Wrong candidate fund identity/currency')
    assets = _response(sources['assets'])['result']
    if not re.fullmatch(r'\d{4}/\d{2}/\d{2}', assets['preDate']):
        raise ValueError('Missing actual candidate asset date')
    day = date.fromisoformat(assets['preDate'].replace('/', '-'))
    if (str(day) != product['reviewed_holdings_date']
            or info['navDate'] != assets['preDate']
            or day > stamp.astimezone(ZoneInfo('Asia/Taipei')).date()):
        raise ValueError('Candidate actual date mismatch/future date')
    expected = {
        'directory': API+f'GetETFList?FundType=&Keyword={symbol}&CurrentPage=1&PerPageCount=20&status=1',
        'info': API+f'GetETFInfoMain?fundCode={fund}&status=1',
        'assets': product['source_url'],
        'stocks': API+f'GetETFDetailStockList?fundCode={fund}&SearchDate={day}&status=1',
        'balance': API+f'GetETFDetailBalList?fundCode={fund}&SearchDate={day}&status=1',
    }
    if 'directory' not in sources:
        del expected['directory']
    receipts = read_json(sources['receipts'])
    if set(receipts) != set(expected):
        raise ValueError('Invalid actual candidate request receipts')
    finished = []
    for key, url in expected.items():
        receipt, raw = receipts[key], sources[key]
        if (receipt['url'] != url or receipt['http_status'] != 200
                or ('directory' in sources and receipt.get('ok') is not True)
                or ('directory' not in sources
                    and receipt.get('method') != 'GET')
                or receipt['message_complete'] is not True
                or receipt['automatic_retries'] != 0 or receipt['redirect_followed'] is not False
                or receipt['body_bytes'] != len(raw)
                or receipt['sha256'] != hashlib.sha256(raw).hexdigest()):
            raise ValueError('Candidate fund/date/body receipt mismatch')
        begin, end = _utc(receipt['started_at']), _utc(receipt['finished_at'])
        if begin > end or end > stamp:
            raise ValueError('Candidate request timestamp order mismatch')
        finished.append(end)
    if max(finished) != stamp:
        raise ValueError('Candidate timestamp must preserve actual completed acquisition')
    inventory = read_cathay(sources['assets'], sources['stocks'], sources['balance'], day, native_nav=True)
    rows = _response(sources['stocks'])['result']
    known = product['markets']
    if (len(rows) != len(known)
            or {row['stockCode'] for row in rows} != set(known)
            or any(row['stockName'] != known[row['stockCode']]['name'] for row in rows)):
        raise ValueError('Candidate complete stock set/native identity mismatch')
    if any(row.shares <= 0 or row.nav_weight is None for row in inventory.stocks):
        raise ValueError('Invalid actual candidate stock quantity/weight')
    if any(not isinstance(row['weights'],str) or not re.fullmatch(r'\d+\.\d{2}',row['weights']) for row in rows):
        raise ValueError('Unknown native percentage precision')
    nav = inventory.nav
    with localcontext() as context:
        context.prec = 50
        units, per_unit = number(assets['fundOutstandingShares']), number(assets['fundPerNav'])
        if (units <= 0 or units != units.to_integral_value() or per_unit <= 0
                or (nav/units).quantize(Decimal('.01'),rounding=ROUND_HALF_UP) != per_unit
                or number(info['nav']) != per_unit):
            raise ValueError('Candidate whole NAV/unit relationship mismatch')
        book = [r for r in _response(sources['balance'])['result'] if r['item']=='股票']
        if len(book) != 1 or inventory.stock_total is None:
            raise ValueError('Missing candidate stock book total')
        book_pct = inventory.stock_total/nav*100
        if (not 0 <= inventory.stock_total <= nav*Decimal('1.005')
                or abs(number(book[0]['ntMkval'].removesuffix('%'))-book_pct) > Decimal('1e-23')
                or abs(sum(r.nav_weight for r in inventory.stocks)*100-book_pct) > len(rows)*Decimal('.005')):
            raise ValueError('Candidate native stock/whole NAV reconciliation mismatch')
    inventory.quality_notes = tuple(product.get('quality_notes', (
        'USD cash/margin NAV weights remain unknown; no FX or complete asset/liability reconciliation. '
        'Futures NAV valuation remains unknown and is excluded from Taiwan-listed spot stocks.',)))
    return inventory
