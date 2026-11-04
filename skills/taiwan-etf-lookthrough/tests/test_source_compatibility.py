"""Synthetic source contracts; no new official financial payloads."""

from datetime import date
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

import support  # noqa: F401
from lookthrough import refresh, refresh_fh, refresh_inventory
from lookthrough.issuers import mixed


TSIT_URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00703"


def tsit_html(hidden="2026/1/2", first="2026/01/02", second="2026/1/02"):
    """Build a minimal synthetic dated inventory, not a captured source."""
    return f'''<meta charset="utf-8"><input id="ETF_ID" value="00703">
    <h3 class="item-title">Synthetic fund (00703)</h3>
    <input id="NAV_DATE" value="{hidden} 上午 12:00:00">
    <div id="paneFive1"><table>
    <tr><td>基金淨資產價值(元)</td><td>TWD 2000</td></tr>
    <tr><td>{first}每基數實際申購總價金(元)</td><td>10</td></tr>
    <tr><td>{second}每基數申購總價金差額(元)</td><td>1</td></tr>
    </table><table><tr><th>代號</th><th>名稱</th><th>股數</th><th>持股權重</th></tr>
    <tr><td>2330</td><td>Synthetic stock</td><td>100</td><td>50%</td></tr>
    <tr><td>股票合計</td><td></td><td></td><td>50%</td></tr></table>
    <table><tr><th>流動資產總額(計價幣)</th><th>流動比率(%)</th></tr>
    <tr><td>1000</td><td>50%</td></tr></table></div>'''.encode()


def fh_source(summary="3.570%"):
    """Build rows where rounded nonstock sums differ from rounded totals."""
    data = dict.fromkeys(refresh_fh.FIELDS)
    details = [
        dict(ftype="股票", stockid="2330", stockname="台積電", qshare="1",
             mvalue="96.422", prate_addaccint="96.422%"),
        dict(ftype="其他資產", stockid="", stockname="期貨保證金(NTD)",
             qshare="0", mvalue="2.344", prate_addaccint="2.340%"),
        dict(ftype="其他資產", stockid="", stockname="扣除應付買入證券款後現金餘額(NTD)",
             qshare="0", mvalue="1.234", prate_addaccint="1.230%"),
    ]
    for row in details:
        row.update(qshareCur="NTD", accint="0")
    data.update(fundID="ETF08", etf002="00731", twNameFull=refresh_fh.NAME,
                dDate="2026/11/03", pcf_FundNav="100", diff=[], detail=details,
                summary=[dict(ftype="股票", totValue="96.422",
                              totRatio="96.422%"),
                         dict(ftype="期貨", totValue="0", totRatio="0.000%"),
                         dict(ftype="其他資產", totValue="3.578",
                              totRatio=summary)],
                result=[dict(ftype="股票", itemName="股票", tot_mvalue="96.422"),
                        dict(ftype="期貨", itemName="期貨", tot_mvalue="0")] + [
                            dict(ftype="其他資產", itemName=r["stockname"],
                                 tot_mvalue=r["mvalue"]) for r in details[1:]])
    table = [[refresh_fh.NAME + " 證劵代碼：00731"], [], ["日期: 2026/11/03"],
             ["基金資產淨值"], ["100"], [], [], [], [], [],
             ["證券代號", "證券名稱", "股數", "金額", "權重(%)"],
             ["2330", "台積電", "1", "96.422", "96.422%"]]
    return data, table


