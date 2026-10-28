"""Parse four qualified mixed products without inferring overseas markets."""

import base64
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

from ..directory import CompanyDirectory
from ..mixed_contract import MARKETS as _MARKETS, PRODUCTS
from ..models import EquityEvidence, Holding, Snapshot
from ..validate import validate_snapshot
from ..source_quality import annotated_active, discrepancy
from ._assets import MAX_RAW_BYTES, nav_number, number, read_json
from ._tables import by_id, has_class, one, read_html, rows
from .mega import _desktop, _mobile, STOCK_HEADER

if TYPE_CHECKING:
    from .registry import SupportedEtf


API = 'https://cwapi.cathaysite.com.tw/api/ETF/'
DATA = Path(__file__).parents[1] / 'data'
_NAMES = {
    '00982T': '兆豐台美動能股債平衡ETF基金',
    '00981T': '凱基雙核收息債股平衡ETF基金',
    '00980T': '凱基美國Top股債平衡ETF基金',
}


def assets_day(raw: bytes) -> date:
    """Require a successful actual NAV response and its explicit preDate."""
    response = _response(raw)
    fields = response['result']
    if not isinstance(fields, dict):
        raise ValueError('Missing Cathay NAV object')
    text = fields.get('preDate')
    if (not isinstance(text, str)
            or not re.fullmatch(r'\d{4}/\d{2}/\d{2}', text)):
        raise ValueError('Missing Cathay actual assets date')
    return date.fromisoformat(text.replace('/', '-'))


def _response(raw: bytes) -> dict:
    """Check the API business success contract, not only HTTP status."""
    response = read_json(raw)
    if (not isinstance(response, dict) or response.get('success') is not True
            or response.get('returnCode') != '2000'):
        raise ValueError('Cathay API business response failed')
    return response


def capture_cathay(
    page: bytes, assets: bytes, stocks: bytes, balance: bytes,
    requested_date: date | None = None,
) -> bytes:
    """Bind four original responses to exact reviewed endpoints and NAV day.

    Stock/balance responses do not echo SearchDate; the collector must pass
    the date it actually requested. Offline callers must preserve request
    provenance. This wrapper cannot independently authenticate supplied bytes.
    """
    day = assets_day(assets)
    if requested_date is not None and requested_date != day:
        raise ValueError('Requested Cathay day differs from actual NAV day')
    parts = [page, assets, stocks, balance]
    if any(not isinstance(p, bytes) or not p for p in parts) or sum(
            len(p) for p in parts) > MAX_RAW_BYTES:
        raise ValueError('Invalid or oversized Cathay capture')
    urls = [PRODUCTS['00735'][1], API + 'GetETFAssets?FundCode=AW',
            API + f'GetETFDetailStockList?FundCode=AW&SearchDate={day}',
            API + f'GetETFDetailBalList?FundCode=AW&SearchDate={day}']
    return json.dumps(dict(
        capture_version=1, etf_symbol='00735', holdings_date=day.isoformat(),
        urls=urls, bodies=[base64.b64encode(p).decode('ascii') for p in parts],
    ), allow_nan=False, separators=(',', ':')).encode()


def _weight(text: str) -> Decimal:
    """Require native percentage points with at most two printed decimals."""
    if not isinstance(text, str) or not re.fullmatch(
            r'\d+(?:\.\d{1,2})?\s*%?', text):
        raise ValueError('Changed mixed source percentage unit or precision')
    value = number(text.replace('%', '').strip())
    if not 0 <= value <= 100:
        raise ValueError('Mixed equity percent outside whole-NAV bounds')
    return value / 100


