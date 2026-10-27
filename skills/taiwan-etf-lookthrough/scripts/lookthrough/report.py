"""Build deterministic offline JSON, Markdown and source-level CSV reports."""

import csv
from dataclasses import asdict
from html import escape
import json
from math import fsum
from pathlib import Path
from typing import Any

from . import __version__
from .calculate import LookthroughResult, PositionStatus
from .dates import PASSIVE_TOLERANCE_WEEKDAYS
from .issuers.cathay_modern import SOURCE_NOTE, SOURCE_URLS
from .metrics import Metrics
from .models import snapshot_sha256
from .portfolio import Portfolio


OVERLAP_DISCLAIMER = "持股重疊不等於報酬相關，也不能單憑重疊判定分散失效。"
EFFECTIVE_N_NOTE = "有效持股數只衡量已辨識股票曝險的權重集中度，不是風險或相關性指標。"
ESTIMATE_NOTE = "曝險為依揭露持股權重估計的配置，不是即時或精確的底層資產金額。"
ACTIVE_NOTE = "主動式：持股可能每日大幅變動，結果僅代表該持股日。"
FUTURES_NOTE = "期貨部位會額外帶來市場（含成分股）曝險，本版未拆解至個股。"
NOT_ADVICE = "本報告為資料整理與計算，不構成投資建議。"
LIMITATIONS = (OVERLAP_DISCLAIMER, EFFECTIVE_N_NOTE, ESTIMATE_NOTE,
               ACTIVE_NOTE, FUTURES_NOTE, NOT_ADVICE)
CSV_FIELDS = ("issuer_code", "name", "industry_code", "amount_twd", "share",
              "source", "security_code", "source_amount_twd")


def _snapshot_metadata(position: PositionStatus) -> dict[str, Any]:
    """Keep actual source identity or explicit nulls for unavailable ETFs."""
    snapshot = position.snapshot
    payload = {
        **({"source_note": SOURCE_NOTE} if snapshot is not None
           and snapshot.issuer == "cathay"
           and snapshot.source_url in SOURCE_URLS else {}),
        "symbol": position.symbol,
        "holdings_date": (snapshot.holdings_date.isoformat()
                          if snapshot else None),
        "issuer": snapshot.issuer if snapshot else None,
        "is_active": snapshot.is_active if snapshot else None,
        "fetched_at": snapshot.fetched_at if snapshot else None,
        "source_kind": snapshot.source_kind if snapshot else None,
        "source_url": snapshot.source_url if snapshot else None,
        "snapshot_sha256": snapshot_sha256(snapshot) if snapshot else None,
        "validation_status": (position.validation.status
                              if position.validation else None),
        "date_status": position.date_status,
    }
    return payload


def build_result_json(
    portfolio: Portfolio, result: LookthroughResult, metrics: Metrics,
    generated_at: str,
) -> dict[str, Any]:
    """Return independent JSON fields from already validated domain inputs.

    The caller supplies the UTC ISO timestamp; no clock, network or I/O is
    consulted. Amounts/shares retain calculation precision. All NAV buckets
    and additional futures remain separate; unknown futures stay null. ETF
    metadata includes failed/unavailable inputs, never a fabricated date.
    """
    snapshots = [_snapshot_metadata(p) for p in result.positions
                 if p.kind == "etf"]
    metadata = {row["symbol"]: row for row in snapshots}
    companies = [{
        "issuer_code": c.issuer_code, "name": c.name,
        "industry_code": c.industry_code, "amount_twd": c.amount,
        "share": c.share, "occurrences": c.occurrences,
        "sources": [{"source": s.source, "security_code": s.security_code,
                     "amount_twd": s.amount} for s in sorted(
                         c.sources, key=lambda s: (s.source, s.security_code,
                                                   s.amount))],
    } for c in sorted(result.companies,
                      key=lambda c: (-c.amount, c.issuer_code))]
    payload = {
        "schema_version": "1", "code_version": __version__,
        "generated_at": generated_at, "input_sha256": portfolio.input_sha256,
        "valuation_date": portfolio.valuation_date.isoformat(),
        "currency": "TWD", "snapshots": snapshots,
        "totals": {
            "total_value": result.total_value,
            "identified_stock_value": result.identified_stock_value,
            "coverage": result.coverage,
            "unresolved_value": result.unresolved_value,
            "cash_value": result.cash_value,
            "other_value": result.other_value,
            "unclassified_gap_value": result.unclassified_gap_value,
            "not_looked_through_value": result.not_looked_through_value,
            "futures": [{"security_code": code, "amount_twd": amount}
                        for code, amount in sorted(result.futures)],
        },
        "companies": companies,
        "industries": [{
            "industry_code": row.industry_code,
            "industry_name": row.industry_name,
            "amount_twd": row.amount, "share": row.share,
        } for row in sorted(metrics.industries,
                            key=lambda r: (-r.amount, r.industry_code))],
        "metrics": {
            "top5_share": metrics.top5_share,
            "top10_share": metrics.top10_share,
            "effective_holdings": metrics.effective_holdings,
            "overlaps": [{"symbol_a": a, "symbol_b": b, "share": share}
                         for a, b, share in sorted(metrics.overlaps)],
        },
        "positions": [{
            "symbol": p.symbol, "kind": p.kind,
            "market_value": p.market_value, "status": p.status,
            "reason": p.reason,
            "snapshot": (dict(metadata[p.symbol])
                         if p.snapshot is not None and p.kind == "etf"
                         else None),
            "validation": asdict(p.validation) if p.validation else None,
            "date_status": p.date_status,
        } for p in result.positions],
        "warnings": list(result.warnings),
        "limitations": list(LIMITATIONS),
    }
    if any(p.snapshot and p.snapshot.schema_version == "2"
           for p in result.positions):
        payload["taiwan_equity"] = _taiwan_equity(result)
    return payload


