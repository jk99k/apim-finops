# アーキテクチャと、データが流れる全経路

このドキュメントは「1回のリクエストが、どこを通って、どのテーブルの、どの属性に、
どんな値で記録されるか」を最後まで追跡したものです。
すべて実環境で取得した実物のレコードにもとづいています。

---

## 0. 全体像

```
[Codex CLI]
     │  ① OpenAI 互換のリクエスト
     │     Ocp-Apim-Subscription-Key: <チームのキー>
     │     X-User-Id: alice
     ▼
[Azure API Management (Developer)]
     │  ② ポリシーが順に実行される
     │     allowlist → 単価解決 → 利用者識別 → 天井 → 計測
     │
     ├─── ③ 診断設定 ──────────▶ [Application Insights]
     │        requests テーブル        └─ Log Analytics ワークスペース
     │
     ├─── ④ emit-token-metric ──▶ [Application Insights]
     │        customMetrics テーブル
     │
     │  ⑤ Managed Identity で認証
     ▼
[Microsoft Foundry]
     │  gpt-5.6-sol の実行
     │
     └─── ⑥ 課金レコード ────────▶ [Cost Management]
```

読み取りは3方向に分かれます。

```
[Application Insights]
     ├──▶ ワークブック        APIM の「監視 > ブック」に表示
     ├──▶ ログ検索アラート     推定コスト超過 / 429 検知
     └──▶ (手動 KQL)

[Cost Management]
     └──▶ 予算アラート        実請求ベース (答え合わせ層)
```

---

## 1. リクエストが APIM に届くまで

### クライアントが送るもの

```http
POST https://<apim>.azure-api.net/openai/deployments/chat-gpt-5.6-sol/chat/completions?api-version=2024-10-21
Ocp-Apim-Subscription-Key: <team-a のキー>       ← チームを決める。詐称できない
X-User-Id: alice                            ← 個人を名乗る。自己申告なので詐称できる
Content-Type: application/json

{"model": "chat-gpt-5.6-sol", "messages": [...], "max_completion_tokens": 300}
```

### この2つのヘッダーの違いが、以降すべてに効いてくる

| ヘッダー | 何を決めるか | 検証されるか | 使い道 |
| --- | --- | --- | --- |
| `Ocp-Apim-Subscription-Key` | **チーム** | APIM が照合する | 統制 (止める) |
| `X-User-Id` | **個人** | されない | 可視化 (見る) |

キーは APIM が持つサブスクリプション情報と突き合わされ、
一致しなければポリシーに入る前に **401** で弾かれます。

---

## 2. APIM のポリシーが順に実行される

`policies/main-policy.xml` が上から順に評価されます。
ここで作られた変数が、後段の計測にそのまま渡ります。

### ステップ1: allowlist

```
リクエストボディの model を読む
   ↓
context.Variables["requestModel"] = "chat-gpt-5.6-sol"
   ↓
Named value {{llm-allowed-models}} = "chat-gpt-5.6-sol" と照合
   ↓
一致しなければ → 403 を返してここで終了 (バックエンドを呼ばない)
```

**403 で終わった場合でも、requests テーブルには記録されます**（後述）。
「弾かれた回数」も可視化の対象だからです。

### ステップ2: 単価の解決

```
Named value {{llm-price-table}}
  = "chat-gpt-5.6-sol:5.0:30.0,__default__:30.0:120.0"
       ↑モデル名    ↑入力単価 ↑出力単価   (USD / 100万トークン)
   ↓
モデル名で一致する行を探す
   ↓
context.Variables["usdPerMillionInput"]  = "5.0"
context.Variables["usdPerMillionOutput"] = "30.0"
```

見つからなければ `__default__`（最高単価）にフォールバックします。
自動化が失敗したとき「過小計上 = 予算突破」ではなく
「過大計上 = 早めに気付く」側に倒すためです。

### ステップ3: 利用者の識別

```
リクエストヘッダー X-User-Id を読む
   ↓
空なら "unassigned"、64文字を超えるなら切り詰め
   ↓
context.Variables["userId"] = "alice"
```

64文字で切るのは、Azure Monitor が
**1 ディメンションあたり 100 種類まで**しか追跡しないためです。
異常な値でスロットを使い切らないようにしています。

### ステップ4: 天井 (llm-token-limit)

```
counter-key = context.Subscription.Id   ← チーム単位でカウンタが分離
token-quota = {{llm-monthly-token-quota}} = 1000000
   ↓
上限を超えていれば → 403 (クォータ) または 429 (レート制限)
   ↓
レスポンスヘッダーに残量を付与
   x-remaining-quota-tokens: 999985
```