def _equities(
    symbol: str, records: list[list[str]], day: date,
) -> list[Holding]:
    """Resolve exact source identities against the saved official market map.

    Added/removed codes or name changes fail closed until requalified.
    A tiny missing row cannot hide inside aggregate rounding tolerance.
    No suffix, nationality or fuzzy company-name inference is used.
    """
    known, seen, holdings = _MARKETS[symbol], set(), []
    for code, name, shares, weight in records:
        if code in seen:
            raise ValueError('Duplicate mixed security')
        seen.add(code)
        if code in known and known[code]['name'] == name:
            identity = known[code]
        else:
            discrepancy('Changed mixed security identity; partial inventory')
            directory = CompanyDirectory.from_file(
                DATA / 'company-directory.json')
            company = directory.get(code)
            if company is None or company.name != name:
                holdings.append(Holding(code, name + ' [market unknown]',
                                        'other', None))
                continue
            identity = dict(market=company.market, listing_date=None)
        listed = identity['listing_date']
        if listed is not None and date.fromisoformat(listed) > day:
            raise ValueError('Security listing postdates actual holdings')
        if number(shares) < 0 or number(shares) % 1:
            raise ValueError(
                'Mixed source shares must be nonnegative integers')
        market = identity['market']
        kind = 'stock' if market in ('TWSE', 'TPEx') else 'foreign_stock'
        holdings.append(Holding(code, name, kind, float(_weight(weight)),
                                market=market))
    if not holdings or seen != set(known):
        discrepancy('Incomplete or changed qualified mixed equity set')
    return holdings


def _kgi(raw: bytes, symbol: str) -> tuple:
    """Read dated KGI assets, duplicate equity tables and NAV weights."""
    root = read_html(raw)
    title = one([n for n in root.find('h2') if n.text], 'KGI product title')
    if not title.text.startswith(_NAMES[symbol] + ' ') or (
            f'({symbol} ' not in title.text):
        raise ValueError('KGI mixed product identity mismatch')
    section = by_id(root, 'FundDetail-06')
    text = section.text
    stamp = re.match(r'持股比重 \((\d{4}/\d{2}/\d{2})\)', text)
    if stamp is None:
        raise ValueError('Missing actual KGI holdings day')
    day = date.fromisoformat(stamp[1].replace('/', '-'))
    nav_match = re.search(r'基金淨值資產價值 TWD\$([\d,]+) ', text)
    assets_match = re.search(
        r'資產成分 計價幣別為新台幣 股票 \$([\d,]+) 期貨 '
        r'\$([\d,]+) 債券 \$([\d,]+) 保證金 \$([\d,]+) 現金 \$([\d,]+)',
        text)
    if nav_match is None or assets_match is None:
        raise ValueError('Missing KGI total NAV/currency/asset composition')
    nav = nav_number(nav_match[1])
    stock_value = number(assets_match[1])
    tables = [rows(t) for t in section.find('table')]
    header = ['股票代號', '股票名稱', '股數', '權重(%)']
    stocks = [t for t in tables if t[0] == header]
    if len(stocks) != 2 or stocks[0] != stocks[1]:
        raise ValueError('KGI desktop/mobile equity tables differ')
    holdings = _equities(symbol, stocks[0][1:], day)
    bonds = [t for t in tables if t[0] == [
        '債券代號', '債券名稱', '面額', '市值', '權重(%)']]
    futures = [t for t in tables if t[0] == [
        '期貨代號', '期貨名稱', '口數', '權重(%)', '契約年月']]
    if (len(bonds) != 2 or bonds[0] != bonds[1]
            or len(futures) != 2 or futures[0] != futures[1]
            or len(tables) != 6):
        raise ValueError('Changed KGI actual asset tables')
    for code, name, face, value, weight in bonds[0][1:]:
        number(face), number(value)
        holdings.append(Holding(code, name, 'other', float(_weight(weight))))
    for code, name, lots, weight, month in futures[0][1:]:
        number(lots), _weight(weight)
        if not re.fullmatch(r'\d{4}/\d{2}', month):
            raise ValueError('Missing KGI futures contract month')
        holdings.append(Holding(code + ':' + month, name + '（金額語意待確認）',
                                'futures', None))
    for label, value, kind in [('保證金', assets_match[4], 'other'),
                               ('現金', assets_match[5], 'cash')]:
        holdings.append(Holding(label, label + '（TWD）', kind,
                                float(number(value) / nav)))
    return day, nav, stock_value, holdings, [raw]