def _taiwan_equity(result: LookthroughResult) -> dict[str, Any]:
    """Separate allocation, printed equity exposure and processed positions."""
    values, display_values, metadata = [], [], []
    for position in result.positions:
        if position.status == "not_looked_through":
            continue
        if position.kind == "stock":
            values.append(position.market_value)
            display_values.append(position.market_value)
            continue
        snapshot = position.snapshot
        printed = fsum(h.weight for h in snapshot.holdings
                       if h.kind == "stock")
        evidence = snapshot.equity_evidence
        aggregate = (evidence.taiwan_stock_value_twd
                     if evidence else None)
        ratio = (aggregate / evidence.total_nav_twd
                 if aggregate is not None else printed)
        basis = ("same_day_stock_value_over_fund_NAV"
                 if aggregate is not None else "sum_of_displayed_NAV_weights")
        values.append(position.market_value * ratio)
        display_values.append(position.market_value * printed)
        metadata.append(dict(symbol=position.symbol, share=ratio,
                             displayed_share=printed, basis=basis,
                             snapshot_schema=snapshot.schema_version))
    amount = fsum(values)
    displayed = fsum(display_values)
    total = result.total_value
    return {
        "scope": "Taiwan-listed spot equities; derivatives excluded",
        "allocation_value_twd": amount,
        "confirmed_allocation_share": amount / total if total else 0.0,
        "whole_portfolio_ratio_known": result.not_looked_through_value == 0,
        "displayed_stock_value_twd": displayed,
        "aggregate_minus_displayed_twd": amount - displayed,
        "foreign_stock_value_twd": result.foreign_stock_value,
        "processed_position_share": (
            (total - result.not_looked_through_value) / total if total else 0),
        "per_etf": metadata,
    }


def _cell(value: Any) -> str:
    """Escape untrusted display text so it stays in one Markdown cell."""
    text = "未提供" if value is None else str(value)
    return escape(" ".join(text.splitlines()), quote=False).replace("|", r"\|")


def _table(headers: tuple[str, ...], rows: list[list[Any]]) -> str:
    """Render a Markdown table with escaped cells and stable line endings."""
    content = ["| " + " | ".join(headers) + " |",
               "| " + " | ".join("---" for _ in headers) + " |"]
    content.extend("| " + " | ".join(_cell(v) for v in row) + " |"
                   for row in rows)
    return "\n".join(content)


def _amount(value: float | None) -> str:
    """Format display amounts only, retaining unknown futures as unknown."""
    return "未知" if value is None else f"{value:,.2f}"


def _source_label(snapshot: dict[str, Any]) -> str:
    """Disclose agent-obtained official inputs without changing provenance."""
    source = snapshot["source_kind"]
    if source == "agent_official":
        return "agent_official（非內建來源，由 agent 取得）"
    return source if source is not None else "未取得"