**個人単位では止めていません。** `X-User-Id` は詐称できるので、
統制の根拠にできないからです。

### ステップ5: 計測 (llm-emit-token-metric)

ここまでで作った変数を、メトリクスのディメンションとして載せます。

```xml
<llm-emit-token-metric namespace="llmops">
    <dimension name="Team"                value="@(context.Subscription.Name)" />
    <dimension name="User"                value="→ userId" />
    <dimension name="Model"               value="→ requestModel" />
    <dimension name="UsdPerMillionInput"  value="→ usdPerMillionInput" />
    <dimension name="UsdPerMillionOutput" value="→ usdPerMillionOutput" />
</llm-emit-token-metric>
```

ディメンションは **1ポリシーあたり最大5個**。ここが上限いっぱいです。

### ステップ6: バックエンド呼び出し

```
Managed Identity で Foundry 用のトークンを取得
   ↓
Authorization: Bearer <MSI トークン> を付与
   ↓
Foundry へ転送
```

**開発者は Foundry の資格情報を一切持ちません。**

---

## 3. データが記録される先 (2つのテーブル)

APIM から Application Insights へは、**性質の違う2系統**が流れます。

|  | requests | customMetrics |
| --- | --- | --- |
| 出どころ | 診断設定 (Diagnostics) | `llm-emit-token-metric` ポリシー |
| 単位 | 1リクエスト = 1行 | 1リクエスト = 複数行 (トークン種別ごと) |
| 記録内容 | HTTP の結果 | トークン数と単価 |
| 用途 | 遮断回数、レイテンシ | コスト計算 |

### 3-1. requests テーブル (実物)

```
timestamp        = 2026-09-03T04:14:39.0774773Z
name             = POST /openai/responses
url              = https://<apim>.azure-api.net/openai/responses?api-version=2025-04-01-preview
resultCode       = 200
duration         = 2260.4065          ← ミリ秒
success          = True
operation_Name   = foundry-responses-api;rev=1 - create-response
client_IP        = 0.0.0.0            ← 既定でマスクされる
customDimensions = {
    "API Name"          : "foundry-responses-api",
    "Operation Name"    : "create-response",
    "Subscription Name" : "team-a",           ← チーム名がここに入る
    "HTTP Method"       : "POST",
    "Service Name"      : "apim-llmops-....azure-api.net",
    "Region"            : "Japan East",
    "Request Id"        : "13927b6c-...",
    "Cache"             : "None"
}
```

**このテーブルには X-User-Id は入りません。** 個人の識別は customMetrics 側にのみ載せています。

このテーブルから分かること:

| 知りたいこと | 使う列 |
| --- | --- |
| 遮断された回数 | `resultCode` が 403 / 429 |
| どの API が使われたか | `customDimensions["API Name"]` |
| どのチームか | `customDimensions["Subscription Name"]` |
| 応答時間 | `duration` |

### 3-2. customMetrics テーブル (実物)

```
timestamp        = 2026-09-03T04:14:41Z
name             = Prompt Tokens        ← トークンの種別
value            = 25857
valueCount       = 2                    ← 集約されたリクエスト数
valueSum         = 25857                ← 合計。金額計算にはこれを使う
valueMin         = 12926
valueMax         = 12931
customDimensions = {
    "Team"                    : "team-a",              ← ポリシーで付けた
    "User"                    : "unassigned",          ← ポリシーで付けた
    "Model"                   : "chat-gpt-5.6-sol",    ← ポリシーで付けた
    "UsdPerMillionInput"      : "5.0",                 ← ポリシーで付けた
    "UsdPerMillionOutput"     : "30.0",                ← ポリシーで付けた
    "Service ID"              : "apim-llmops-...",     ← APIM が自動で付ける
    "Service Name"            : "...azure-api.net",    ← APIM が自動で付ける
    "Region"                  : "Japan East",          ← APIM が自動で付ける
    "Service Type"            : "API Management",      ← APIM が自動で付ける
    "_MS.AggregationIntervalMs": "80000"               ← 集約された時間幅
}
```

#### ★ここが最重要: `value` ではなく `valueSum` を使う

メトリクスは**集約されて届きます**。上の例は 2 リクエスト分がまとまった1行です。

| 列 | 意味 |
| --- | --- |
| `valueCount` | まとめられたリクエスト数 (この例では 2) |
| **`valueSum`** | **合計。金額計算はこれを使う** |
| `valueMin` / `valueMax` | 内訳の最小・最大 |

#### ★もう一つの罠: timestamp は集約バケットの開始時刻

