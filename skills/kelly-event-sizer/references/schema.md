# JSON 輸入與輸出

機率、比例與費率使用小數，不使用百分比字串；只接受有限 JSON 數字。本金與費用採同一貨幣單位，不自動換匯。`outcomes` 預留擴充位置，目前只接受 `YES`／`NO` 的一側或兩側。

## 五個指令

| 指令 | 輸入 | 輸出 |
| --- | --- | --- |
| `intake` | 已確認的累積資料；可選 `operation: single/joint/normalize` | `ready` 或不帶計算結果的 `awaiting_input` |
| `normalize` | `outcomes`、可選基準 | 每側淨賠率 `b`、打平點、單位成本、q、潛在套利旗標 |
| `single` | 機率、基準、模式、本金、可成交報價 | 每側比較、建議方向、比例／金額／情境資金，或停止狀態 |
| `joint` | 最多三件已選方向的事件與聯合機率 | 各部位與每種情境的機率／資金，或停止狀態 |
| `calibrate` | 已結算預測紀錄 | λ、樣本外分數、bootstrap 區間、每折資訊，或樣本不足 |

## 先檢查缺項

v1.1.0 起，`single` 與 `joint` 在計算前共用 `intake` 檢查；`normalize` 共用其中的報價與費用檢查。這會讓先前省略結算規則、費用或部位揭露的輸入改回 `awaiting_input`，須補齊才能計算。`calibrate` 只用歷史紀錄，不要求交易本金。

例如 `assets/incomplete-input.json`：

```json
{"p": 0.6, "q": 0.7}
```

送入 `intake` 或 `single` 都回 `status: awaiting_input`，列出 `missing_fields`，以及一至兩項 `questions`；每題包含分組 `field`、英文 `prompt` 與該題缺少的欄位。完整輸出見 `assets/incomplete-result.json`。AI 依缺項用使用者的語言詢問並等待，不能補出價格、費用、本金、方向或結果卡。

下一輪合併已確認的答案後重新傳入，不需伺服器保存對話。已知欄位不會出現在缺項中。資料齊全回 `status: ready`、`missing_fields: []`、`questions: []`；單一事件另列 `sides_evaluable` 與 `sides_unknown`，只表示哪側可評估，不含數值或選邊建議。

`operation` 只決定 `intake` 要檢查哪種資料，預設 `single`；不能用它改變 `single`、`joint` 或 `normalize` 本身的檢查。檢查器不推測自然語言或驗證來源真偽，呼叫端只能傳入已確認內容。已知的非法數值仍是錯誤，不應填入虛構資料繞過缺項。

## 單一事件

```json
{
  "purpose": "example",
  "example_requested": true,
  "event": {
    "definition": "Synthetic event E; YES iff its settlement record is true",
    "settlement_at": "2026-12-01T00:00:00Z",
    "resolver": "Synthetic scenario ledger",
    "cancellation_rule": "No cancellation or refund in this synthetic case"
  },
  "p": 0.55,
  "probability_reason": "Explicit synthetic probability assumption",
  "q": 0.4,
  "q_source": "Explicit synthetic benchmark assumption",
  "mode": "lambda",
  "lambda": 0.5,
  "bankroll": 1000,
  "related_positions": "none",
  "outcomes": [{"name": "YES", "format": "contract", "price": 0.4,
                "purchase_fee": 0, "profit_commission": 0}]
}
```

此範例所有事件規則、機率與理由、基準、成交價格、零費用、本金、無相關部位及 λ 都是明列的合成假設，沒有對應真實市場。只有使用者明確要求示範才用此模式。完整輸入與逐項 `assumptions` 見 `assets/video-example-input.json`，程式結果見 `assets/video-example-result.json`。實際決策須使用使用者確認的資料，不得套用這裡的值。

共用欄位：

