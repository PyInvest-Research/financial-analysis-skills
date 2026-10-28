# v1.5.0 逐列容錯與歷史比例估算

本文說明 v1.5.0 行為；線上版本與資料日期須核對公開套件與快照清單。原始 demo、固定研究內容與雜湊不改寫。

## 輸入與 strict 相容性

`run_lookthrough` 預設 `validation_policy="annotated"`。直接呼叫
`parse_portfolio`、`load_portfolio`、`load_official_file`、`validate_snapshot`、
`compute_lookthrough` 仍預設 `policy="strict"`，須明確傳 annotated 才放寬。
strict 不新增估算／暫列欄位，不接受字串金額、零市值、新 kind 或非原規格 symbol。

annotated 的字串市值移除逗號並解析；零值略過，`skipped_rows` 只留列號與原因。
負值、bool、NaN／Inf、無法解析的金額仍拒絕，避免默默丟失分母。
非台股 symbol、未知 kind 或無法解析的代號保留正市值於 `other_asset`，不穿透。
現金使用 `kind="cash"`，進 cash 桶；重複輸入合併，kind 衝突整個代號保留於未穿透。

## 日期、重複及官方檔

- 預設日期為 Asia/Taipei。預告持股日不限天數，標記 `forward_pcf` 並明說「非已實現持股」。
- 同日內容（排除 fetched_at）相同取最新取得時間；不同亦取最新並列出捨棄檔名。strict 仍拒絕衝突。
- 共同持股日最多讓任何一檔退回 5 個平日；超過則各取最新。平日只扣週末，不扣國定假日。
- CSV、XLSX 與 XLS 表頭可到第 60 列，`ColumnMap` 每欄可傳依序比對的 tuple 同義詞。
  XLSX 從 active sheet 開始搜尋；其他 sheet 採用時記入 quality_notes。
- BIFF XLS 由 `xlrd==2.0.2` 解析，HTML 偽裝 XLS 由標準庫抽表。不執行公式、巨集或網頁腳本。
- 重複表頭、已知區段標題、合計／總計／Total 與註解頁尾不當作持股。
  合計揭露值保留在 quality_notes；`-`、`--`、`—`、`N/A`、`NA` 為未知權重。
- 無法解析的列記錄列號與 quality_notes；疑似證券資料列（數字代號）略過超過 5 列或 5% 就拒絕整檔。
  NaN／Inf、溢位、錯單位或單列權重大於 1 不適用略過容錯，直接拒絕來源。
- 同市場、代號與名稱的證券合併；任一權重未知則合併後未知。不同名稱保留各列並警告。
  原始快照不改寫，計算用衍生列，保留輸入來源 SHA。
- 沒有台股列的非空資產檔仍計算已知非台股分桶，但標明完整性未驗證、不代表確認零台股。
  空快照一律退到下一來源。

## 來源與五種等級

| 等級 | 意義 |
| --- | --- |
| exact | 指定日已揭露資料通過檢查；不是即時精確市值保證 |
| annotated | 部分、過期、預告、品質或分母警告；只算可用列 |
| research | 套件內固定且綁定雜湊的研究快照 |
| estimated_ratio | 歷史基金台股 NAV 比例乘本次部位市值，無個股拆分 |
| unavailable | 無可用來源，或非本工具穿透範圍；保留全額與原因 |

當期官方／PyInvest／本機資料優先，接著為過期資料、固定研究、歷史比例。
本機只有過期或固定研究時，仍嘗試 PyInvest；明確官方檔可替換有警告的 cache，
但不能用過期官方檔蓋掉較高級的可用來源。取得失敗原因保留在 JSON 與報告。

## 輸出欄位

annotated 的 `positions[].result_quality` 與 snapshots 採上述五級。
snapshots 新增 `date_kind`：`observed`／`historical`／`forward_pcf` 或 null。
`totals.provisional_stock_value` 與 `provisional_companies` 保留名錄外且有來源名稱的股票，
標記 `directory_status="not_in_bundled_directory"`、`industry_code="unknown"`。
此金額不進 identified、產業、集中度或重疊；可另執行 refresh_directory() 更新名錄。

`totals.estimated` 包含 `estimated_taiwan_equity_value`、完整組合分母的 `share`，
及帶 symbol／date／share／source_url／估算金額的 `positions`。
`coverage` 仍為 identified_stock_value / total_value。
`coverage_including_estimates` 為 (identified_stock_value + estimated_taiwan_equity_value) / total_value。
估算不抵減 `not_looked_through_value`，所以估算桶不是可再加總的 NAV 分桶。
摘要同時顯示含估算涵蓋率、已辨識比例與估算比例；開頭按部位列等級、日期、主要警告。
六句固定限制維持不變，另說明估算不含個股。
沒有完整市場證據的零台股加總，其配置 share 為 null／未知；已揭露列加總與已確認整檔配置分開，不用空加總證明零台股。

## SITCA 通用估算來源評估

現有已核對研究資料有 66 檔 ratio_only，加上 009828 的 2026-09-30 58.29% 參考，共 67 份可用比例紀錄；其中 00690 為已核對的兆豐官方比例，其他沿用公會研究資料。
程式對任何有有效逐檔紀錄的代號共用同一估算入口，並未限死 009828。
0.00% 是歷史四捨五入比例，仍標 estimated_ratio，不能宣稱已確認零台股。

2026-10-28 重新查看 [SITCA 基金投資比率官方頁](https://www.sitca.org.tw/ROC/Industry/IN2605.aspx?pid=IN22602_02)，
確認有年月、公司、基金及投資分類查詢。僅此入口不足以驗證每一檔 ETF 的當期比例、
基金識別映射、幣別分母及完整涵蓋。通用自動擷取仍需獨立取得逐檔官方輸出、日期、
NAV 分母、台股現貨分類、基金代號對照與原檔 SHA；本版沒有宣稱已新增全市場月報 collector。
缺逐檔紀錄時保留 unavailable，不拿其他基金或產業平均替代。

## 設計與檢查摘要

- 目的：降低單列與單來源錯誤的影響，同時維持分母與來源可信度。
- 輸入／輸出：本機 TWD portfolio、官方表格／JSON；輸出 result.json、report.md、exposures.csv。
- 相依：標準庫、既有 openpyxl、固定 xlrd 2.0.2。pandas 讀 BIFF 仍需引擎；直接使用 xlrd 避免額外依賴。
- 錯誤點：空資料、NaN／Inf、來源身分、未知權重、重複缺值、台北跨日、編碼、XLS 工作表與 I/O。
- 效能：持股依群組線性合併，既有目錄／研究快取沿用；XLS 會讀表格，極大檔案宜先另存精簡檔。
- 替代方案：全部以 pandas 讀表可縮短解析碼，但仍須 xlrd／openpyxl，引入更多安裝與型別轉換成本。
- 測試：原核心 strict 回歸、合成逐列／日期／來源退階／比例估算／真實 BIFF 記錄；歷史 demo 比較原有金融欄位。
