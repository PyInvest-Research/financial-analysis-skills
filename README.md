# Financial Analysis Skills

Reusable AI skills for financial analysis, starting with [Taiwan DCF Valuation](skills/taiwan-dcf-valuation/SKILL.md).
Each skill includes instructions, references, and tools under the [MIT License](LICENSE).

## Taiwan ETF Lookthrough

[Taiwan ETF Lookthrough v1.7](skills/taiwan-etf-lookthrough/SKILL.md) 以內建快照與你提供的官方檔計算台灣 ETF 公司曝險、重疊與資料缺口；可離線計算，不需 GCS 帳號；研究與雲端開發可另接私人唯讀資料。明確指定本機快照時，同一商品優先，缺漏商品仍讀內建快照，官方檔可升級不完整或過期候選。

本次（2026-11-02）全量取得 72 檔成功、16 檔失敗。套件內建 72 檔快照，持股日為 2026-10-29 至 2026-11-02；下載後不會自行更新。數量不代表全市場支援或每日取得全部成功。資料缺少、持股日距真實執行日超過 30 天或不完整時，由具備工具的 agent 另取官方檔，計算函式本身不會上網。沒有更佳資料時保留舊快照原日期與警告，再退至固定研究、歷史比例或未取得。

未知權重不補零，完整組合分母不縮小；海外股票另列。見[內建快照清單](skills/taiwan-etf-lookthrough/references/snapshot-coverage.md)及[研究與註記模式](skills/taiwan-etf-lookthrough/references/research-and-annotations.md)。固定歷史示範與當期資料分開；需要指定 demo 時才明確傳入其快照目錄。

請依套件環境安全指引確認工具與 TLS；所有解釋數字以實際生成的 result.json 為準。本工具供資料整理與研究，不構成投資建議。