`_MS.AggregationIntervalMs` が示すとおり、この行は 80 秒分をまとめたものです。
そのため **`timestamp` はリクエストを送った時刻より前になり得ます**。

反映遅延を測るときに「送信時刻より新しいレコード」で探すと、永久に見つかりません。
件数の増加を見る方式にする必要があります。

#### 記録されるメトリクスの種類

1リクエストにつき、以下が**それぞれ別の行**として記録されます。

| name | 意味 | 金額計算に使うか |
| --- | --- | --- |
| **`Prompt Tokens`** | 入力トークン | ✅ 入力単価を掛ける |
| **`Completion Tokens`** | 出力トークン | ✅ 出力単価を掛ける |
| `Total Tokens` | 合計 | ❌ **上2つと重複。使うと二重計上** |
| `Prompt Cached Tokens` | キャッシュヒット分 | 参考 |
| `Completion Reasoning Tokens` | 推論トークン | 参考 |
| `Prompt Audio Tokens` 他 | 音声等 | 参考 |

---

## 4. 記録から金額を出すまで

### 計算式

```
推定コスト = Σ (valueSum × 該当する単価 ÷ 1,000,000)
```

「該当する単価」を `name` で選び分けるのが肝です。

```kusto
customMetrics
| where name in ('Prompt Tokens', 'Completion Tokens')
| extend Team   = tostring(customDimensions['Team']),
         User   = tostring(customDimensions['User']),
         UsdIn  = todouble(customDimensions['UsdPerMillionInput']),
         UsdOut = todouble(customDimensions['UsdPerMillionOutput'])
| extend UnitPrice = iff(name == 'Prompt Tokens', UsdIn, UsdOut)   ← ここで選び分ける
| summarize EstimatedUsd = sum(valueSum * UnitPrice / 1000000.0) by Team, User
```

入力と出力では単価が **6倍** 違うため、分けないと金額が大きくずれます。

### 実際の値の流れ (1リクエストの例)

```
リクエスト: 入力 72 トークン / 出力 873 トークン
   ↓ ポリシーが単価を解決
UsdPerMillionInput = "5.0" / UsdPerMillionOutput = "30.0"
   ↓ customMetrics に2行記録される
name="Prompt Tokens"     valueSum=72   dimensions{ UsdIn:5.0,  UsdOut:30.0 }
name="Completion Tokens" valueSum=873  dimensions{ UsdIn:5.0,  UsdOut:30.0 }
   ↓ KQL で name に応じて単価を選ぶ
入力: 72  × 5.0  ÷ 1,000,000 = $0.00036
出力: 873 × 30.0 ÷ 1,000,000 = $0.02619
   ↓
合計 $0.02655
```

---

## 5. 読み取り側 (3つの出口)

### 5-1. ワークブック (APIM の画面内)

```
dashboards/cost-workbook.json
   ↓ infra/modules/workbook.bicep が sourceId に APIM を指定
Azure ポータル → API Management → 監視 > ブック
```

`sourceId` を APIM のリソース ID にすることで、**APIM のページ内に表示されます**。
コストを見るために別のリソースを開く必要がありません。

| パネル | データ元 |
| --- | --- |
| 推定コスト合計 | customMetrics |
| チーム別 | customMetrics の `Team` |
| 個人別 | customMetrics の `User` |
| モデル別 | customMetrics の `Model` |
| 遮断された回数 | **requests の `resultCode`** |

最後の1つだけ、別テーブルを見ています。

### 5-2. ログ検索アラート

```
infra/modules/alerts.bicep
   ↓
① 推定コスト超過   customMetrics を KQL で金額換算 → しきい値超えで通知
② 429 検知        requests の resultCode = 429 → 発生で通知
   ↓
アクショングループ → メール
```

アラートのクエリでは `ago()` を書きません。
評価期間 (`windowSize`) が自動で適用されるため、書くと二重に絞られます。

### 5-3. Cost Management (答え合わせ層)

```
Foundry の課金レコード
   ↓ (数時間〜1日の遅延)
Cost Management
   ↓
予算アラート (infra/modules/budget.bicep)
   実績 80% / 予測 100% でメール通知
```

**この経路だけ APIM を通りません。** Foundry が直接 Azure の課金基盤に記録します。

だからこそ「答え合わせ」になります。上の層はすべて
「APIM が計測できた分」しか見えませんが、ここは実際に使われた分がすべて出ます。

---

## 6. 4層の位置づけ (どこを見ているか)