def render_report_md(result_json: dict[str, Any]) -> str:
    """Render the fixed report sections without mutating JSON or using I/O.

    Display rounding never changes JSON/CSV calculation precision. All six
    fixed limitations always appear, including absent instrument types.
    Date statuses compare holdings dates to the latest selected holdings
    date, separately from the portfolio valuation date.
    """
    totals = result_json["totals"]
    snapshots = result_json["snapshots"]
    valuation_date = result_json["valuation_date"]
    total = totals["total_value"]
    stale = sorted(s["symbol"] for s in snapshots
                   if s["date_status"] == "stale")
    summary = []
    if stale:
        summary.append("⚠ 持股資料過期：" + "、".join(map(_cell, stale)))
    summary.extend([
        f"估值日：{_cell(valuation_date)}；幣別：TWD。",
        f"已辨識股票涵蓋率：{totals['coverage']:.2%}。所有配置比例以完整組合總市值為分母。",
        f"程式版本：{_cell(result_json['code_version'])}；"
        f"輸入 SHA-256：{_cell(result_json['input_sha256'])}。",
    ])
    summary.extend(f"{_cell(row['symbol'])}：{_cell(row['source_note'])}"
                   for row in snapshots if row.get("source_note"))
    coverage = [("總市值", "total_value"),
                ("已辨識股票", "identified_stock_value"),
                ("未辨識", "unresolved_value"), ("現金", "cash_value"),
                ("其他", "other_value"),
                ("未分類差額", "unclassified_gap_value"),
                ("未穿透", "not_looked_through_value")]
    sections = ["# ETF 持股穿透報告", "## 摘要\n\n" + "\n\n".join(summary),
                "## 資料涵蓋\n\n" + _table(
                    ("分類", "金額（TWD）", "占總市值比例"), [
                        [label, _amount(totals[key]),
                         f"{totals[key] / total if total else 0:.2%}"]
                        for label, key in coverage])]
    mixed = result_json.get("taiwan_equity")
    if mixed:
        sections.append("## 台股現貨配置與資料可用範圍\n\n" + _table(
            ("項目", "數值"), [
                ["已確認台股現貨配置占完整組合",
                 f"{mixed['confirmed_allocation_share']:.4%}"],
                ["海外股票顯示權重估計金額（TWD）",
                 _amount(mixed['foreign_stock_value_twd'])],
                ["已處理部位市值比例",
                 f"{mixed['processed_position_share']:.2%}"],
                ["聚合市值與逐股顯示權重的差額（TWD）",
                 _amount(mixed['aggregate_minus_displayed_twd'])],
            ]) + "\n\n台股配置比率與資料完整程度不同；已處理部位不代表"
            "已涵蓋全部資產與衍生品風險。海外股票另列，不併入台股公司、"
            "產業、重疊或有效持股數。\n\n"
            + ("仍有未穿透部位，全組合實際台股比率未知。\n\n"
               if not mixed['whole_portfolio_ratio_known'] else "")
            + _table(("ETF", "台股占整檔 NAV", "逐股顯示加總", "計算基礎"), [
                [r["symbol"], f"{r['share']:.6%}",
                 f"{r['displayed_share']:.6%}", r["basis"]]
                for r in mixed["per_etf"]])
            + "\n\n顯示權重與聚合市值精度可能不同，差額不分攤、"
            "不補零，也不重新放大。零值僅限已完整確認的該日台股現貨。")
    source_rows = [[
        s["symbol"], s["holdings_date"],
        "主動式" if s["is_active"] else
        "被動型" if s["is_active"] is False else "未提供",
        _source_label(s), s["source_url"], s["validation_status"],
        s["date_status"], s["snapshot_sha256"], s["fetched_at"],
    ] for s in snapshots]
    sources = _table(("代號", "持股日", "類型", "來源", "來源網址", "驗證狀態",
                      "日期狀態", "快照 SHA-256", "取得時間（UTC）"), source_rows)
    dates = [s["holdings_date"] for s in snapshots if s["holdings_date"]]
    if dates:
        sources += f"\n\n日期狀態參考日（最新選定持股日）：{_cell(max(dates))}。"
    for snapshot in snapshots:
        holdings_date = snapshot["holdings_date"]
        if holdings_date and holdings_date != valuation_date:
            sources += (f"\n\n{_cell(snapshot['symbol'])}："
                        f"以 {_cell(valuation_date)}"
                        f" 的部位市值，套用 {_cell(holdings_date)} 揭露的持股權重估計")
    sources += "\n\n" + _table(
        ("部位", "原始市值（TWD）", "穿透狀態", "原因"), [
            [p["symbol"], _amount(p["market_value"]), p["status"],
             p["reason"] if p["reason"] is not None else "—"]
            for p in result_json["positions"]])
    sections.append("## 各檔持股日與來源\n\n" + sources)
    sections.append("## 公司曝險前 10 大\n\n" + _table(
        ("發行人代號", "公司", "產業代碼", "金額（TWD）", "占總市值比例"), [
            [c["issuer_code"], c["name"], c["industry_code"],
             _amount(c["amount_twd"]), f"{c['share']:.2%}"]
            for c in result_json["companies"][:10]]))
    sections.append("## 產業分布\n\n" + _table(
        ("產業代碼", "產業", "金額（TWD）", "占總市值比例"), [
            [i["industry_code"], i["industry_name"], _amount(i["amount_twd"]),
             f"{i['share']:.2%}"] for i in result_json["industries"]]))
    metrics = result_json["metrics"]
    effective = metrics["effective_holdings"]
    concentration = _table(("指標", "數值", "分母／範圍"), [
        ["前 5 大公司", f"{metrics['top5_share']:.2%}", "完整組合總市值"],
        ["前 10 大公司", f"{metrics['top10_share']:.2%}", "完整組合總市值"],
        ["有效持股數", f"{effective:.4f}" if effective is not None else
         "無可計算資料", "僅已辨識股票曝險正規化"],
    ])
    concentration += "\n\n" + _table(("ETF A", "ETF B", "持股重疊"), [
        [p["symbol_a"], p["symbol_b"], f"{p['share']:.2%}"]
        for p in metrics["overlaps"]])
    concentration += "\n\n重疊以各 ETF 揭露之原始股票權重計算，排除未辨識證券，不重新放大。"
    if totals["futures"]:
        concentration += "\n\n期貨額外曝險（不計入總市值分桶）\n\n" + _table(
            ("期貨代號", "額外曝險估計（TWD）"), [
                [f["security_code"], _amount(f["amount_twd"])]
                for f in totals["futures"]])
    sections.append("## 集中度與重疊\n\n" + concentration)
    limits = [*LIMITATIONS,
              f"被動型日期容忍 {PASSIVE_TOLERANCE_WEEKDAYS} 個平日；主動式為 0。"
              "交易日以週一至週五近似，不扣國定假日，判定偏保守。",
              "各金額與比例僅於畫面四捨五入；未辨識、未穿透與未分類差額均保留，未補零或放大。"]
    limits.extend("警告：" + _cell(w) for w in result_json["warnings"])
    sections.append("## 限制與說明\n\n" + "\n\n".join(limits))
    return "\n\n".join(sections) + "\n"


