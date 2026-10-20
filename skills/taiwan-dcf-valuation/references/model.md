# 模型定義與驗收

## 工作簿

- `Model`：估值日、所選情境、同一份五年 FCFF、EV 至普通股權益橋與敏感度。
- `Assumptions`：三情境輸入與一個 selector。金額為 TWD million，股數為 million shares。藍字／黃色為輸入，綠字為跨表公式。
- `History`：目標五個完整年度；原始科目與歷史比率。缺項保持空白。
- `Facts`：可追溯原始值、tag、period、unit、scale、scope、source URL、取得時間、公開時間與缺漏清單。
- `Evidence`：輸入組成、加減方向、來源分類及研究紀錄；與模型輸入的差異可見。

## 核心公式

```text
Revenue[t] = Revenue[t-1] * (1 + growth[t])
EBIT[t] = Revenue[t] * operating_margin[t]
Operating_tax[t] = MAX(EBIT[t], 0) * operating_tax_rate[t]
NOPAT[t] = EBIT[t] - Operating_tax[t]
DA[t] = Revenue[t] * da_ratio[t]
Capex[t] = Revenue[t] * capex_ratio[t]
NWC[t] = Revenue[t] * nwc_ratio[t]
FCFF[t] = NOPAT[t] + DA[t] - Capex[t] - (NWC[t] - NWC[t-1])
          - extra_reinvestment[t]
Cost_of_equity = risk_free + levered_beta * equity_risk_premium
WACC = Cost_of_equity * (1 - debt_weight)
       + cost_of_debt * (1 - marginal_tax_rate) * debt_weight
Terminal_NOPAT = NOPAT[5] * (1 + terminal_growth)
Terminal_reinvestment = Terminal_NOPAT * terminal_growth / terminal_ROIC
Terminal_FCFF = Terminal_NOPAT - Terminal_reinvestment
Terminal_EV = Terminal_FCFF / (WACC - terminal_growth)
EV = SUM(FCFF[t] / (1 + WACC)^t) + Terminal_EV / (1 + WACC)^5
Common_equity = EV + cash - debt + other_common_equity_adjustments
Price_per_share = Common_equity / common_shares_outstanding
```

五個預測年度為估值日起未來一至五年，於每年期末折現；不是把估值當年度的全年現金流當成完整未來一年。最新完整年度營收只是可覆寫的校準草稿，必須核對成適用當前的年度 run-rate。最新 cash 的實際報表日期列在其來源旁，不能宣稱是估值日現金餘額。

本模型不處理虧損抵減或預期可立即實現的退稅，負 EBIT 的 operating tax 為零。有效稅率歷史值與 WACC 的邊際稅率意義不同，不能未經判斷自動互換。

`History` 的歷史 FCFF proxy 以 `tax_expense / pretax_income` 為有效稅率代理，套用至正 EBIT，再加回 D&A、扣除 cash capex 與相鄰兩年的營運 NWC 變動。這個稅額不是已查核的實際營運現金稅。缺前期 NWC、component 或合法稅率就空白；第一年不會因缺 opening NWC 而當零。

`extra_reinvestment` 是五年額外稅後現金用途，正值扣現金、負值釋放現金。可以放特許經營、預付投資或現金轉換率校準的淨額；不是任意平衡項。不得重複扣除已在 capex／NWC 處理的項目。若使用淨現金轉換率校準，修改營收或利潤率後也應調整這五個金額。

租賃密集公司需說清楚租賃負債、使用權資產折舊及租賃再投資的處理是否一致。若把租賃視為融資並在 debt 扣除租賃負債，不能同時無理由再把同一筆融資本金當營運費用扣一次；也不能加回使用權資產折舊卻忽略維持／新增租約的資產投入。附註不齊可用明示代理，說明放在 capex 或 `extra_reinvestment` 的哪一處，避免重複計算。估值一致性可參考 [Damodaran 的租賃處理說明](https://pages.stern.nyu.edu/adamodar/pdfiles/papers/newlease.pdf)。

終值以 `g / ROIC` 決定再投資需求。可以用歷史／同業 ROIC，或明示採 `ROIC = WACC` 的無超額報酬假設；後者不是官方數字。Model 列出末年與終值再投資落差，揭露後仍可計算，不要求人工打勾。此簡化假設業務持續經營，不代表個別 BOT 合約必然續約。

## 假設與缺漏

每個情境有各自五年 growth、margin、tax、D&A、capex、NWC ratio 與額外現金調整，另有 WACC components、terminal growth／ROIC。AI 應提供可用草案；可依歷史／同業／分析師判斷設定，說明選擇理由即可。程式不暗中套用固定數字。未選情境留白不會阻擋所選情境。

`equity_adjustment` 包含非控制權益、優先股、非營運投資／資產等加減調整。債務包含適用的有息借款及租賃。股數優先用已發行普通股減庫藏股；若只能取得較舊或加權平均股數，可以當明示代理，揭露稀釋與時點限制。不得把股本除以未驗證的面額。小項可以有理由地估零，不能把沒查到的數字冒充已公告的零。

下列情況不顯示每股估值：金融業不適用一般 FCFF、缺必要 base／bridge 或所選情境數字、非有限數字、growth 不大於 -1、稅率不在 0–1、capex／D&A 為負、WACC 不在 0–1 或不大於 g、terminal ROIC 不為正、股數不為正、cash／debt 為負。負 terminal FCFF 或負普通股權益保留數值，標成診斷估計。NWC 可為負。

`reported`／`derived`／`analyst_estimate` 只標示資料性質，不封鎖計算。來源標籤不完整會顯示 `Rough estimate; partial metadata`，數字仍可算，但 AI 交付時應補齊說明。舊版的 `basis_reviewed`／`terminal_reviewed` 不再是門檻。

資料不足時的 Excel 是待補齊模型，仍保留所有歷史、來源與公式。補完必要欄位後，由 Excel 或 LibreOffice 依公式重算；不需重建。`openpyxl` 本身不計算公式，因此剛寫出的工作簿沒有已驗證的 cached 估值。

## 敏感度

中心 WACC／g 連結所選情境。AI 交付時填入適當側邊區間，說明幅度屬敏感度假設。公式重用五年 FCFF，重算折現與終值（含 `g / ROIC`）；ROIC 固定為所選情境值。中心應等於模型價格。這是公式網格，並非 Excel 的 What-If Data Table。

三情境同時改變成長、利潤率與 WACC 時，結果不是信賴區間或機率預測。區間很寬時，另用固定其他假設的敏感度說明主要驅動因素。揭露終值現值占 EV 的比例（EV 為零或負時另說明），不以固定占比拒絕估值。若長期超額報酬缺乏依據，可補充 `terminal_ROIC = WACC` 的對照結果，保留原估計並說明差異。

## 驗收

合成測試與真實公司分開報告。本版以 2330、6803 的官方來源加明示估計完成三情境原生重算，並以獨立公式比對；不代表所有公司都有同樣完整的資料。驗收包含真零、缺欄、WACC/g、股數、ROIC、負終值、情境切換、歷史缺漏不阻擋預測，以及敏感度中心等於模型結果。

原始碼測試與可開啟的 XLSX 均不等於實際 Excel 重算驗收；只有在可用試算表引擎實際重算過，才說明已重算的引擎與範圍。