class SourceCompatibilityTests(unittest.TestCase):
    """Check calendar, identity, amounts, and access stops independently."""

    def setUp(self):
        guard = patch(
            "socket.socket.connect",
            side_effect=AssertionError("offline"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_tsit_calendar_padding_preserves_inventory(self):
        baseline = refresh_inventory.read_tsit(
            tsit_html("2026/01/02"), "00703")
        result = refresh_inventory.read_tsit(tsit_html(), "00703")
        self.assertEqual(result, baseline)
        self.assertEqual(result.day, date(2026, 1, 2))
        leap = refresh_inventory.read_tsit(
            tsit_html("2024/2/29", "2024/02/29", "2024/2/29"), "00703")
        self.assertEqual(leap.day, date(2024, 2, 29))

    def test_tsit_invalid_dates_clock_and_identity_still_rejected(self):
        for value in ("2026/2/29", "2026/13/2", "2026/1/0", "2026/011/2",
                      "2026-01-02", "x2026/1/2", "2026/1/2x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                refresh_inventory.read_tsit(tsit_html(value), "00703")
        for raw in (tsit_html().replace(
                "上午 12:00:00".encode(), "下午 12:00:00".encode()),
                    tsit_html().replace(b'value="00703"', b'value="00851"')):
            with self.assertRaises(ValueError):
                refresh_inventory.read_tsit(raw, "00703")

    def test_tsit_each_nav_label_must_be_valid_and_agree(self):
        for first, second in (("2026/1/3", "2026/1/2"),
                              ("2026/1/2", "2026/1/3"),
                              ("bad", "2026/1/2"),
                              ("2026/1/2", "2026/011/2")):
            with (self.subTest(first=first, second=second),
                  self.assertRaises(ValueError)):
                refresh_inventory.read_tsit(
                    tsit_html(first=first, second=second), "00703")

    def fh_inventory(self, summary="3.570%", edit=None):
        data, table = fh_source(summary)
        if edit:
            edit(data)
        with patch.object(refresh_fh, "_workbook_rows", return_value=table):
            payload = json.dumps({"status": 0, "result": [data]}).encode()
            return refresh_fh.read_inventory(
                payload, b"synthetic workbook stub")

    def test_fh_rounded_row_sum_keeps_amount_weights_and_warning(self):
        result = self.fh_inventory()
        self.assertTrue(
            any("rounded" in note for note in result.quality_notes))
        self.assertEqual(result.nav, Decimal("100"))
        self.assertEqual(result.stocks[0].nav_weight, Decimal("0.96422"))
        self.assertAlmostEqual(sum(h.weight for h in result.assets), 0.03578)
        direct = self.fh_inventory("3.580%")
        self.assertEqual(result.assets, direct.assets)
        self.assertEqual(direct.quality_notes, ())

    def test_fh_nearby_but_not_exact_row_sum_still_rejected(self):
        for value in ("3.571%", "3.560%", "NaN%", "3.57%", "101.000%"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fh_inventory(value)

    def test_fh_invalid_rows_totals_and_completeness_still_rejected(self):
        for edit in (
            lambda d: d["detail"][1].update(prate_addaccint="2.350%"),
            lambda d: d["summary"][2].update(totValue="3.579"),
            lambda d: d["detail"].pop(),
            lambda d: d["detail"].append(d["detail"][1].copy()),
            lambda d: d["detail"][1].update(mvalue="NaN"),
        ):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.fh_inventory(edit=edit)

    def test_fh_unexplained_book_overage_is_not_a_rounding_exception(self):
        def overage(data):
            data["detail"][2].update(mvalue="1.235", prate_addaccint="1.240%")
            data["summary"][2].update(totValue="3.579")
            data["result"][-1].update(tot_mvalue="1.235")
        with self.assertRaisesRegex(
                ValueError, "whole-fund book totals disagree"):
            self.fh_inventory("3.580%", overage)

    def test_budget_diagnostic_names_limit_without_extra_request(self):
        for requests, size, elapsed, label in ((100, 0, 0, "requests"),
                                               (0, 80 * 1024**2, 0, "bytes"),
                                               (0, 0, 240, "elapsed")):
            with patch("lookthrough.refresh.monotonic", return_value=0):
                session = refresh.RefreshSession()
            session.requests, session.bytes = requests, size
            with (
                patch("lookthrough.refresh.monotonic", return_value=elapsed),
                patch("lookthrough.refresh.build_opener") as opener,
            ):
                with self.assertRaisesRegex(
                        ValueError, "budget exhausted.*" + label):
                    session.get(TSIT_URL)
                opener.assert_not_called()
                self.assertEqual(session.requests, requests)

    def test_response_crossing_budget_reports_counters_and_stops_host(self):
        """Keep actual counters when one response crosses the shared budget."""
        url = TSIT_URL
        with patch("lookthrough.refresh.monotonic", return_value=0):
            session = refresh.RefreshSession()
            session.bytes = 80 * 1024**2 - 1
            with patch.object(
                    session.transport, "read", return_value=b"xx") as read:
                with patch("lookthrough.refresh.build_opener"):
                    with self.assertRaisesRegex(
                            ValueError, "budget exhausted: bytes.*"
                            r"requests=1/100, bytes=83886081/83886080, "
                            r"elapsed=0\.000/240 seconds"):
                        session.get(url)
                    self.assertEqual(session.requests, 1)
                    self.assertEqual(session.bytes, 83886081)
                    self.assertIn("www.tsit.com.tw", session.blocked)
                    with self.assertRaisesRegex(ValueError, "host stopped"):
                        session.get(url)
                    self.assertEqual(read.call_count, 1)

    def test_kgi_unmodeled_settlement_is_not_misreported_as_missing_nav(self):
        raw = f'''<meta charset="utf-8">
        <h2>{mixed._NAMES['00981T']} (00981T synthetic)</h2>
        <div id="FundDetail-06">持股比重 (2026/11/03)
        基金淨值資產價值 TWD$100 基金每單位淨值 1
        資產成分 計價幣別為新台幣 股票 $20 期貨 $1 債券 $60
        申贖付款 $2 保證金 $3 現金 $19</div>'''.encode()
        with self.assertRaisesRegex(
                ValueError, "Unqualified KGI asset composition"):
            mixed._kgi(raw, "00981T")
        with self.assertRaisesRegex(
                ValueError, "Missing KGI total NAV/currency"):
            mixed._kgi(raw.replace(b"TWD$100", b"USD$100"), "00981T")


if __name__ == "__main__":
    unittest.main()
