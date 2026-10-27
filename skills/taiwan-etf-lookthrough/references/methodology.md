# 公司名錄與發行人解析

v1.2.0 已新增四檔混合／跨市場 ETF 的台股現貨部分，請先讀 [mixed-etfs.md](mixed-etfs.md)。下方原有來源合約仍適用各自商品，不能外推成整家投信支援。

## 帶符號 NAV 貢獻（2026-10-26 核准修訂）

使用者已核准：現金與其他的正規 NAV `weight` 可為 `[-1, 1]` 的帶符號小數；股票及期貨 NAV `weight` 仍為 `[0, 1]`。期貨 `exposure_weight` 繼續表示帶符號名目曝險，可超過上述範圍，另列且不拆解成個股。使用者輸入的 `market_value` 仍須為有限且嚴格大於零的數值；字串、布林、NaN／Inf、零與負值均拒絕。

`amount = market_value * weight` 保留現金／其他的正負符號；缺漏權重維持 `None`，不補零、不重分配。`gap = 1 - sum(stock) - sum(cash) - sum(other)`；只有 `abs(gap) < 0.0005` 歸零，負差額保留金額並警告，差額不改稱現金。公司曝險與涵蓋率仍以全部使用者部位總市值為分母；有效持股數仍僅正規化已辨識股票曝險。本次修訂不改六句固定報告限制與原六組人工核算案例。

正規權重代表經濟上的 NAV 貢獻，來源原始欄位和值屬證據，不得改動。若官方欄位明示以正絕對值列示應付款，必須另做明確記錄的來源專屬負債轉換，於來源脈絡／研究保留原欄位、原值、經濟符號與轉換理由。通用 loader 不按名稱猜測翻號，也不靜默修改既有研究 fixture。

合成回歸案例使用股票 `0.95`、現金 `-0.02`、其他 `-0.03`，搭配 `100000` 部位市值，得到股票 `95000`、現金 `-2000`、其他 `-3000`、未分類差額 `10000`；涵蓋率維持 `0.95`，JSON 與 Markdown 保留負號。這是核心合約的合成回歸案例；富邦來源專屬應付款轉換已在 parser 實作，來源證據見 data-sources.md。完整真實資料與平台驗收仍待完成，套件仍為草稿且未發布。

## 官方來源與資料日期

公司名錄只使用下列公開官方來源；JSON API 失敗、空資料或結構不符時，才退到同市場 MOPS CSV。兩個市場都取得並通過解析後，才以原子置換寫入快照。

