---
name: taiwan-dcf-valuation
description: 建立台股上市或上櫃非金融公司的當前 FCFF DCF 估值與可編輯 Excel，包含官方財報、五年歷史、五年預測、三情境及敏感度。適用逐檔公司估值；不適用金融業一般 FCFF 或歷史 as-of 回測。
---

# Taiwan DCF Valuation

版本 1.0.1。為指定台股非金融公司完成三情境 DCF 粗估與可編輯 Excel。**預設要完成估值：資料不足就補查或合理估計，完整揭露；不要因為資料不完美而只交空模型。** 不設股票白名單。

公告數字、推導數值與分析師估計分開標示。可以使用歷史比率、同業代理、前期數值延用、簡化假設與有理由的零值，不需逐項等待使用者確認。說清楚理由、來源日期及可能影響；不得把估計說成公告值，或虛構來源。

## 開始使用

一般使用者可在 **ChatGPT 網頁版**上傳完整套件，請它讀取 `SKILL.md` 與相關檔案，再輸入股票代號。若介面不接受 ZIP，解壓後提供需要的文件與程式。可直接說：「依這套 Skill 分析 2753；使用可用的搜尋／瀏覽工具取得資料，合理估計缺項並揭露依據，交付三情境估值與可編輯 Excel。」

AI 先確認本次對話能讀檔、搜尋／瀏覽及執行 Python；工具與額度以當次環境為準，不要求一般使用者設定 Python、API key 或後端。讀到網站或 Chrome 分頁不等於已載入其他檔案，也不等於 Python 可以連網。

**安裝或連網前的安全檢查：** 在安裝／更新套件、下載 Python 或使用 Python HTTPS 之前，先依 [執行環境安全檢查](references/runtime-safety.md) 核對實際下載工具、修補版本與 CA 信任來源。2026-10 事件的最低基準為 `uv >= 0.13.4`、使用 pip 時 `pip >= 26.2.2`、使用 certifi 時 `certifi >= 2026.10.19`；版本符合仍須核對實際 CA。不要先用舊 pip 連網升級，也不能關閉 TLS 驗證。無法確認託管環境時，改用可用的搜尋／瀏覽工具取得資料與既有套件離線產表；缺套件且無安全安裝路徑時如實說明產檔限制。

**API 與 Web 兩條取數路徑並存，共用同一個模型：**

