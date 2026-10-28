# ETF estimation-first 實作紀錄

依據：`pyivest/docs/superpowers/specs/2026-10-28-taiwan-etf-lookthrough-estimation-first-design.md` 及使用者當次五項決定。

- 正本：financial-analysis-skills，基底 be47f24；分支 codex/etf-estimation-first。
- 工作順序：輸入與 loader → 驗證與日期 → 來源退階 → 計算與報告 → 文件、打包與回歸。
- 全域限制：strict 維持既有行為；估算、暫列與已辨識股票分開；完整組合分母、不補零、不放大、不推定完整零台股。
- 介面檢查：portfolio／loader 的放寬均由 policy 控制；run 傳入 annotated；原始快照及研究雜湊不重寫。
- 決策：pandas 讀 BIFF 仍須 xlrd；只新增固定版本 xlrd，避免引入整套 pandas。
- 決策：五級採 exact／annotated／research／estimated_ratio／unavailable；過期與預告日另保留 date_kind。
- 決策：估算不抵減 not_looked_through_value，避免把非逐股估算冒充已穿透；coverage_including_estimates 另行呈現。
- 環境：Homebrew uv 0.13.5，無 CA 覆寫，使用內建信任庫；2026-10-28 重新核對 Python／uv 官方事件公告。既有環境離線依鎖檔建立。

## 驗證紀錄

- Task 1–5：完成輸入／官方 loader、驗證與台北日期、來源分級、估算／暫列分桶及報告、CA 文件與鎖檔。
- 新行為以合成資料先重現失敗再修正；最初 14 案擴充至 34 案。
- 正本自足測試：`.venv/bin/python -m unittest discover -s tests`，216/216 通過。包含原核心 strict 合約、來源限制、publisher 錯誤處理、BIFF 二進位、HTML XLS、第二工作表、未來日期、去重、5% 邊界與估算。
- 舊 publisher CLI 測試原本以未來日觸發 fail；依使用者新規則，改用 weight=2 觸發硬失敗，保留該測試的錯誤輸出契約。
- flake8：本次修改的 11 個功能模組及 tests 全部通過；autopep8 已執行。uv lock --check --offline 通過。
- 歷史 demo：所有原 totals 欄位、companies、industries、metrics 與 expected-result.json 完全相同；原始 demo／固定研究檔無 diff。
- 套件：既有 build_skill_packages.mjs 在隔離 staging 建置 85 個 manifest 檔；ZIP CRC、逐檔 SHA-256 及解壓後離線端到端驗證通過。
- 獨立架構審查完成：2 個 Important 已以測試重現並修正；未取得台股配置完整性時不把零加總標為確認零，其他欄位錯誤亦不能掩蓋越界權重。Minor「待執行」紀錄已更新。
- Final: fixed numeric errors hidden by invalid kind — test_bad_kind_does_not_hide_out_of_range_weight RED→GREEN。
- Final: fixed unknown zero allocations — test_incomplete_cash_source_keeps_taiwan_unknown / test_partial_zero_row_keeps_allocation_unknown RED→GREEN。
- 已核對 67 份既有 ratio_only／fallback 紀錄可使用共同估算入口（含公會與兆豐官方比例）；未新增或宣稱驗證當期全市場 SITCA collector。
- 線上 PyInvest TLS、當期外部比例、多日排程及部署狀態不在本輪離線驗證結論內。
- 實作階段結束時未 commit／push／部署。2026-10-28 使用者接續授權提交、推送及網站直接部署；只同步正本 manifest 與必要回歸測試，保留其他未提交檔案。
