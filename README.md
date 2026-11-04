# Financial Analysis Skills

Reusable AI skills for financial analysis, starting with [Taiwan DCF Valuation](skills/taiwan-dcf-valuation/SKILL.md).
Each skill includes instructions, references, and tools under the [MIT License](LICENSE).

## Taiwan ETF Lookthrough

[Taiwan ETF Lookthrough v1.7.3](skills/taiwan-etf-lookthrough/SKILL.md) 使用內建持股快照與官方檔案，計算公司曝險、持股重疊與資料缺口；沒有網路也能計算。

版本標示分成兩部分，例如 `v1.7.3 · 資料包 2026-11-03`。純快照刷新保留程式版本，只更換資料包日期與內容識別碼；功能、解析器、計算規則或操作介面有實質變動時才換版。同日不同內容可區分，同日相同內容不重複提交。實際資料包 ID 與涵蓋範圍以 skill.json 及內建快照索引為準。

資料包日期是台北時間的打包日，不是 ETF 持股日。每檔 ETF 請依各自持股日解讀；未知權重不補零，完整組合分母不縮小，海外股票另列。見[內建快照清單](skills/taiwan-etf-lookthrough/references/snapshot-coverage.md)及[研究與註記模式](skills/taiwan-etf-lookthrough/references/research-and-annotations.md)。固定歷史示範與當期資料分開。

下載副本不會自動更新，需要較新的資料請重新下載。重現研究時保存程式版本、資料包 ID、輸入檔與生成的 result.json。請遵守套件環境與 TLS 指引；本工具供資料整理與研究，不構成投資建議。

## Kelly Event Sizer

[Kelly Event Sizer v1.1.0](skills/kelly-event-sizer/SKILL.md) 逐步確認事件與結算規則、個人機率與理由、市場基準及來源、可成交報價與報酬、費用、本金及相關部位，資料齊全後才交給 Python 計算。資料未齊時只列缺項，每輪最多問兩件事，已提供的內容不重問；不先給方向或結果卡，也不自行猜測報酬、NO 成交價格、成本或本金。若已明確提供含全部費用的總成本或最終淨賠率，就不再詢問費用。支援分數凱利、向基準收縮、YES／NO 選邊、最多三個相關事件的聯合配置，以及歷史預測的 λ 校準；λ 模式可預設為 0.5，但這項預設不會延伸到其他欄位。

腳本提供 `intake`、`normalize`、`single`、`joint`、`calibrate` 五個 JSON 指令；缺少費用時不視為零，也不以 YES 的補數推測 NO 成交價。輸入格式、公式，以及逐項列明假設的合成範例，均隨套件提供。

下載與固定示範：[pyecontech.com/skills/kelly-event-sizer/](https://pyecontech.com/skills/kelly-event-sizer/)。網站上的範例是固定的合成資料，不提供即時試算。計算不依賴特定平台、不取價、不下單；沒有優勢時不提供下注金額。本工具不推薦市場、不替使用者估計機率，不構成投資建議，在地法規請自行確認。
