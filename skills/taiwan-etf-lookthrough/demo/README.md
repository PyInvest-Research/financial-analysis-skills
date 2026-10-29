# 固定歷史示範：原五檔＋直接2330

這是使用者核准的**固定示範特例**，本檔歷史產物由 v1.0.0 生成，後續版本保留原 bytes。五份共同官方持股日與假設估值日都是 **2026-10-22**；本次實際生成日為真實台北 **2026-10-26**。不等待五檔首次排程發布來決定固定示範日期，亦不把假設市值當使用者真實部位或即時估值。

| 部位 | 假設TWD市值 |
| --- | ---: |
| 0050 | 300000 |
| 006208 | 200000 |
| 0056 | 150000 |
| 00878 | 150000 |
| 00919 | 100000 |
| 直接2330 | 100000 |
| 全部位分母 | 1000000 |

## 授權與每日資料分開

00878這一份已核對的legacy官方來源Snapshot可隨固定skill/demo；此例外不適用其他日期或每日發布。v1.0.0 發布時，國泰五檔 daily 為 `official_only`，沒有 browser 自動 publisher；這是歷史狀態，當期支援範圍與日期請查 [snapshot-coverage.md](../references/snapshot-coverage.md)。本 demo 不作為 daily index／latest／dated snapshot 的種子。repository的`public/data/etf-holdings/v1/pinned.json`只記五個(symbol,2026-10-22)保留政策，沒有持股資料或網站data-date；它不是公開快照已發布的證據。固定示範不代表任意國泰日期的每日快照已提供。

## 檔案與來源

- `portfolio.json`與五份`snapshots/*.json`按原核准研究案例逐byte複製，原source URL／holdings_date／fetched_at均不改。
- `expected-result.json`由本次既有`run_lookthrough`實跑一次生成，並非複製舊expected或手拼JSON；它是歷史參考結果，不是未來執行一定會相同的承諾。
- `report.md`為本次原始核心報告加上本示範必要來源語境；`exposures.csv`是本次原始輸出，只有`source_amount_twd`可逐來源加總。
- `pre-run-evidence.json`保留首次draft的事前核對，另以`v1_pre_run`保存v1執行前的六輸入hash與獨立預期核對；`generation-evidence.json`記v1的default today、零網路、輸出hash及與draft的完整比較，僅生成時間與code_version不同。
- `independent-2330-precheck.json`原bytes保留root於03:44:49Z、早於原研究04:13run的獨立來源核對。draft於05:18:38Z先驗hash並保存，05:19:16Z才實跑；v1沿用同一獨立預期，事前與執行時間另記於上述兩份證據。
- `provenance.json`記原始來源表示法、URL／UTC／raw hash與canonical snapshot hash。大型raw5、原始截圖、日期AX／請求metadata及雙run／Codex乾淨ZIP證據完整留在repository `research/etf-lookthrough-feasibility/acceptance/original-five-draft/`及相鄰`codex-clean-zip/`；**這些大型原件沒有放入下載demo，不能聲稱包內已有全部原始capture**。

本例0050／0056使用已資格官方Daily API實際持股的原生百分比；006208每股採原TWD金額／同頁基金NAV；00878採已有總NAV的legacy公開中文完整表、原生股票百分比；00919採明確PCF清單日2026/10/27查詢的正totStock回應，實際持股日仍date2=10/22。沒有把modern無NAV來源、申購籃子、每單位NAV或外部價格嫁接進本例。

## 本機離線使用

先依SKILL.md準備對應專案環境；本例不安裝、不上網。從skill目錄、`PYTHONPATH=scripts`執行下列小段，**不傳today**，讓核心依真實執行日檢查過期：

```python
from pathlib import Path
from lookthrough.run import run_lookthrough

outputs = run_lookthrough(
    Path("demo/portfolio.json"), Path("demo-local-output"),
    local_snapshot_dir=Path("demo/snapshots"), use_network=False,
)
```

