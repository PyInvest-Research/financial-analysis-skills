# 混合資產與跨市場 ETF：台股現貨範圍

v1.3.0 另新增 36 檔固定研究快照，支援缺海外權重與註記計算；
見 [research-and-annotations.md](research-and-annotations.md)。以下四檔是原自動來源合約。

v1.2.0 新增以下四檔。2026-10-27 一輪本機官方 HTTPS 取得全部通過，
共 7 次 GET、1,783,351 bytes，約 6.8 秒；不是多日排程穩定性驗收。

| ETF | 官方實際持股日 | 台股占整檔 NAV | 股票現貨範圍 |
| --- | --- | --- | --- |
| 00982T | 2026-10-22 | 77.7162707753% | 21 TWSE＋9 TPEx；逐股顯示權重合 77.72% |
| 00981T | 2026-10-22 | 27.5470910616% | 25 TWSE；逐股顯示權重合 27.55% |
| 00735 | 2026-10-27 | 64.52%（顯示加總） | 58 TWSE＋18 KOSPI＋2 KOSDAQ；未取得未四捨五入台股總市值 |
| 00980T | 2026-10-22 | 0%（已確認） | 完整 10 股皆 Nasdaq 普通股；僅該日台股現貨為零 |

009828 仍缺可用逐股快照，保留 agent 官方備援；v1.3.0 另列 2026-09-30 月末總比例 58.29% 作歷史參考，不進公司曝險。
其公開說明書將週投資組合公告指向 SITCA，但實際下載端點尚未確認。
不能拿公開說明書、前十大、指數比例或季揭露門檻資料補成完整持倉。

## 適用範圍與精度

台股指 TWSE／TPEx 掛牌股票，含海外註冊但台灣掛牌者；海外 ADR、
海外掛牌股票、債券、基金與衍生品另列。只穿透台股現貨公司／產業，
不遞迴 ETF，不計算全球公司或債券發行人集中度。

00982T、00981T 的台股聚合比率來自同日 TWD 股票總市值／基金總 NAV；
逐家公司仍使用來源顯示的權重。00735 只用逐股顯示 NAV 權重加總台股，
保留該精度。不得為了讓逐股合計等於聚合比例而分攤差額或 rescale。

00980T 全股票顯示合計 67.27%，股票市值／NAV 約 67.2537749438%，
兩者差異符合逐列顯示精度。其台股零值由完整海外股票清單佐證，
不是沒有資料就補零，也不代表期貨、其他資產或未來日期永遠無台灣曝險。

## 來源與安全取得

- 00982T：[兆豐產品 id=22](https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id=22)。
  讀實際 `asset_div` 與 `fund_content_list_1`，排除另列指數分配權重。
  NAV 幣別依同區原始 HTML 的 TWD 標記；桌面／行動股票與債券表須一致，
  重複版面只計一次。USD 現金保留未知 NAV 比重，不自行套匯率。