| 市場 | 主要來源 | CSV 備援 |
| --- | --- | --- |
| TWSE | [上市公司基本資料](https://openapi.twse.com.tw/v1/opendata/t187ap03_L) | [MOPS 上市公司 CSV](https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv) |
| TPEx | [上櫃公司基本資料](https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O) | [MOPS 上櫃公司 CSV](https://mopsfin.twse.com.tw/opendata/t187ap03_O.csv) |

`scripts/lookthrough/data/company-directory.json` 是執行 `refresh_directory` 產生的實際官方名錄，並非合成資料。`as_of` 是 UTC 擷取日；`sources` 分別記錄最終官方 URL、原始回應 bytes 的 SHA256、列數、UTC 擷取時間與原始出表日期。出表日期保留官方民國格式，不把擷取日當成兩市場的共同資料日。公司目前的名稱、產業可能與歷史持股日不同，本版不提供歷史公司分類。

| 正規欄位 | 中文來源欄位 | TPEx 英文來源欄位 |
| --- | --- | --- |
| `code` | 公司代號 | SecuritiesCompanyCode |
| `name` | 公司簡稱 | CompanyAbbreviation |
| `industry_code` | 產業別 | SecuritiesIndustryCode |

來源字串只去前後空白，不以公司全名代替簡稱。必要欄位缺漏或型別不符時拒絕整批資料；相同代碼資料衝突也拒絕，不靜默挑選市場。未知產業碼保留原始值，`industry_name` 留空；不得因此刪除公司或猜測名稱。2026-10-25 首次名錄有 10 列 TWSE 原始產業碼 `91` 未對應，後續產業彙總應歸入未對應曝險，不能重新分配至已辨識產業。

## 產業代碼名稱表

`directory.py` 的 `INDUSTRY_NAMES` 以 [TWSE 集中市場即時交易資訊傳輸規格書 B.12.13](https://dsp.twse.com.tw/public/static/downloads/computerPlanningOperationsDepartment/TWSE%E9%9B%86%E4%B8%AD%E5%B8%82%E5%A0%B4%E5%8D%B3%E6%99%82%E4%BA%A4%E6%98%93%E8%B3%87%E8%A8%8A%E5%82%B3%E8%BC%B8%E8%A6%8F%E6%A0%BC%E6%9B%B8%28B.12.13%29%28202612%29_20260911094449.pdf) 附錄三「證券產業別」、印刷頁 106（PDF 第 118 頁）為主表。入口為 [TWSE 證券商作業手冊](https://dsp.twse.com.tw/brokerManual/list) 的 O-126-A10，2026-09-11 更新。雖文件標題列 2026-12 規格，產業表的觀光餐旅及 35–38 分類在其修訂紀錄已註明於 2023-07 實施。

TPEx 特有 `32`（文化創意業）、`33`（農業科技業）補自 [櫃買中心上櫃股票 IP 行情網路規格書](https://www.tpex.org.tw/storage/eb_data/11209/1120500723-1.pdf)，2023-09-07，附錄三「上櫃證券產業別代碼」、印刷頁 89。2026-10-25 已核對官方搜尋索引中的該表；直接讀取此 TPEx PDF 回 403，未繞過限制。TWSE 主表無 32／33，不把 TPEx 補充項目冒稱為 TWSE 分類。

相同代碼跨市場採 TWSE 顯示名稱，例如 `17` 為金融保險、`31` 為其他電子；TPEx 文件稱金融業、其他電子業，本版依代碼合併，不把文字差異當不同產業。未列於已核對表中的碼保留空名稱；表格是資料分類，不能把代碼 `91` 猜作公司的實際經濟產業。

## 發行人解析

1. 證券代碼直接命中名錄，回傳 `common`，發行人為原代碼。
2. 否則須符合 `^[0-9]{4}[A-Z]$`、前四碼存在名錄，而且持股名稱以該公司的官方簡稱開頭，才回傳 `preferred` 並對應前四碼。
3. 其他一律 `unresolved`，包括 ETF、查無代碼、名稱不符及不符合格式的尾碼。不模糊比對、不推測發行人，也不在此函式自動補零、轉大小寫。

`common` 是本版的直接名錄命中分類，並非另一份證券法律種類認定。名錄包含六位公司代碼等既有編碼；本版不新增 TDR 排除規則。

## 真實測試子集與重現

`tests/fixtures/twse_t187ap03_L.json` 保留 2317、2330、2881；`tpex_t187ap03_O.json` 保留 3680、6488。這些是 2026-10-25 直接下載官方 API 的真實列投影，只留出表日期、公司代號、公司簡稱、產業別四欄，欄名和值未改。未把公開聯絡人、住址、電話、email 等無關欄位帶入套件。

首次下載原始回應 SHA256：

- TWSE：`ad2f13eee00e1c118d7a89acccf38bcaca3fd3dd3ed35e9a0308888fbe6e8513`，1,097 列，出表日期 `1151024`。
- TPEx：`00d924c450bac6973506a5e2a57075f6cb95533d99da77be4307a2b8cd61d081`，894 列，出表日期 `1151025`。

原始回應可能隨來源更新；內建快照以其自己的 `sources` 中雜湊與時間為準。單元測試使用上述小型 fixture 與合成錯誤回應；CSV 備援、逾時與轉址拒絕為模擬測試，不能宣稱本次已走過線上備援。

離線入口為 `parse_directory` 和 `CompanyDirectory.from_file`，均不連網；只有明確呼叫 `refresh_directory` 才下載。執行刷新前先依 [執行環境安全檢查](runtime-safety.md) 核對實際工具、Python CA 與官方來源。這份名錄不包含投資組合資料。

## 曝險、集中度與日期

對每個已驗證持股使用 `amount = market_value * weight`。普通股與已確認特別股先合併至發行人，再加總 ETF 與直接部位；公司及產業 `share = amount / total_portfolio_value`。`coverage = identified_stock_value / total_portfolio_value`；未穿透、未辨識、現金、其他與 gap 均留在完整組合分母。

每檔 `gap = 1 - sum(stock) - sum(cash) - sum(other)`，只在嚴格 `abs(gap) < 0.0005` 時歸零；等於邊界或負值都保留，負值另警告。期貨 NAV 及名目曝險不參與此分桶，`exposure_amount = market_value * exposure_weight` 另列；同契約任何一個來源未知時，合計也維持 null。

公司前五／十大集中度是最大五／十個公司金額除以完整組合。有效持股數 `N_eff = 1 / sum(p_i**2)`，其中 `p_i = identified_company_amount_i / identified_stock_value`，只正規化已辨識股票；沒有正曝險時為 null。它不是風險、相關性或包含現金的配置指標。

兩檔 ETF 各自先合併普通股／特別股發行人，再以原始揭露 NAV fraction 計算 `overlap(A,B) = sum(min(w_Ai, w_Bi))`；排除未知證券與非股票，不重放大。只列有成功穿透的 ETF 配對。

本機與 PyInvest 候選選最新共同持股日；沒有共同日才用各自最新日。明確官方檔保留實際日期。`classify_dates(..., tolerance=5)` 以最新選定持股日為參考，被動型預設容忍五個平日，主動式容忍零天；週一至週五近似交易日，不扣假日。此 tolerance 可由直接呼叫函式設定，`run_lookthrough` 使用預設值。另有獨立 validator `MAX_AGE_DAYS=30`，相對執行日超過 30 個日曆日拒絕，也拒絕未來日期。兩種日期檢查不可混同為相對估值日的限制。

保留原始持股日與估值日差異、UTC 擷取時間、來源及快照雜湊。來源失敗後若備援成功，仍把失敗原因留在 warnings；所有來源失敗時保留整個未穿透部位與 reason。不得用新估值日改寫舊快照。


## 固定歷史示範與重現邊界

使用者核准原五檔＋直接2330、假設TWD總額1000000、共同官方日2026-10-22的固定示範特例。00878只隨skill/demo，國泰daily五檔仍official_only；public pinned.json僅為五個日期保留政策，不能當每日持股已發布。這不等待五檔首次排程發布，也不代表Task24對外發布批准。

本次expected-result由既有run以真實台北2026-10-26、default today離線生成一次，source/snapshot bytes與原fetched stamps不變。root的2330事前獨立來源式與預期在首次demo run前保存；只有generated_at可在完整JSON比較中排除，provenance、snapshot hash、validation age、metrics與warnings皆不可刪。單元測試為歷史fixture可明示test-only固定驗證clock；產品範例不能使用此clock，日後過期就如實回報未穿透／原因。

原生披露比例與amount/NAV是不同精度口徑。此demo的00878 native97.13%與同頁股票amount/NAV97.111452727684...%差0.0185472723157...百分點，保留而不rescale；既有逐ETF小gap裁0後桶合計比1m多44.631996610434726 TWD，保留而不補帳。NYF、TX、TX202611、WTXX6F四個原始代號列的期貨amount未知，不當0，也未標準化成四個已知獨立合約曝險。來源語境、原始完整研究證據及六句限制見demo/README.md與demo/report.md。