本次實跑沒有傳歷史today，五份驗證均pass。之後日子改變，完整JSON的驗證age等內容可能不同；strict 模式持股年齡超過30天會驗證失敗並保留未穿透；v1.3.0 預設 annotated 則計算歷史暫估並標注，應誠實解釋為歷史資料已過期，不可回填新fetched_at、刪警告或把today鎖回歷史日期冒充新鮮。需要當期分析時另用合格新來源與新的市值／日期，不覆寫本固定示範。

`tests/test_demo.py` 保留原 v1.0.0 金融數字對照；v1.3.0 的完整欄位比較與明示 test-only clock 見下方「歷史驗收」。產品範例一律 default today。

ChatGPT Web人類操作：使用者已回傳原draft的實跑PASS報告及相符hash，原始Web輸出未附；本次v1候選另作本機驗證，沒有宣稱新的Web實跑。自行上傳套件時，請對話先讀SKILL.md與本README；若環境可解壓與執行Python，依上例離線跑。無執行能力就回報，不心算expected；來源過期就保留未穿透與原因，不覆寫日期。

## 數字解讀與不可補帳的差異

2330事前來源式：300000×0.57＋200000×260833292640/457796188686＋100000。0056／00878／00919完整官方表沒有2330。事前獨立預期384951.7099907112528127300190 TWD，實際384951.7099907112，差-5.28127300190E-11 TWD，在事前1e-7容差內；006208不用56.9758顯示捨入值，沒有從result反推預期。

本次已辨識股票993432.8768510489、現金3351.994619240979、other1939.7605263205364、未分類差額1320 TWD，未辨識／未穿透均0。四筆原future代號NYF／TX／TX202611／WTXX6F的amount_twd都是null，不當0、不推notional／方向；未作跨投信契約標準化，不能直接說是四個獨立已知曝險。

原生披露精度及既有逐ETF `abs(gap)<0.0005`裁0，造成已列桶合計比1m分母多 **44.631996610434726 TWD**。保留此殘差，不增平衡列、不改分母或rescale。generic warnings=[]不代表精確未捨入100%對帳。

00878原生逐股合計 **97.13%**，同頁股票TWD/NAV為 **97.11145272768428930496746522%**，差0.01854727231571069503253478百分點（fraction0.0001854727231571069503253478），超過0.0001參考。它是不同口徑的上游公開精度診斷，不改標為同口徑通過，不搬用其他資料補帳；原30項官方逐股／原生合計抽查和研究雙run證據仍保留。

## 六句固定限制

- 持股重疊不等於報酬相關，也不能單憑重疊判定分散失效。
- 有效持股數只衡量已辨識股票曝險的權重集中度，不是風險或相關性指標。
- 曝險為依揭露持股權重估計的配置，不是即時或精確的底層資產金額。
- 主動式：持股可能每日大幅變動，結果僅代表該持股日。
- 期貨部位會額外帶來市場（含成分股）曝險，本版未拆解至個股。
- 本報告為資料整理與計算，不構成投資建議。

## v1.3.0 歷史驗收

原 v1.0.0 產物維持不動。新增 `v1.3.0/expected-result.json`、report.md、
exposures.csv 與 generation-evidence.json，使用真實執行日、不傳 today、
一次禁網執行產生。六個輸入雜湊不變，totals／companies／industries／metrics
與 v1.0.0 完全一致；新增品質標籤、來源檢查與 0056 分母警告是刻意的版本差異。

重現 v1.3.0 的 ChatGPT 驗收時應比較 `v1.3.0/expected-result.json`。同日只排除 generated_at；
跨日驗證 age 必須按真實日期解釋，不能為通過比較隱藏警告。
歷史 v1.0.0 單元測試保留金融數字對照，v1.3.0 單元測試使用其生成日
的明示 test-only clock，除 generated_at 外逐欄完整比較。這不代表新平台已驗收。