- 00981T：[凱基 J022](https://www.kgifund.com.tw/Fund/Detail?fundID=J022)。
- 00980T：[凱基 J021](https://www.kgifund.com.tw/Fund/Detail?fundID=J021)。
  兩檔讀 `FundDetail-06` 的實際持股比重日與基金總 NAV，非每單位淨值。
  期貨金額未確認 NAV／名目意義，保持權重與名目曝險未知，不併入股票。
- 00735：[國泰 EAW](https://www.cathaysite.com.tw/fund-details/EAW)。
  公開 API 基金代號是 `AW`。先讀 `GetETFAssets?FundCode=AW` 的 `preDate`，
  再將同一日期帶入 `GetETFDetailStockList`、`GetETFDetailBalList` 的
  `SearchDate=YYYY-MM-DD`。三份 API 均須 `success=true`、`returnCode=2000`。
  API host 為 `cwapi.cathaysite.com.tw`，由原官方頁面 JS 來源研究確認；
  parser 不執行 JS。股票／資產回應不回傳查詢日，取得紀錄必須保存實際
  查詢 URL，不能將不同日期回應自行封裝冒充同日。期貨明示名目本金，
  與 NAV 桶分開。僅取得此商品成功，不能外推其他國泰商品也已自動支援。

Collector 每個 GET 30 秒、10 MiB，固定官方 exact host／路徑，
拒絕轉址、環境 proxy、cookie、登入與重試。執行前沿用 runtime-safety.md
的 CA 前檢；這次新加的兩個 host 是 `www.kgifund.com.tw`、
`cwapi.cathaysite.com.tw`。遇存取拒絕停止該路線，不繞過防護。

`mixed-markets.json` 是此批逐證券官方市場證據的精簡投影：保留原代號、
來源名稱、掛牌市場、可取得的掛牌日及原始名錄 SHA-256，不帶入聯絡人、
電話或住址。TWSE／TPEx／KRX／Nasdaq 名錄觀察於 10/26–27，部分持股
日為 10/22；是身分證據，不是當日歷史市場成員名單或價格。
韓股 `.KS` 不代表全為 KOSPI；036930、058470 已確認為 KOSDAQ。

新增／移除代號或名稱變動無新證據時，parser 拒絕新快照並保留上次資料；
須另外核對官方市場證據再更新映射，不自動猜測、不下載全市場名錄。
因此日後調整持股可能需要補證；實際可用日與錯誤以 index 為準。

## 本機已取得官方檔的使用

單一 Mega／KGI 原始 HTML，使用 `PARSERS[etf.issuer]` 與同一 registry：

```python
from pathlib import Path
import json
from lookthrough.issuers.registry import PARSERS, get_supported
from lookthrough.models import snapshot_to_dict

etf = get_supported("00981T")
snapshot = PARSERS[etf.issuer](
    Path("00981T.html").read_bytes(), etf, fetched_at_utc,
)
Path("snapshots").mkdir(exist_ok=True)
Path("snapshots/00981T.json").write_text(
    json.dumps(snapshot_to_dict(snapshot), ensure_ascii=False), encoding="utf-8",
)
```

`fetched_at_utc` 須為實際 UTC 取得時間。國泰需先封裝四份原始回應：

```python
from datetime import date
from lookthrough.issuers.mixed import capture_cathay

raw = capture_cathay(
    Path("00735.html").read_bytes(),
    Path("00735-assets.json").read_bytes(),
    Path("00735-stock.json").read_bytes(),
    Path("00735-balance.json").read_bytes(),
    requested_date=date.fromisoformat(actual_requested_date),
)
etf = get_supported("00735")
snapshot = PARSERS[etf.issuer](raw, etf, fetched_at_utc)
Path("snapshots/00735.json").write_text(
    json.dumps(snapshot_to_dict(snapshot), ensure_ascii=False), encoding="utf-8",
)
```

`actual_requested_date` 必須來自真實 GET URL 與同次 `preDate`，不得填今天。
此 capture 是本機原始 bytes 的封裝，不是官方原生 JSON。
上面程式仍須使用既有專案環境。之後傳入
`run_lookthrough(..., local_snapshot_dir=Path("snapshots"), use_network=False)`。
`run_lookthrough` 本身不抓官網；通用 CSV／XLSX loader 不猜混合型市場。

## Schema 2 與報告

只有新混合型快照使用 `schema_version="2"`，原 schema 1 的 bytes、
雜湊與固定 demo 不改。每筆股票須明示 `market`；台股為 `stock`，
海外為 `foreign_stock`。同一 ticker 跨市場不會合併為台灣公司。

`equity_evidence` 包含 `complete`、`total_nav_twd`、`stock_value_twd`、
`stock_rows`、`taiwan_stock_value_twd`（未知為 null）與原始 `source_hashes`。
所有入口（原始 parser、本機 JSON、公開快照）共用商品資格與完整
`code/name/market` 集合核對；只有上述四檔可採混合型門檻。
驗證全部股票列與聚合 NAV、市場、日期及顯示精度；低台股配置可合格，
但不能全域放寬原純台股的 50%／前十大防漏規則。

`result.json` 僅在使用混合快照時新增 `taiwan_equity`：

- `allocation_value_twd`／`confirmed_allocation_share`：已確認台股現貨配置，
  永遠除完整組合總市值；有未穿透部位時不能解讀為全組合精確台股比率。
- `whole_portfolio_ratio_known`：是否已處理所有部位；未處理時為 false。
- `foreign_stock_value_twd`：海外股票顯示 NAV 權重估計金額。
- `processed_position_share`：具有可計算輸入的部位市值比例，非台股占比，
  也非完整資產負債或全部風險涵蓋率。
- `aggregate_minus_displayed_twd`：聚合比率與逐股顯示加總的差額，不分攤。
- `per_etf`：每檔台股比率、顯示加總與計算基礎。

舊 `coverage` 仍是已辨識台股公司曝險／完整組合市值；海外股票不進入
台股公司、產業、有效持股數與重疊。未揭露現金或其他負債保留未知與 gap。
六句固定限制仍適用。

## 更新流程

每日 06:30 台北的同一 workflow 先跑離線 `test_mixed_*.py`，再執行
同一 publisher。registry 為 49 檔：40 HTTP＋9 official_only；新增四檔
合計正常路徑 7 GET，沒有官網重試或名錄每日重抓。原其他流程與 17:00
本機部署依賴仍保留。網站／Skill 下載更新不等於已跑過新整批 Actions。