- **已完成安全檢查且可連網的 Python**：保留 `collect_current_data()`，取得官方資料後繼續補查。
- **ChatGPT Web**：由可用的搜尋／瀏覽工具取得官方資料；可先試讀公開 GET API，讀不到就改公司 IR、財報 PDF 或可信次級來源。把查到的資料寫成 JSON，再離線呼叫 `create_researched_workbook()`，不必先成功執行抓取程式。詳見 [Web 取數流程](references/data-sources.md#chatgpt-web-取數流程)。

以下由 AI 在已解壓的套件目錄執行；兩份 JSON 由本次研究建立，格式見 [補充資料](references/supplemental-data.md)，不使用測試數字估值：

```python
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path("scripts").resolve()))
from taiwan_dcf import create_researched_workbook

result = create_researched_workbook(
    Path("outputs/2753-normalized.json"),
    Path("outputs/2753-assumptions.json"),
    Path("outputs/2753-dcf.xlsx"),
    recalculate=shutil.which("soffice") is not None,
)
print(result)
```

本機／開發環境完成安全檢查後，可在套件目錄使用 `uv run --locked --no-build --no-python-downloads`，或使用自己專案的既有虛擬環境，不全域安裝。`.python-version` 選用 Python 3.12，並非安全修補證明；缺少合適的 Python 時先完成安全安裝，不移除限制重試。API 入口仍為 `data = collect_current_data(ticker, capture_dir=...)`，再將 `data` 傳給相同的 `create_researched_workbook()`。相依套件列於 `pyproject.toml`。

## 工作方式

1. 讀取 [資料來源](references/data-sources.md)，核對代號、上市／上櫃與業務。金融業改建議 FCFE、DDM 或 residual income；此 helper 不計算其一般 FCFF。名錄分類缺漏時可依公司業務自行判斷並記錄。
2. 依環境使用 API 或 Web 路徑，盡量取得五個完整年度與最新可得季報。**gaps 是待處理項目，不是研究結論。** Python 連線失敗不代表資料缺漏，先改由 Web 查詢。檢查公司 IR／法說，避免將較慢更新的 API 快照誤認為最新資訊。
3. 對重要缺項，先用 `inspect_ixbrl()` 查看原始科目，再查正式財報 PDF／附註。官方來源難以取得時，可用可追溯的可信次級來源並標示。歷史不足五年就用已有年度；可用 `ttm_value()` 組合前期全年與同期 YTD。不要為小項無限搜尋。
4. 建立 [補充資料](references/supplemental-data.md)：營收基期、營運 NWC、現金、有息負債、股數、其他股權調整，以及三情境假設。缺值依「近期數字 → 歷史比率 → 同業代理 → 明示分析師簡化」處理；不重要項目可合理假設零。保留 `reported`、`derived`、`analyst_estimate`、來源／參考依據與計算方法。
5. 預設提供可修改的五年成長、利潤率、稅率、D&A、capex、NWC、WACC 與終值草案。WACC 可用有日期的公債、ERP、個股或同業 Beta、實際或代理融資成本；不能因缺個股 Beta 就停止。**ERP 先查台灣最新可得資料，核對來源更新日期與台灣資料列；台灣值不可取得或不適用時，才依 [ERP 查詢順序](references/data-sources.md#台灣-erp-查詢順序) 選擇代理並揭露原因。** 悲觀／樂觀可對基準作有理由的調整，註明幅度是分析師選擇。
6. 依 [模型定義](references/model.md) 處理特殊現金流。BOT、特許經營或大額預付投資不能只看 PPE 購置；可用歷史現金轉換率或 `extra_reinvestment` 簡化，但要說明稅後金額及避免重複扣除。終值 ROIC 可採歷史／同業代理，或明示採 ROIC 等於 WACC 的保守假設。
7. 用 `create_researched_workbook()` 建立工作簿；有可用的 Excel 引擎就實際重算。沒有引擎時用 `recalculate=False` 產檔，並可由 Python 依同一份 JSON 與 [模型公式](references/model.md) 計算三情境數值；分別標示「Python 已計算」與「Excel 公式尚未原生重算」，不能將兩者混稱。沒有 Python 工具時如實說明本次無法產檔。`create_current_workbook()` 是資料匯入／初稿工具，不代表已完成研究。
8. 交付三情境價值、敏感度、Excel、研究 JSON 與主要假設。針對影響最大的兩三項列出「採用值／來源或推導／改變時的估值影響」，例如 WACC 與租賃再投資；不能只寫「分析師假設」。區間很寬或終值占比高時，說明由哪些假設主導，不設任意占比門檻、不強行縮窄區間。依估計程度標示「粗估」；只有關鍵數字仍無合理估計基礎，才交不完整 Excel。已重算表示算術執行成功，不代表預測已獲驗證。

## 邊界

- 當前估值；來源以取得當下內容為準，不承諾任何歷史 as-of 可得性。
- 金額與股數在工作簿分別使用 TWD million 與 million shares，股價為 TWD/share。保留原始單位與縮放，不用股本除以假定面額取得股數。
- 年度現金流採全年或四個單季；YTD 必須先相減成單季，不能重複累加。來源期間與公告時間是不同欄位，查不到公告日就留白。
- 不將總負債直接當有息負債，也不將流動資產減流動負債直接當營運 NWC。未拆清的項目可以估計或以重要性排除，但要揭露。
- 歷史明細缺漏、前期數字延用、同業代理、合理零值、來源日期差異及未選情境留白，不單獨阻擋估值。負現金流／負普通股權益仍顯示診斷值，不包裝成可交易的負股價。
- 保留數學底線：有限數值、正股數、有效稅率與折現率、`WACC > g`、正 terminal ROIC；無法建立必要現金流與股權調整時不虛構結果。
- 程式與新寫文件採 MIT。官方財報與第三方資料各依原資料授權與使用條款，本套件不替資料重新授權。

## 驗證 helper

```text
uv run --locked --no-build --no-python-downloads --group dev python -m unittest discover -s tests -v
uv run --locked --no-build --no-python-downloads --group dev flake8 scripts tests
```

`tests/fixtures/synthetic.json` 完全是合成資料，只驗算公式與邊界，不代表任何公司估值或投資建議。