| 層 | 実装 | 見ているもの | 遅延 | 性質 |
| --- | --- | --- | --- | --- |
| **天井** | `llm-token-limit` | 自分が数えたトークン | なし | 確実だが粗い |
| **計測** | `llm-emit-token-metric` | 自分が数えたトークン × 自前の単価 | 約2分 | 推定値 |
| **検知** | ログ検索アラート | 上と同じデータ | 数分 | 通知のみ |
| **答え合わせ** | Cost Management | **実際の課金** | 数時間〜1日 | 唯一の正解 |

### ★上3層には共通の弱点がある

**すべて「APIM が計測できた分」しか見ていません。**

実際にこの弱点を踏みました。上限超過の検証用に別 API を作った際、
そこに `llm-emit-token-metric` と診断設定を付け忘れていたため、
**出力トークンの 99.6% が集計から漏れていました**。

|  | 入力 | 出力 |
| --- | --- | --- |
| APIM が計測 | 13,335 | 176 |
| 実際の消費 | 16,868 | 42,770 |

推定 $0.07 に対し、実請求 $1.30。**19倍の乖離**です。

しかも Foundry の実トークン数に**同じ単価表**を掛けたら、
実請求と 5% 以内で一致しました。つまり単価表も計算式も正しく、
**入力データだけが欠けていた**ことになります。

**計測漏れは、計測の仕組みでは検出できません。**
自分が測った数字だけを見ていても、測っていない場所には気づけない。
だから 4 層目が要ります。

---

## 7. 単価表の更新経路 (別系統)

```
[Azure Functions (Timer Trigger)]
     │  毎日 UTC 18:00 (JST 翌 3:00)
     │
     ├─▶ ① Azure Retail Prices API から価格を取得 (認証不要)
     │       serviceName = "Foundry Models"
     │       armRegionName = "japaneast"
     │
     ├─▶ ② meter-mapping.json でモデル名と品名を対応付け
     │       "chat-gpt-5.6-sol" ⇔ "5.6 sol ShortCo Inp Std Gl 1M Tokens"
     │
     └─▶ ③ APIM の Named value を更新 (Managed Identity で認証)
             llm-price-table = "chat-gpt-5.6-sol:5.0:30.0,__default__:30.0:120.0"
                                    ↓
                        次のリクエストから、この単価で計算される
```

### ここが自動と手動の境界

|  | 頻度 | 誰が |
| --- | --- | --- |
| **単価の値** | 毎日 | **機械** |
| モデル名と品名の対応 | 新モデル追加時のみ | **人** |

`meterName` は `5.6 sol ShortCo Inp Std Gl 1M Tokens` という表記で、
`ShortCo`（ショートコンテキスト）や `Std Gl`（Standard Global）といった、
モデル名に含まれない属性まで入っています。
そのため**文字列一致では特定できず**、対応表だけは人が書く必要があります。

新しいモデルを追加するときに人が触るのは、次の2箇所だけです。

1. `llm-allowed-models` に追加（追加しないと 403 で弾かれる）
2. `meter-mapping.json` に対応3行を追加

単価そのものは、翌朝の同期で自動的に入ります。

---

## 8. 各リソースの役割一覧

| リソース | 役割 | データが入るか |
| --- | --- | --- |
| API Management | ゲートウェイ本体。ポリシーを実行 | — |
| Microsoft Foundry | モデルの実行 | — |
| **Application Insights** | requests / customMetrics を保持 | ✅ |
| **Log Analytics ワークスペース** | App Insights のバッキングストア | ✅ |
| ログ検索アラート × 2 | 推定コスト超過 / 429 検知 | — |
| ワークブック | 可視化 (APIM の画面に表示) | — |
| Function App | 単価表の定期同期 | — |
| ストレージ | Function のコードと実行状態 | — |
| Cost Management 予算 | 実請求ベースの通知 | — |

Application Insights は**ワークスペースベース**で構成しており、
実データは Log Analytics ワークスペースに保存されます。
App Insights はその上のクエリ層という位置づけです。

---

## 9. 認証がどこで行われているか

シークレットを持たない構成にしています。

| 区間 | 認証方法 | シークレットの保存 |
| --- | --- | --- |
| クライアント → APIM | サブスクリプションキー | クライアント側のみ |
| **APIM → Foundry** | **Managed Identity** | **なし** |
| **Function → APIM** | **Managed Identity** | **なし** |
| **Function → ストレージ** | **Managed Identity** | **なし** (共有キー認証は無効化) |
| Function → Retail Prices API | 不要 (認証なしの公開 API) | — |

開発者が持つのは APIM のキーだけで、Foundry の資格情報は誰にも配られません。