- `p`：原事件 YES 機率，包含端點；端點需 `certainty_confirmed: true`。
- `mode`：`lambda`（預設）或 `k`，不可混用係數。λ 預設 `0.5`；k 必須明確提供，範圍均為 `[0,1]`。
- `bankroll`：正的總本金。可選 `cap` 為 `[0,1]` 的單筆比例上限；可選 `days` 須為正。
- `q`／`q_source`：直接給基準與來源；或改用 `benchmark`，兩者不可同時存在。
- `event`：必填物件，`definition`、`settlement_at`、`resolver`、`cancellation_rule` 均須為非空文字。內容是否真的互斥、完整或適合此模型，由對話流程核對。
- `probability_reason`：必填非空文字；也接受 `event.probability_reason`。使用者明說沒有額外資訊也是有效紀錄，AI 不能代編理由。
- `related_positions`：必填 `"none"` 或相關事件名稱陣列。單一模式遇到非空陣列會等待，須切到聯合模式提供全部相關事件，不忽略曝險。
- `purpose`：預設 `decision`；明確要求示範時用 `example` 並設定 `example_requested: true`。示範仍須完整輸入，AI 逐項標出 `assumptions`；此附加欄位只保存說明，腳本不驗證假設來源。

### 賠率

每個 `outcomes` 物件必須有 `name: YES` 或 `name: NO`，名稱不可重複：

| `format` | 必要欄位 | 語意 |
| --- | --- | --- |
| `contract` | `price` 或 `total_cost`，嚴格介於零與一 | 已確認贏時每股回收一單位；不能從市場機率猜合約條款 |
| `decimal` | `odds > 1` | 小數賠率，含本金 |
| `american` | 非零 `odds` | 正或負的美式賠率 |
| `stake` | 正 `stake`、`payout`、`payout_kind` | `net` 為淨賺、`total` 為含本金回收，淨賺須為正 |
| `net` | 正 `net_odds` | 最終實際淨賠率，已含成本 |

原始報價必須明確提供兩項費用：`purchase_fee` 為非負、按買進金額收取；`profit_commission` 為 `[0,1)`、按淨利抽成。零費用須明填 `0`，只提供一項仍會等待另一項。無法放入此費率模型時，先取得使用者確認的最終成本或淨賠率。

- 合約的 `total_cost` 覆蓋買進費，但仍須確認 `profit_commission`。
- 若 `total_cost` 已包含**全部費用**，另設 `fees_included: true`，就不追問兩項費率；此表示勝出回收一單位的全部成本，不可再疊加非零抽成。
- 最終實際 `net_odds` 已含全部費用，覆蓋兩種費用，不重問費率。可直接用 `format: net`。若未提供合約實際總成本，`units` 仍為 `null`，不從原價推測單位數。

```json
{"name": "NO", "format": "contract", "total_cost": 0.3, "fees_included": true}
```

```json
{"name": "NO", "format": "net", "net_odds": 2}
```

以上兩段只展示報價格式，其數值是合成假設；不是完整 `single` 請求。

### 基準

```json
{"benchmark": {"kind": "midpoint", "bid": 0.38, "ask": 0.42, "source": "YES quote at recorded time"}}
```

`bid` 是使用者可賣價、`ask` 是可買價，不可反轉。`outcomes.price` 是實際要買的 ask。

```json
{"benchmark": {"kind": "devig", "source": "Two-sided decimal quote"}}
```

去抽水需提供兩側 `outcomes`。外部歷史基準改用：

```json
{"benchmark": {"kind": "external", "value": 0.4, "source": "Named historical reference and date"}}
```

### 單一輸出

`sides` 保留每個已提供方向的 `full_fraction`（帶符號原版）、`adjusted_full_fraction`、`candidate_fraction`（裁切前）、`uncapped_fraction`、`fraction`（實際上限後）、`break_even`、`edge`、`growth`、`equivalent` 與淨賠率。`scaled_full_fraction` 用來比較 λ 倍原版，不是另一筆建議。

只有有正部位的 `status: ok` 含 `selected`：另列 `full_amount`、`uncapped_amount`、`amount`、`units`、`wealth_if_yes`、`wealth_if_no`、`cap_applied`。`units: null` 表示無可確認單位成本。單側報價會有另一側未知的警告；不自動假設能以補數價格買到另一側。

