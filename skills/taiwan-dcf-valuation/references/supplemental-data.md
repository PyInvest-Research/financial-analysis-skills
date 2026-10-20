# 補充資料 JSON

由 AI 研究並填寫本機 JSON，不需使用者逐項確認。可用 `create_researched_workbook(data, supplement_path, output_path)`，或先 `load_supplement()` 再 `write_workbook()`。所有估計需有來源／參考依據與方法；純判斷可寫 `Analyst assumption` 並說明理由，不虛構網址。

## Web 資料的離線入口

`create_researched_workbook()` 的第一個參數可為 `Dataset` 或 JSON 路徑。JSON 不必由 API collector 產生，也可由 AI 根據瀏覽結果建立；以下為格式示範，身分與日期均須替換成實際研究結果：

```json
{
  "ticker": "9999",
  "company": "SYNTHETIC EXAMPLE ONLY",
  "market": "TWSE",
  "industry": "",
  "valuation_date": "2026-10-20",
  "financial": false,
  "classification_note": "SYNTHETIC: replace with verified business and source",
  "facts": [],
  "gaps": []
}
```

`market` 使用核對後的 TWSE／TPEx；產業代碼不明可留空，於 `classification_note` 寫業務判斷與來源，不能把未核對公司直接視為非金融業。歷史數字依下方 `facts` 格式填寫，放此檔或補充 JSON 其中一處即可。`gaps` 可記錄工具存取限制與真正未取得的科目，兩者要分開描述。

接著建立第二份補充 JSON，填寫 `inputs`、`scenarios` 與研究紀錄。兩個檔案都只是當次 Python 工作目錄中的資料，不需伺服器或使用者手工設定。

## 補充 JSON 範例

```json
{
  "facts": [],
  "inputs": {
    "debt": {
      "value": 300,
      "classification": "analyst_estimate",
      "source": "https://example.com/official-filing.pdf#page=20",
      "method": "SYNTHETIC EXAMPLE: total interest-bearing debt including leases"
    }
  },
  "scenarios": {
    "base": {
      "revenue_growth": {
        "value": [0.05, 0.04, 0.03, 0.02, 0.02],
        "classification": "analyst_estimate",
        "source": "synthetic://example",
        "method": "SYNTHETIC EXAMPLE ONLY; not company guidance"
      }
    }
  }
}
```

以上只是格式範例，數字與網址均非真實公司估值資料，不會自動套用。完整可跑的合成格式見 `tests/fixtures/synthetic.json`。

## facts

每筆必要欄位：`metric`, `value`, `start`, `end`, `period`, `source_url`, `retrieved_at`。`start` 在 instant 時為空字串；其餘日期用 ISO `YYYY-MM-DD`。公告已查到時填 `published_at`，否則省略／空白。可加入 `output_date`, `tag`, `raw_value`, `raw_unit`, `scale`, `scope` 保留原始資訊。

`value` 已正規化為 **TWD million**；`shares` 是 **million shares**。例如 1,000,000,000 元填 1000，1,000,000 股填 1。`raw_value` 與 `raw_unit` 只是來源記錄，程式不再縮放補充資料。只接受合併 `scope="consolidated"`。外幣財報需另行核對匯率與期間，helper 不自動換匯。

可用歷史 metric：`revenue`, `ebit`, `pretax_income`, `tax_expense`, `depreciation`, `amortisation`, `capex_ppe`, `capex_intangible`, `nwc`, `cash`, `debt`, `shares`, `equity_adjustment`、額外調整 `extra_reinvestment`，以及程式 `METRICS` 所列資產負債組成。Capex 以正的支出額表示。不得混合報表範圍或重複累計 YTD。推導／估計的 fact 加上 `classification` 與 `method`，原公告數字保持 `reported`。

已存在同 metric／期間但數值衝突時，停止建模，回原始資料確認。不要以新增一筆零或相鄰期間替代。新增來源對應的程式變更應另外驗證，不要僅靠標題相似就映射。

## inputs

必要輸入為 `base_revenue`, `base_nwc`, `cash`, `debt`, `shares`, `equity_adjustment`。可合理估計，也可用明示且有理由的零值。舊版 `basis_reviewed`／`terminal_reviewed` 僅相容讀取，不必填寫，不阻擋估值。

所有值用 `{"value": number, "classification": "analyst_estimate", "source": "URL or reference", "method": "calculation and rationale"}`。分類為 `reported`、`derived`、`analyst_estimate`；舊 JSON 未填分類時保守視為 `analyst_estimate`。可選 `as_of`、`period_start`、`period_end`，日期用 ISO。期間差異需說明，不因差異直接拒絕。不要以 `NaN`、`Infinity` 或文字代替數字。

對影響大的估計，`method` 應保留實際運算與選值理由。例如全年營收估計寫明「已公布累計營收 + 剩餘月份估計」及各項數字，而非只寫「依歷史趨勢」；前期股數延用寫日期與可能稀釋。沒有可引用的數據時可以明示判斷，並以敏感度呈現其影響。

需要拆解的純量輸入可加 `components` 陣列，每項包含 `label`、正的 `value`、`sign`（`1` 或 `-1`），以及上述 classification／source／method／日期。程式核對帶符號的總和等於輸入值，並在 `Evidence` 顯示；此表是核對用，修改組成後也要同步更新模型輸入。

## scenarios

情境 key 為 `bear`, `base`, `bull`。每個 driver 仍用 value／source／method 結構。

- 五個數值的陣列：`revenue_growth`, `ebit_margin`, `tax_rate`, `da_ratio`, `capex_ratio`, `nwc_ratio`, `extra_reinvestment`。
- 單一數值：`risk_free`, `beta`, `equity_risk_premium`, `cost_of_debt`, `debt_weight`, `wacc_tax_rate`, `terminal_growth`, `terminal_roic`。

`extra_reinvestment` 為 TWD million 稅後金額，正值使用現金、負值釋放現金；不適用時提供有理由的零陣列。BOT／特許經營可用此欄放經說明的淨現金轉換調整，避免與 capex、NWC 重複。

百分比以小數輸入，如 5% 為 `0.05`。Debt weight 可用實際市場值或明示的同業／目標比例。Beta 採 levered equity beta，若由 unlevered beta 推導則保留公式。現金流與折現率需幣別、名目／實質一致；代理值的國家、日期與適用限制寫入 method。

`research_log` 可記錄 `topic`, `source`, `result`, `notes`, `retrieved_at`，說明已查來源、採用代理與仍缺的項目。不要把取得日當公告日。

在 Excel 直接補值也可，但必須在該輸入旁的來源／方法區記錄證據；從網站看到數字不能取代檢查原報表、單位與適用期間。