def _mega(raw: bytes) -> tuple:
    """Read Mega actual holdings without mixing target index allocation."""
    root = read_html(raw)
    selected = one([n for n in by_id(root, 'fund_id').find('option')
                    if 'selected' in n.attrs], 'Mega selected fund')
    if (selected.attrs.get('value') != '22'
            or selected.text != _NAMES['00982T']):
        raise ValueError('Wrong Mega mixed fund identity')
    section = by_id(root, 'asset_div')
    text = section.text.split('指數分配權重')[0]
    match = re.search(r'資料來源：兆豐投信，(\d{4}/\d{2}/\d{2})', text)
    nav_match = re.search(r'淨資產價值 ([\d,]+) ', text)
    stock_match = re.search(r'股票 \( ([\d.]+)% \) TWD\$ ([\d,]+)', text)
    if match is None or nav_match is None or stock_match is None:
        raise ValueError('Missing Mega actual date, total NAV or stock value')
    day = date.fromisoformat(match[1].replace('/', '-'))
    raw_assets = raw.decode('utf-8-sig').split('id="asset_div"', 1)[1]
    if not re.search(r'淨資產幣別</div>\s*<div class="st-unit">\s*TWD\s*</div>',
                     raw_assets[:1600]):
        raise ValueError('Missing Mega explicit total NAV currency')
    nav, stock_value = nav_number(nav_match[1]), number(stock_match[2])
    if abs(stock_value / nav - _weight(stock_match[1])) > Decimal('.00005'):
        raise ValueError('Mega stock category/NAV mismatch')
    stocks = _desktop(root, 1, STOCK_HEADER)
    if stocks != _mobile(root, 1, STOCK_HEADER):
        raise ValueError('Mega desktop/mobile equity tables differ')
    holdings = _equities('00982T', stocks, day)
    bonds = ['債券代號', '債券名稱', '面額', '市值', '持股權重(%)']
    panel = by_id(root, 'fund_content_list_6')
    title = one([n for n in panel.children if has_class(n, 'fund-title-box')],
                'Mega bond header')
    actual = [n.text for n in title.children]
    if actual != bonds:
        raise ValueError('Changed Mega actual bond header')
    bond_rows = _desktop(root, 6, actual)
    if bond_rows != _mobile(root, 6, actual):
        raise ValueError('Mega desktop/mobile bond tables differ')
    for code, name, face, value, weight in bond_rows:
        number(face), number(value)
        holdings.append(Holding(code, name, 'other', float(_weight(weight))))
    assets = [n for n in section.find('div') if has_class(n, 'fs-item')]
    for index, node in enumerate(assets):
        label = one([n for n in node.find('div')
                     if has_class(n, 'fund-title')],
                    'Mega category').text
        amount = one([n for n in node.find('div')
                      if has_class(n, 'fund-amount')], 'Mega amount').text
        if label.startswith(('股票 (', '債券 (')):
            continue
        if label not in ('保證金', '現金/存款'):
            raise ValueError('Unqualified Mega mixed asset class')
        match = re.fullmatch(r'(TWD|USD)\$\s+([\d,.]+)', amount)
        if match is None:
            raise ValueError('Changed Mega cash currency or amount')
        value = number(match[2])
        weight = float(value / nav) if match[1] == 'TWD' else None
        holdings.append(Holding(f'ASSET-{index}', label + ' ' + amount,
                                'cash' if label == '現金/存款' else 'other',
                                weight))
    return day, nav, stock_value, holdings, [raw]


