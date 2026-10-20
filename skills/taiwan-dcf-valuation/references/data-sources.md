# 官方資料與取得範圍

1.0.0 以 2330、6803 的官方來源完成本機案例。資料內容與可用性仍須每次核對；不同公司可能使用不同 taxonomy。程式負責可靠讀取，AI 負責補查、選擇代理與揭露估計。

| 用途 | 官方入口 | 處理方式 |
| --- | --- | --- |
| 上市公司名錄 | [TWSE OpenAPI](https://openapi.twse.com.tw/v1/opendata/t187ap03_L) | 核對公司代號、名稱與產業代碼；17 為金融保險業 |
| 上櫃公司名錄 | [TPEx OpenAPI](https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O) | 同時接受英文欄位的實際 API 回應 |
| 名錄備援 | [MOPS 上櫃 CSV](https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv) | API 失敗時用同一官方系統 CSV，上市改用 `_L.csv` |
| 最新一般業損益快照 | [TWSE](https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci)／[TPEx](https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci) | 只發現最新年度與季別，不將其視為歷史資料；金額不直接套用未知單位 |
| 歷史合併 iXBRL | [2330 2025Q4 實例](https://mopsov.twse.com.tw/server-java/t164sb01?step=1&CO_ID=2330&SYEAR=2025&SSEASON=4&REPORT_ID=C) | `SYEAR` 使用西元年；請求前五完整年度 Q4 及最新季別 |
| 人工核對與補件 | [公開資訊觀測站](https://mops.twse.com.tw/)／[XBRL 分類標準入口](https://wwwc.twse.com.tw/rwd/XBRL/standard) | 公司財報、XBRL、公告時間與公司 IR 的來源入口 |

規格：[TWSE swagger](https://openapi.twse.com.tw/v1/swagger.json)、[TPEx swagger](https://www.tpex.org.tw/openapi/swagger.json)。不繞過驗證、存取封鎖或限制；抓取失敗寫入 gaps，再用官方財報手工補充。

執行環境由 `.python-version` 選用 Python 3.12。此輪本機 Python 3.14 對公開來源 TLS chain 回報 `Missing Subject Key Identifier`；保留標準憑證與主機名稱驗證，不以關閉 TLS 驗證解決。

## ChatGPT Web 取數流程

一般 ChatGPT 的搜尋／瀏覽與資料分析 Python 是不同工具。[OpenAI 資料分析文件](https://help.openai.com/en/articles/8437071-data-analysis-with-chatgpt) 說明 Python 環境不能直接呼叫外部網站或 API；不能因 `requests` 或名錄抓取失敗，就判定公司資料不存在。工具能力可能改變，以當次結果為準。

1. 若 Python 已能連網，照常使用現有 API collector。否則從可用的 Web 工具開始，不要求使用者修改 DNS、安裝本機工具或申請金鑰。
2. **Web 也可嘗試直接讀公開 GET API**：用上表官方 URL，只有讀到實際回應中的公司列、欄位與日期才算成功。Swagger 文件、搜尋摘要、空白頁或只有下載連結，都不代表已取得 API 數據。不假設工具能送任意 POST、headers 或處理全部 JSON／CSV。
3. API 讀不到時，改查同一官方資料的網頁、公司 IR／法說與財報 PDF；再使用可追溯的可信次級來源。重大輸入能交叉核對就核對，不以來源數量作估值門檻。遇到 DNS、403、驗證或不支援格式就換可用來源，不反覆嘗試同一路徑或繞過限制。
4. 從實際讀到的原文整理必要欄位，寫入 `normalized.json`／補充 JSON。記錄 URL、期間、單位、合併範圍、取得時間，PDF 可加頁碼；搜尋摘要只作找來源線索。網頁取得的官方數字仍是 `reported`，不因取數工具不同變成估計。真正缺少的資料才使用揭露完整的代理。
5. 直接呼叫 `create_researched_workbook()`；它不發出網路請求，不依賴 collector 成功。若 Web 與 Python 都無法取得關鍵資料，才請使用者提供最必要的財報檔案，或依現有資料明示估計。

若當次對話另有可讀取檔案的瀏覽器或已設定的資料連接工具，可以使用其實際可用能力取得 API 回應；不把這些工具當成每位使用者都有。[ChatGPT Work 的雲端瀏覽器](https://help.openai.com/en/articles/20001280-using-cloud-browser-in-chatgpt) 可操作支援的網站，但其方案、權限及站方限制仍適用，不保證任意 API 都可讀。整個基礎流程不依賴 Work 或額外連接設定。

## iXBRL 對應

所有金額只接受 unit 定義精確為 `iso4217:TWD`、無維度的合併 context，並確認 `CompanyID` 與 `ReportCategory`。依 `scale` 與 `sign` 還原原始數值，再除以 `1_000_000`。例如畫面值 `3,809,054,272`、`scale=3`、TWD，工作簿金額為 `3,809,054.272` TWD million。不能把畫面數字再當成元。

| normalized metric | exact concept |
| --- | --- |
| revenue | `ifrs-full:Revenue` |
| ebit | `ifrs-full:ProfitLossFromOperatingActivities` |
| pretax_income | `ifrs-full:ProfitLossBeforeTax` |
| tax_expense | `ifrs-full:IncomeTaxExpenseContinuingOperations` |
| depreciation | `ifrs-full:AdjustmentsForDepreciationExpense` |
| amortisation | `ifrs-full:AdjustmentsForAmortisationExpense` |
| capex_ppe | `ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` |
| capex_intangible | `ifrs-full:PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities` |
| cash | `ifrs-full:CashAndCashEquivalents` |

現金流的 purchase concepts 轉為正的支出額，原始 sign／scale 保留。歷史 D&A 與 capex 不會默默把缺少的組成當零；缺項可由財報補齊，或以 `analyst_estimate` 明示估零及理由。歷史合計留白不阻擋使用合理預測比率估值。

1.0.0 另對應應收款、存貨、合約資產／負債、預付款、應付款、借款、公司債、租賃、非控制權益與庫藏股候選科目，完整 exact tag 見 `scripts/taiwan_dcf.py` 的 `TAGS`。它們不會盲目加總成完整 debt／NWC。`inspect_ixbrl(body, ticker, year)` 提供未對應 tag、原始值、context、單位與維度，供 AI 查附註後使用。

名錄股數是 `issued_shares` 候選，仍需考慮庫藏股、TDR 與稀釋；不是 EPS 加權平均股數。無法取得當期流通股數時可使用較舊／加權平均數作明示代理，並揭露限制。

不支援的 taxonomy、外幣單位、維度資料、不同合併範圍與衝突的同期間值，不自動猜測。NWC 與完整有息負債沒有通用的可靠單一科目，所以保留為逐檔補充。流動資產減流動負債包含現金及融資項目，不能直接當營運 NWC；總負債也不能代替有息負債。

## 期間與時間

- `annual`：西元 1 月 1 日至 12 月 31 日。
- `ytd`：1 月 1 日起的未滿全年累計；不能加總 Q1、H1、9M。
- `quarter`：單季；年度缺席時必須有完整四季，YTD 先相減成單季。
- `instant`：單一資產負債表日期；不可加總全年。
- `retrieved_at`：本次 UTC 取得時間。
- `published_at`：原始公告時間，查不到就留白。未因出表日期或抓取日推算。
- `output_date`：快照的出表日期，與原始財報公告時間無關。

程式保留指定年度；最新季報另保留前一年度同期 YTD，以供 `ttm_value()` 計算 `prior FY + current YTD - prior YTD`，不與全年重複相加。缺任一期不自動年化；AI 如決定年化，需另列為估計。這是當前取得的財報，可能含更正，不提供歷史 as-of 可得性保證。

## 補查與代理

先查公司 IR／法說、合併財報與附註，再用可信次級來源。MOPS 沒有檔案不代表公司尚未公告：本次兩檔 2026 Q3 MOPS 回覆「檔案不存在」，台積電 IR 卻已有 Q3 簡明財報。不要反覆盲目重試同一網址。

合理代理包括前期借款／股數延用、近年 NWC 或 D&A 比率、同業 Beta／融資成本、帳面非控制權益等。公債可查 [TPEx](https://www.tpex.org.tw/)，ERP／同業 Beta 可參考 [Damodaran](https://pages.stern.nyu.edu/~adamodar/)；標示幣別、日期、地區與推導，沒有適用數字時再用明示分析師假設。不要將特定案例的 WACC 固定套用所有股票。

## 授權與資料保存

MIT 只涵蓋本套件的新寫程式與文件。官方開放資料依各資料集所列授權；財報與公司 IR 文件依各站條款。保存資料時保留 URL、取得時間與原始 capture，不將第三方資料混寫成 MIT。套件不附真實公司財報，合成測試資料有明確標記。
