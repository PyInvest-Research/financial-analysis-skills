# JSON 與執行合約

v1.2.0 新增混合型 Snapshot schema 2：`Holding.market` 明示掛牌市場，
`foreign_stock` 不進入台股公司計算；`equity_evidence` 提供股票完整性、
同日整檔 NAV、股票聚合市值、股票列數及原始來源雜湊。完整欄位與離線
parser 合約見 [mixed-etfs.md](mixed-etfs.md)。schema 1 的序列化與雜湊保留，
不得在 schema 1 塞入混合型欄位以繞過驗證。下方為原 schema 1 合約。

## Snapshot / Holding

每個本機 snapshot JSON 頂層直接是一個 Snapshot，放在指定資料夾的 `*.json`，不是陣列或包裝物件。JSON `null` 對應 Python `None`。`snapshot_from_dict` 解碼後仍須通過 `validate_snapshot`，不能只確認 JSON 語法。

| Snapshot 欄位 | 型別／限制 |
| --- | --- |
| schema_version | string，固定 `"1"` |
| etf_symbol, issuer | 非空 string；必須核對請求的 ETF 身分 |
| is_active | boolean，指主動管理，不是掛牌存續 |
| holdings_date | string `YYYY-MM-DD`，官方實際持股日 |
| fetched_at | string，UTC ISO 8601 timestamp |
| source_url | string，實際官方或 PyInvest 來源，另依 runtime-safety 檢查 |
| source_kind | `pyinvest` / `official` / `agent_official` |
| holdings | Holding object array；Python 模型為 tuple |

| Holding 欄位 | 型別／限制 |
| --- | --- |
| code, name | string，保留原證券代號與名稱 |
| kind | `stock` / `futures` / `cash` / `other` / `derivative_nav` |
| weight | finite number / null；股票必須有值且在 `[0,1]`，期貨 NAV 同範圍，現金／其他可為 `[-1,1]`；非股票未知權重保留 null |
| exposure_weight | finite number / null，預設 null；期貨獨立帶符號名目曝險，可超過 1，不得以 NAV 權重或保證金推算 |

來源正數應付款的原值與經濟負號轉換分開留存。通用 loader 不依名稱翻號。stock/futures NAV 不允許負值；任何權重禁止 bool、string、NaN/Inf，不補零、不正規化。

本地候選新增 `derivative_nav`：只用於有明確來源依據的衍生品帳面 NAV 貢獻，
weight 必須為有限數值且在 `[-1,1]`，exposure_weight 必須 null。它進入
`other_value` 並參與 gap 一次；同契約另外的 `futures` 行只記額外名目曝險，
兩者不重複計算。原四種 kind 的序列化不變，固定歷史示範不改。

摩根原檔明文說明期貨結算損益與選擇權 NAV，可用此類型；聯博尚無同等
口徑證據，期貨／選擇權原生比例留於來源 capture 與名稱，canonical weight
維持 null。選擇權沒有原生代碼時以原始名稱作內部識別，不冒充證券代號。
舊版 Skill 不認得新類型時應拒絕該快照並更新套件，不丟列或猜測。

## Portfolio JSON

| 欄位 | 型別／限制 |
| --- | --- |
| currency | string `TWD` |
| valuation_date | string `YYYY-MM-DD`，估值日，不改寫持股日 |
| positions | 非空 object array |
| positions[].symbol | string，去空白／轉大寫後 4–6 位英數代號 |
| positions[].kind | `etf` / `stock` |
| positions[].market_value | strictly positive finite number，TWD；拒絕 bool、string、零、負值、NaN/Inf |

同代號同 kind 自動合併並警告；kind 衝突或合併／總額溢位拒絕。原始輸入 bytes 的 SHA-256 保留在結果，不能用重新序列化後內容冒充。

## result.json