def _cathay(raw: bytes) -> tuple:
    """Parse a date-bound API capture; overseas markets require exact proof."""
    payload = read_json(raw)
    if (not isinstance(payload, dict) or payload.get('capture_version') != 1
            or payload.get('etf_symbol') != '00735'
            or not isinstance(payload.get('bodies'), list)
            or len(payload['bodies']) != 4):
        raise ValueError('Invalid Cathay mixed source capture')
    try:
        bodies = [base64.b64decode(p, validate=True)
                  for p in payload['bodies']]
    except (ValueError, TypeError) as error:
        raise ValueError('Invalid Cathay source encoding') from error
    day = assets_day(bodies[1])
    expected = read_json(capture_cathay(*bodies, requested_date=day))
    if payload != expected:
        raise ValueError('Cathay capture date or exact source URLs differ')
    root = read_html(bodies[0])
    if '00735 國泰臺韓科技' not in root.text:
        raise ValueError('Cathay product page identity mismatch')
    nav = nav_number(_response(bodies[1])['result']['fundNav'])
    records = _response(bodies[2])['result']
    if not isinstance(records, list):
        raise ValueError('Missing Cathay actual equity rows')
    stocks = [[r['stockCode'], r['stockName'], r['volumn'], r['weights']]
              for r in records]
    holdings = _equities('00735', stocks, day)
    balances = _response(bodies[3])['result']
    if not isinstance(balances, list):
        raise ValueError('Missing Cathay actual asset rows')
    stock_rows = [r for r in balances if r['item'] == '股票']
    if len(stock_rows) != 1:
        raise ValueError('Missing or duplicated Cathay stock aggregate')
    stock_value = number(stock_rows[0]['amount'].removeprefix('NT$'))
    category = number(stock_rows[0]['ntMkval'].removesuffix('%')) / 100
    if abs(stock_value / nav - category) > Decimal('1e-12'):
        raise ValueError('Cathay stock aggregate/NAV denominator mismatch')
    for index, row in enumerate(balances):
        item, amount = row['item'], row['amount']
        if item == '股票':
            continue
        if item == '期貨（名目本金）':
            notional = number(amount.removeprefix('NT$')) / nav
            if abs(notional - number(row['ntMkval'].removesuffix('%'))
                   / 100) > Decimal('1e-12'):
                raise ValueError('Cathay disclosed futures notional mismatch')
            holdings.append(Holding('FUTURES-NOTIONAL', item, 'futures',
                                    None, float(notional)))
            continue
        if item not in ('現金', '保證金', '申贖應付款'):
            raise ValueError('Unqualified Cathay mixed asset class')
        match = re.fullmatch(r'\((TWD|USD|KRW)\) \$ ([\d,.-]+)', amount)
        if match is None:
            raise ValueError('Changed Cathay native asset currency')
        value = number(match[2])
        weight = float(value / nav) if match[1] == 'TWD' else None
        holdings.append(Holding(f'ASSET-{index}', item + ' ' + amount,
                                'cash' if item == '現金' else 'other', weight))
    return day, nav, stock_value, holdings, bodies


def parse(raw: bytes, etf: 'SupportedEtf', fetched_at: str) -> Snapshot:
    """Return a financially qualified schema-2 snapshot or fail closed."""
    if (etf.symbol not in PRODUCTS or etf.is_active
            or (etf.issuer, etf.source_url) != PRODUCTS[etf.symbol]):
        raise ValueError('Unqualified mixed product identity/source URL')
    if etf.symbol == '00735':
        day, nav, stock_value, holdings, bodies = _cathay(raw)
        taiwan_value = None
    elif etf.symbol == '00982T':
        day, nav, stock_value, holdings, bodies = _mega(raw)
        taiwan_value = float(stock_value)
    else:
        day, nav, stock_value, holdings, bodies = _kgi(raw, etf.symbol)
        taiwan_value = 0.0 if etf.symbol == '00980T' else float(stock_value)
    equities = [h for h in holdings if h.kind in ('stock', 'foreign_stock')]
    complete = ({h.code for h in equities} == set(_MARKETS[etf.symbol])
                and not any('[market unknown]' in h.name for h in holdings))
    evidence = EquityEvidence(
        complete, float(nav), float(stock_value), len(equities), taiwan_value,
        tuple(hashlib.sha256(body).hexdigest() for body in bodies))
    snapshot = Snapshot('2', etf.symbol, etf.issuer, False, day, fetched_at,
                        etf.source_url, 'official', tuple(holdings), evidence)
    directory = CompanyDirectory.from_file(DATA / 'company-directory.json')
    result = validate_snapshot(snapshot, directory, day,
                               policy='annotated' if annotated_active()
                               else 'strict')
    if result.status == 'fail':
        raise ValueError(
            'Mixed source equity completeness/NAV validation failed')
    return snapshot
