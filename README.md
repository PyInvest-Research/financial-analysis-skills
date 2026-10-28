# Financial Analysis Skills

Reusable AI skills for financial analysis, starting with [Taiwan DCF Valuation](skills/taiwan-dcf-valuation/SKILL.md).
Each skill includes instructions, references, and tools under the [MIT License](LICENSE).

## Taiwan ETF Lookthrough

[Taiwan ETF Lookthrough v1.4.0](skills/taiwan-etf-lookthrough/SKILL.md) 依官方持股與帶日期的研究快照，計算台股公司曝險、產業及重疊。預設讓可用部分先算，明示來源、日期、估算與缺漏；沒有合適快照時由 agent 查安全官方來源。

本版共 77 檔自動更新候選：原 40 檔、原研究商品 36 檔，再加入 00878。計算與每日發布都採可用部分先算、限制標注；來源失敗保留舊日資料。原研究副本仍附在套件中作離線歷史備援。其餘 66 檔歷史總比例與 009828 的 9 月底總比例僅供基金層級參考，不當作公司持股或當期資料。

未知權重不補零，完整組合分母不縮小；海外股票另列。0055／0056 沿用公布權重暫估，並說明已知 NAV 分母差異。見[快照清單](skills/taiwan-etf-lookthrough/references/snapshot-coverage.md)及[研究與註記模式](skills/taiwan-etf-lookthrough/references/research-and-annotations.md)。單檔支援不等同整家投信或全市場支援。

內附 2026-10-22 固定歷史示範；00878 僅此示範例外。請依套件環境安全指引確認工具與 TLS；本工具不構成投資建議。
