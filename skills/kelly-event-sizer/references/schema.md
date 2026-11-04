# JSON 輸入與輸出

機率、比例與費率使用小數，不使用百分比字串；只接受有限 JSON 數字。本金與費用採同一貨幣單位，不自動換匯。`outcomes` 預留擴充位置，目前只接受 `YES`／`NO` 的一側或兩側。

## 四個指令

| 指令 | 輸入 | 輸出 |
| --- | --- | --- |
| `normalize` | `outcomes`、可選基準 | 每側淨賠率 `b`、打平點、單位成本、q、潛在套利旗標 |
| `single` | 機率、基準、模式、本金、可成交報價 | 每側比較、建議方向、比例／金額／情境資金，或停止狀態 |
| `joint` | 最多三件已選方向的事件與聯合機率 | 各部位與每種情境的機率／資金，或停止狀態 |
| `calibrate` | 已結算預測紀錄 | λ、樣本外分數、bootstrap 區間、每折資訊，或樣本不足 |

## 單一事件

```json
{
  "p": 0.55,
  "q": 0.4,
  "q_source": "Synthetic fair quote; no spread or fees",
  "mode": "lambda",
  "lambda": 0.5,
  "bankroll": 1000,
  "outcomes": [{"name": "YES", "format": "contract", "price": 0.4}]
}
```

這是合成示範，沒有提供真實事件或市場推薦。對話仍須先確認使用者的事件、時間與判定規則。完整示範輸入見 `assets/video-example-input.json`，程式產出的完整結果見 `assets/video-example-result.json`。

共用欄位：

- `p`：原事件 YES 機率，包含端點；端點需 `certainty_confirmed: true`。
- `mode`：`lambda`（預設）或 `k`，不可混用係數。λ 預設 `0.5`；k 必須明確提供，範圍均為 `[0,1]`。
- `bankroll`：正的總本金。可選 `cap` 為 `[0,1]` 的單筆比例上限；可選 `days` 須為正。
- `q`／`q_source`：直接給基準與來源；或改用 `benchmark`，兩者不可同時存在。
- `event`：可保留事件定義、結算時間、判定方與使用者理由等脈絡，計算器不判定規則是否足夠明確，由對話流程檢查。

### 賠率

每個 `outcomes` 物件必須有 `name: YES` 或 `name: NO`，名稱不可重複：

| `format` | 必要欄位 | 語意 |
| --- | --- | --- |
| `contract` | `price`，嚴格介於零與一 | 贏時每股回收一單位 |
| `decimal` | `odds > 1` | 小數賠率，含本金 |
| `american` | 非零 `odds` | 正或負的美式賠率 |
| `stake` | 正 `stake`、`payout`、`payout_kind` | `net` 為淨賺、`total` 為含本金回收，淨賺須為正 |
| `net` | 正 `net_odds` | 最終實際淨賠率，已含成本 |

可選費用：`purchase_fee` 為非負、按買進金額收取；`profit_commission` 為 `[0,1)`、按淨利抽成，可同時使用。合約的 `total_cost` 覆蓋買進費，仍可另有抽成；任何格式的 `net_odds` 覆蓋兩種費用。若直接給淨賠率但未提供合約實際總成本，不從原價臆測單位數。

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

```json
{
  "mode": "lambda", "lambda": 0.5, "bankroll": 1000,
  "conditional_b_given_a": 0.7,
  "events": [
    {"name": "A", "side": "YES", "p": 0.55, "q": 0.4,
     "outcomes": [{"name": "YES", "format": "contract", "price": 0.4}]},
    {"name": "B", "side": "YES", "p": 0.5, "q": 0.45,
     "outcomes": [{"name": "YES", "format": "contract", "price": 0.45}]}
  ]
}
```

事件 `name` 必須唯一；`side` 必須有對應成交報價。每件可指定 `cap` 覆蓋共用上限、`existing_fraction` 表示固定長部位；上限作用於既有加追加。既有部位必須與當前本金及該側淨賠率的估值基準一致，無法如此表示就停止，不把既有曝險當零。固定持有只支援 λ 模式。

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

- `no_edge`：沒有可執行正部位，或倍數／上限是零；不提供下注金額。
- `arbitrage_out_of_scope`：潛在套利，停止配置，不提供金額。
- `confirmation_required`：先詢問極端確定性，不能自動確認。
- `insufficient_samples`：不估 λ，建議上限與理由另列。
- `error`：錯誤文字，CLI 退出碼為 2。匯入函式則丟出 `ValueError` 等例外，由呼叫端處理；求解失敗不退回模型心算。

`assets/test-cases.json` 是全部 16 案例的公開驗收表；其中 `repeat` 僅為測試 fixture 的重複紀錄簡寫，正式 API 必須傳完整 `records` 陣列。
