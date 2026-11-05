"""Offline 00903/00920 candidates: complete native equity rows, explicit
markets.

No registry entry or daily acquisition is enabled. Reuse the established
Fubon HTML reader, but never feed foreign tickers to the domestic
parser.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from .fubon import (
    _Document, _decode, _number, _one, _rows, STOCK_HEADER, FUTURES_HEADER,
)
from ..models import (
    EquityEvidence, Holding, Snapshot, snapshot_from_dict, snapshot_to_dict,
)

SOURCE_URL = (
    'https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx?stkId=00903&lan=TW'
)
MARKET_FILE = (
    Path(__file__).resolve().parents[1] / 'data/fubon-00903-markets.json'
)
MARKET_SHA = '4ced39a66f157ba9aaf3c13818a27d8e7f351b9fc621e6351453d42f9eddbd6c'
MARKET_FILE_00920 = MARKET_FILE.with_name('fubon-00920-markets.json')
MARKET_SHA_00920 = (
    '003d5fa5ac6dd62688d069926f249f252406216f86ae5f6d528edf8634f7962b'
)
ASSET_LABELS = {'保證金 (TWD)', '保證金 (USD)', '現金 (TWD)', '現金 (USD)'}
# Observed in 00903's 2026-11-04 source (run 41956343015).
# Preserve signed native balances as unknown-weight nonstock rows.
DAILY_SETTLEMENT_LABELS_00903 = frozenset({
    '應收(付)證券款 (HKD)', '應收(付)證券款 (TWD)',
    '應收(付)證券款 (USD)',
})
# Observed in 00920's 2026-11-02 source (run 41572697707). These
# settlement balances are optional, signed, and never converted to NAV
# weights. Keep the historical candidate and other funds' contracts intact.
DAILY_SETTLEMENT_LABELS_00920 = frozenset({
    '應收(付)證券款 (CAD)', '應收(付)證券款 (DKK)',
    '應收(付)證券款 (EUR)', '應收(付)證券款 (ILS)',
    '應收(付)證券款 (JPY)', '應收(付)證券款 (KRW)',
    '應收(付)證券款 (TWD)', '應收(付)證券款 (USD)',
})
PRECISION_PP = Decimal('0.0001')
FUNDS = {
    '00903': ('00903 富邦元宇宙', SOURCE_URL, ASSET_LABELS),
    '00920': ('00920 富邦ESG綠色電力',
              'https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx'
              '?stkId=00920&lan=TW',
              {'現金 (TWD)', '現金 (USD)'}),
}


def _percentage(text):
    """Read finite native NAV percentage points within the known range."""
    if not re.fullmatch(r'\d+(?:\.\d{1,4})?', text):
        raise ValueError('Invalid Fubon native percentage points')
    result = _number(text)
    if not 0 <= result <= 100:
        raise ValueError('Fubon percentage outside NAV range')
    return result


def _markets(symbol='00903'):
    """Load the hash-bound security market map for a qualified fund."""
    if symbol not in FUNDS:
        raise ValueError('Unknown Fubon candidate fund')
    path, expected_sha = ((MARKET_FILE, MARKET_SHA) if symbol == '00903'
                          else (MARKET_FILE_00920, MARKET_SHA_00920))
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('Changed Fubon qualified market resource')
    return json.loads(raw)['securities']


def parse(raw, etf, fetched_at, *, daily=False):
    """Retain native NAV percentages and unresolved nonstock fields."""
    if (etf.symbol not in FUNDS or etf.issuer != 'fubon' or etf.is_active
            or etf.source_url != FUNDS[etf.symbol][1]):
        raise ValueError('Unqualified Fubon global request')
    fund_title, source_url, asset_labels = FUNDS[etf.symbol]
    market_sha = MARKET_SHA if etf.symbol == '00903' else MARKET_SHA_00920
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 10 * 1024 * 1024:
        raise ValueError('Missing or oversized Fubon global HTML')
    if not isinstance(fetched_at, str):
        raise ValueError('Missing Fubon capture timestamp')
    stamp = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Fubon capture timestamp needs timezone')
    root = _Document(_decode(raw)).root
    title = _one([n for n in root.find('h6') if 'top' in (
        n.attrs.get('class') or '').split()], 'fund title')
    hidden = _one([n for n in root.find('input') if n.attrs.get(
        'id') == 'mainContent_subMainContent_hidStkId'], 'fund ID')
    if (title.text != fund_title or hidden.attrs.get('type') != 'hidden'
            or hidden.attrs.get('value') != etf.symbol):
        raise ValueError('Wrong Fubon fund identity')
    panel = _one([n for n in root.find('div') if n.attrs.get(
        'id') == 'mainContent_subMainContent_pnlData'], 'holdings panel')

    def summary(label):
        """Read one unambiguous native numeric summary from the fund panel."""
        row = _one([n for n in panel.find('li') if n.find(
            'p') and n.find('p')[0].text == label], label)
        cells = row.find('p')
        if len(cells) != 2 or any(not n.closed for n in cells):
            raise ValueError('Malformed Fubon summary')
        return _number(cells[1].text)
    nav = summary('基金淨資產(新台幣)')
    units = summary('基金在外流通單位數(單位)')
    unit_nav = summary('基金每單位淨值(新台幣)')
    if nav <= 0 or units <= 0 or unit_nav <= 0 or abs(
            nav / units - unit_nav) > Decimal('.01'):
        raise ValueError('Wrong Fubon NAV/units denominator')
    day_node = _one([n for n in panel.find(
        'p') if n.text.startswith('資料日期')], 'holdings date')
    match = re.fullmatch(r'資料日期\s*：\s*(\d{4})/(\d{2})/(\d{2})', day_node.text)
    if match is None:
        raise ValueError('Missing actual Fubon holdings date')
    day = date(*(int(v) for v in match.groups()))
    if day > stamp.astimezone(timezone(timedelta(hours=8))).date():
        raise ValueError('Future Fubon actual holdings date')
    tables = {}
    for table in panel.find('table'):
        rows = _rows(table)
        kind = ('stock' if rows[0] == STOCK_HEADER else
                'futures' if rows[0] == FUTURES_HEADER
                else 'assets' if rows[0] == ['項目', '金額'] else None)
        if kind is None or kind in tables:
            raise ValueError('Unknown/duplicate Fubon global table')
        tables[kind] = rows
    if not {'stock', 'assets'} <= set(tables):
        raise ValueError('Missing actual Fubon stock or asset table')
    expected = {'股票', '特別注意事項'} | ({'期貨'} if 'futures' in tables else set())
    headings = panel.find('h6')
    if len(headings) != len(expected) or {
            n.text for n in headings} != expected:
        raise ValueError('Changed Fubon disclosure sections')
    stocks = tables['stock']
    if not 2 < len(stocks) <= 5002 or len(
            stocks[-1]) != 5 or stocks[-1][:3] != ['股票合計', '', '']:
        raise ValueError('Incomplete Fubon stocks/total')
    total = _number(stocks[-1][3])
    aggregate_pct = _percentage(stocks[-1][4])
    if not 0 < total <= nav:
        raise ValueError('Invalid Fubon aggregate NAV weight')
    markets = _markets(etf.symbol)
    holdings, seen, amounts, percentages = [], set(), [], []
    tw_value = Decimal(0)
    for row in stocks[1:-1]:
        if len(row) != 5 or row[0] in seen or row[0] not in markets:
            raise ValueError('Unknown/duplicate Fubon security market')
        code, name, quantity, amount, percent = row
        proof = markets[code]
        if name != proof['source_name']:
            raise ValueError('Changed Fubon security identity')
        qty, value, pct = _number(quantity), _number(
            amount), _percentage(percent)
        if qty < 0 or qty != qty.to_integral() or not 0 <= value <= nav:
            raise ValueError('Invalid Fubon equity amount/quantity')
        if abs(pct - value / nav * 100) > PRECISION_PP:
            raise ValueError(
                'Fubon row percentage disagrees with whole-fund NAV')
        market = proof['market']
        kind = 'stock' if market in ('TWSE', 'TPEx') else 'foreign_stock'
        holdings.append(
            Holding(code, name, kind, float(pct / 100), market=market))
        if kind == 'stock':
            tw_value += value
        seen.add(code)
        amounts.append(value)
        percentages.append(pct)
    if (sum(amounts) != total
            or abs(sum(percentages) - aggregate_pct)
            > len(amounts) * PRECISION_PP
            or abs(aggregate_pct - total / nav * 100)
            > len(amounts) * PRECISION_PP):
        raise ValueError('Partial Fubon stock table does not reconcile')
    notes = [
        (f'{etf.symbol}官方來源：持股日期取完整資產表；查詢控制日期不是持股日期。'
         if daily else
         f'{etf.symbol}候選來源：持股日期取完整資產表；查詢控制日期不是持股日期。尚未啟用每日更新。'),
        '保留官方原生NAV百分比；與同頁金額/NAV差異在一個0.0001百分點內，未證實差異原因，不重放大。',
        f'股票總表原生{aggregate_pct}%與總市值/NAV的{total / nav * 100}%有差異；'
        '逐列界限只能限制差額，不能證明總表捨入原因，保留未知警告。',
        ('海外代碼與市場按保存官方名錄核對；HIMX US、IQ US是ADS，未併入台灣股票。'
         '市場名錄觀察日期10/29–30，不冒稱逐日歷史交易所證明。'
         if etf.symbol == '00903' else
         '市場按保存交易所／發行人證據核對；ELPC US、ENIC US、PAM US是ADS，'
         'ENLT IT為TASE普通股，均不併入台股。歷史掛牌證據不冒稱當日存續或停復牌證明。'),
        f'全基金NAV減股票市值為{nav - total} TWD，仍未分配；現金／保證金幣別與換算口徑未全核，不補零或會計平衡。',
    ]
    if etf.symbol == '00920':
        notes.extend([
            ('373220 KS與051910 KS市場已核，精確股份類別仍未知；不補成普通股或ADR。'
             if daily else
             '373220 KS與051910 KS市場已核，精確股份類別仍未知；不補成普通股或ADR。候選資料不是正式發布資格。'),
            'NXT US官方名錄為Nextpower Inc.，基金原文NEXPOWER INC保留；'
            '依唯一代號與市場對應，名稱差異保留警示。',
            'SIBE代碼依歷史BME公告與發行人／交易所資料配對；託管PDF文字未與本地原檔逐位元綁定，保留證據限制。',
        ])
    assets = tables['assets']
    daily_settlement_labels = {
        '00903': DAILY_SETTLEMENT_LABELS_00903,
        '00920': DAILY_SETTLEMENT_LABELS_00920,
    }
    allowed_asset_labels = asset_labels | (
        daily_settlement_labels[etf.symbol] if daily else set())
    asset_seen = set()
    for row in assets[1:]:
        if (len(row) != 2 or row[0] not in allowed_asset_labels
                or row[0] in asset_seen):
            raise ValueError('Unknown/duplicate Fubon nonstock label')
        _number(row[1])
        asset_seen.add(row[0])
        holdings.append(
            Holding(row[0], row[0],
                    'cash' if row[0].startswith('現金') else 'other', None))
        notes.append(f'官方非股原值：{row[0]} {row[1]}；NAV權重未知。')
    if not asset_labels <= asset_seen:
        raise ValueError('Missing disclosed Fubon nonstock row')
    if 'futures' in tables:
        futures = tables['futures']
        if len(futures) < 3 or len(
                futures[-1]) != 5 or futures[-1][:3] != ['期貨合計', '', '']:
            raise ValueError('Incomplete Fubon futures table')
        values = []
        for row in futures[1:-1]:
            if len(row) != 5 or not re.fullmatch(
                    r'[A-Z0-9]+', row[0]) or not row[1] or row[0] in seen:
                raise ValueError('Invalid/duplicate Fubon future')
            quantity, value, pct = map(_number, row[2:])
            if quantity != quantity.to_integral():
                raise ValueError('Invalid futures quantity')
            values.append(value)
            seen.add(row[0])
            holdings.append(Holding(row[0], row[1], 'futures', None, None))
            notes.append(
                f'期貨{row[0]}原數量{quantity}、金額{value}、比率{pct}%；曝險與NAV權重未驗。')
        if sum(values) != _number(futures[-1][3]):
            raise ValueError('Partial Fubon futures table')
        _number(futures[-1][4])
    evidence = EquityEvidence(
        True, float(nav), float(total), len(amounts), float(tw_value),
        (hashlib.sha256(raw).hexdigest(), market_sha))
    return snapshot_from_dict(snapshot_to_dict(Snapshot(
        '2', etf.symbol, 'fubon', False, day, fetched_at, source_url,
        'official', tuple(holdings), evidence, tuple(notes))))