| 路徑 | 型別／意義 |
| --- | --- |
| schema_version, code_version | string，schema 固定 `"1"`，程式版本由 runtime 提供 |
| generated_at, input_sha256, valuation_date, currency | string；UTC timestamp、輸入雜湊、ISO 估值日、`TWD` |
| snapshots | metadata object array，包含未取得快照的 ETF |
| totals | object；`total_value`, `identified_stock_value`, `coverage`, `unresolved_value`, `cash_value`, `other_value`, `unclassified_gap_value`, `not_looked_through_value` 均 finite number；coverage 為 fraction，其他為 TWD |
| totals.futures | array，`security_code` string、`amount_twd` finite number / null；額外曝險不併入 NAV 分桶 |
| companies | array，`issuer_code`, `name`, `industry_code` string；`amount_twd`, `share` number；`occurrences` integer（不同 ETF／直接來源數）；`sources` array |
| companies[].sources[] | `source`, `security_code` string；`amount_twd` number |
| industries[] | `industry_code`, `industry_name` string；`amount_twd`, `share` number；未知產業彙總為 `unknown`／未對應 |
| metrics | object；`top5_share`, `top10_share` number；`effective_holdings` number / null；`overlaps` array |
| metrics.overlaps[] | `symbol_a`, `symbol_b` string；`share` number |
| positions[] | `symbol`, `kind` string；`market_value` number；`status` 為 `direct` / `looked_through` / `not_looked_through`；`reason` string / null；`snapshot`, `validation` object / null；`date_status` string / null |
| warnings, limitations | string array；limitations 永遠含全部六句 |

Snapshot metadata 欄位為 `symbol` string；`holdings_date`, `issuer`, `fetched_at`, `source_kind`, `source_url`, `snapshot_sha256`, `validation_status` 可為 string / null；`is_active` boolean / null；`date_status` 為 `common`, `within_tolerance`, `stale`, `active_mismatch` 或 null。未取得快照保留 null，不填假日期。validation 為 `{status, checks}`，status 是 `pass` / `warn` / `fail`，checks 每筆 `{name, status, detail}` 為 string。

`run_lookthrough(portfolio_path, out_dir, *, local_snapshot_dir=None, official_files=None, use_network=True, today=None, base_url=..., opener=None)` 回傳以 `result.json`, `report.md`, `exposures.csv` 為 key 的 `dict[str, Path]`。不要把回傳 mapping 當結果 JSON；需讀取對應檔案。CSV 是 UTF-8 BOM，欄位為 `issuer_code,name,industry_code,amount_twd,share,source,security_code,source_amount_twd`；來源貢獻才可加總。

## 發布 index 與 pins（格式合約，尚無公開資料）

`index.json`：`schema_version: "1"`、`generated_at` UTC timestamp、`etfs` array。每筆有 `symbol`, `issuer` string、`is_active` boolean、`route`（`http` / `playwright` / `official_only` / `research`）、`latest_date` ISO date / null、`dates` ISO date array、`last_status`（`pass` / `warn` / `fail` / `fetch_error` / `official_only` / null）、`last_error` string / null、`last_attempt_at` UTC timestamp / null、`source_url` string / null；可有 `revised_at` UTC timestamp / null。`official_only` 的 latest_date 必須 null；dates 與本機 dated snapshots 一致。候選來源不能自行標成 official_only 或加入支援名單。

歷史檔位於 `{symbol}/{YYYY-MM-DD}.json`，最新為 `{symbol}/latest.json`。`pinned.json` 格式是 `{"pinned": [["0052", "2026-10-22"]]}`：每筆恰為 symbol、ISO date 字串的一對，**不是** symbol-to-date dictionary。此例只說明格式，不表示建立過 pin 或滿足五檔示範。

## v1.3.0 註記與研究擴充

schema 2 的 total_nav_twd、stock_value_twd 亦可為 null；缺值不等於零。
market 保留官方或研究已核對的市場標籤，不再只限原四檔市場。
研究內容以原包與 canonical SHA 綁定；新資料需 v1.3.0。
Snapshot metadata 新增 result_quality 與研究來源說明；taiwan_equity 新增
foreign_stock_weights_complete。whole_portfolio_ratio_known 在部分資料或
NAV 口徑保留時為 false。historical_ratio_references 只作歷史總比例參考，
不進公司計算。research route 是固定研究快照，不會每日抓取或改寫日期。
完整行為見 [research-and-annotations.md](research-and-annotations.md)。