def write_outputs(result_json: dict[str, Any],
                  out_dir: Path) -> dict[str, Path]:
    """Write three stable artifacts and return paths keyed by filename.

    CSV repeats issuer totals per source; source_amount_twd is the additive
    column. Empty companies produce a BOM plus header. I/O errors propagate
    to the caller. Existing same-named outputs are replaced; no other file
    is changed. JSON rejects nonfinite values rather than emitting NaN/Inf.
    """
    json_text = json.dumps(result_json, sort_keys=True, ensure_ascii=False,
                           indent=2, allow_nan=False) + "\n"
    markdown = render_report_md(result_json)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {name: out_dir / name
             for name in ("result.json", "report.md", "exposures.csv")}
    paths["result.json"].write_text(json_text, encoding="utf-8", newline="\n")
    paths["report.md"].write_text(markdown, encoding="utf-8", newline="\n")
    with paths["exposures.csv"].open("w", encoding="utf-8-sig",
                                     newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS,
                                lineterminator="\n")
        writer.writeheader()
        for company in result_json["companies"]:
            for source in company["sources"]:
                writer.writerow({
                    **{key: company[key] for key in CSV_FIELDS[:5]},
                    "source": source["source"],
                    "security_code": source["security_code"],
                    "source_amount_twd": source["amount_twd"],
                })
    return paths