## 聯合事件

完整合成輸入見 `assets/test-cases.json` 第 13、14 案。根層提供 `bankroll`、模式與聯合情境；每個 `events` 項目各自提供單一事件的 `event`、`p`、理由、基準來源、報價及費用，不共用或推測另一件事件的資料。

事件 `name` 必須唯一；`side` 必須有對應成交報價。每件可指定 `cap` 覆蓋共用上限，必填 `existing_fraction` 表示固定長部位；沒有既有部位須明填 `0`。上限作用於既有加追加。根層 `related_positions` 為 `"none"` 或相關事件名稱陣列：所有非零持有都必須列入，所有列入名稱也須包含於 `events`。`"none"` 表示沒有既有或其他未列入模型的相關部位，不能掩蓋已知曝險。

既有部位必須與當前本金及該側淨賠率的估值基準一致，無法如此表示就停止。固定持有只支援 λ 模式。

二事件也可提供四格 `scenario_probabilities`；三事件提供八格。順序依原事件 YES 狀態由全零到全一，例如二事件 `00, 01, 10, 11`，三事件 `000, 001, 010, 011, 100, 101, 110, 111`。或明確傳 `independent: true`。三種輸入互斥；單事件可直接由 p 建表。

可選 `benchmark_probabilities` 採同一順序，其邊際須等於各事件的 q；省略則採基準獨立。每格在 `[0,1]`，總和允許 `1e-6` 誤差後正規化；邊際允許 `1e-6`，不是允許省略情境。

輸出 `fractions`／`positions` 為追加部位；`full_fractions` 為原始機率的聯合最佳解，`total_fractions` 含固定部位。`scenarios` 含使用者、基準、調整後機率與資金；`growth` 是包含既有部位的整體值，`incremental_growth` 比較零追加。不同事件共同結算是模型假設，不會模擬中途資金解鎖。

## 校準

```json
{"records": [
  {"p": 0.6, "q": 0.5, "y": 1, "timestamp": "2026-01-01T00:00:00Z"},
  {"p": 0.4, "q": 0.5, "y": 0, "timestamp": "2026-01-02T00:00:00Z"}
], "seed": 20261104}
```

此短例只展示格式，會得到 `insufficient_samples`。實際估計至少 30 筆；有時間時須六個不同時間，支援任意 ISO 8601 UTC offset、內部按時間排序。所有列都含時間或都不含。

輸出：`lambda_hat`、`scores.baseline`（λ 為零）、`scores.calibrated`（每折在訓練資料重選 λ）、`scores.user`（λ 為一），各含 log loss、Brier 與條件式 90% 分數區間。`bootstrap.interval_90` 是重選 λ 的 90% 區間；固定 1,000 次。`folds`、`evaluation` 與評分樣本數保留切分證據。詳見公式文件的評分與 bootstrap 限制。

## 停止狀態與錯誤

- `awaiting_input`：資料未齊，僅列缺項與一至兩題；沒有方向、比例、金額或結果卡，CLI 退出碼為 0。
- `ready`：僅由 `intake` 回傳；問題為空陣列，可執行對應計算，不代表已有優勢。
- `no_edge`：沒有可執行正部位，或倍數／上限是零；不提供下注金額。
- `arbitrage_out_of_scope`：潛在套利，停止配置，不提供金額。
- `confirmation_required`：先詢問極端確定性，不能自動確認。
- `insufficient_samples`：不估 λ，建議上限與理由另列。
- `error`：錯誤文字，CLI 退出碼為 2。匯入函式則丟出 `ValueError` 等例外，由呼叫端處理；求解失敗不退回模型心算。

`assets/test-cases.json` 是全部 16 案例的公開驗收表；其中 `repeat` 僅為測試 fixture 的重複紀錄簡寫，正式 API 必須傳完整 `records` 陣列。
