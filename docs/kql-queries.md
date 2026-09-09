# KQL クエリ集: LLM トークン消費の可視化

`policies/main-policy.xml` の `llm-emit-token-metric` が Application Insights の
`customMetrics` テーブルに送るデータを集計するためのクエリ集です。

## 前提: 実際に記録されるデータの形

`llm-emit-token-metric` は 1 リクエストにつき複数の `customMetrics` 行を出力します
(2026-09 時点の実測)。

| `name` | 意味 |
| --- | --- |
| `Prompt Tokens` | 入力トークン数 |
| `Completion Tokens` | 出力トークン数 |
| `Total Tokens` | 合計トークン数 |
| `Prompt Cached Tokens` | キャッシュヒットした入力トークン数 |
| `Completion Reasoning Tokens` | 推論(reasoning)トークン数 |
| `Prompt Audio Tokens` / `Completion Audio Tokens` | 音声トークン数 |
| `Completion Accepted/Rejected Prediction Tokens` | 予測デコード関連 |

値は `valueSum` に入ります (`value` ではありません。集約済みメトリクスのため)。

ポリシーで付与しているカスタムディメンション:

| ディメンション | 内容 |
| --- | --- |
| `Team` | APIM サブスクリプション表示名 (= チーム名) |
| `SubscriptionId` | APIM サブスクリプション ID |
| `Model` | リクエストボディの `model` |
| `UsdPerMillionInput` | 単価表から引いた**入力**単価 (USD / 100万トークン) |
| `UsdPerMillionOutput` | 単価表から引いた**出力**単価 (USD / 100万トークン) |

`llm-emit-token-metric` のカスタムディメンションは **最大5個** なので、これが上限いっぱいです。

APIM が自動で付ける既定ディメンション: `Region`, `Service ID`, `Service Name`, `Service Type`。

> 注意1: `Total Tokens` は `Prompt Tokens` + `Completion Tokens` と重複します。
> 金額を計算するときに 3 つすべてを合計すると **二重計上** になります。
> 下記のクエリは `Prompt Tokens` と `Completion Tokens` のみを使います。
>
> 注意2: `timestamp` は **集計バケットの開始時刻** であり、リクエストを送った時刻より
> 前になり得ます (`_MS.AggregationIntervalMs` 参照)。
> 「送信時刻より新しいレコード」という条件で探すと見つからないことがあります。

---

## 1. チーム別トークン消費量 (直近 24 時間)

```kusto
customMetrics
| where timestamp > ago(24h)
| where name in ('Prompt Tokens', 'Completion Tokens')
| extend Team = tostring(customDimensions['Team'])
| where isnotempty(Team)
| summarize Tokens = sum(valueSum) by Team, name
| order by Team asc, name asc
```

## 2. チーム別の推定コスト (USD)

入力トークンと出力トークンは単価が異なるため、`name` に応じて単価を選び分けます。
これが「入力と出力で単価を分けて計算する」という設計ルールの実装です。

```kusto
customMetrics
| where timestamp > ago(24h)
| where name in ('Prompt Tokens', 'Completion Tokens')
| extend Team  = tostring(customDimensions['Team']),
         Model = tostring(customDimensions['Model']),
         UsdIn  = todouble(customDimensions['UsdPerMillionInput']),
         UsdOut = todouble(customDimensions['UsdPerMillionOutput'])
| where isnotempty(Team)
| extend UnitPrice = iff(name == 'Prompt Tokens', UsdIn, UsdOut)
| summarize Tokens = sum(valueSum),
            EstimatedUsd = sum(valueSum * UnitPrice / 1000000.0)
          by Team, Model
| order by EstimatedUsd desc
```

> これはあくまで「自前の単価表 × トークン数」による**推定**です。
> 割引契約や課金上の丸めは反映されないため、実際の請求額とは一致しません。
> 突き合わせは Cost Management の予算アラート (答え合わせ層) で行います。

## 3. 時系列でのトークン消費推移 (チーム別・1時間バケット)

```kusto
customMetrics
| where timestamp > ago(7d)
| where name == 'Total Tokens'
| extend Team = tostring(customDimensions['Team'])
| where isnotempty(Team)
| summarize Tokens = sum(valueSum) by bin(timestamp, 1h), Team
| render timechart
```

## 4. モデル別の利用割合

```kusto
customMetrics
| where timestamp > ago(24h)
| where name == 'Total Tokens'
| extend Model = tostring(customDimensions['Model'])
| where isnotempty(Model)
| summarize Tokens = sum(valueSum) by Model
| order by Tokens desc
```

## 5. allowlist で弾かれたリクエスト (403 model_not_approved)

ポリシーの `return-response` による 403 は APIM の診断ログ (`requests` テーブル) に残ります。

```kusto
requests
| where timestamp > ago(24h)
| where resultCode == '403'
| summarize Count = count() by bin(timestamp, 1h), name
| order by timestamp desc
```

## 6. トークン上限に到達したリクエスト (429)

`llm-token-limit` が上限超過を検知すると 429 を返します。

```kusto
requests
| where timestamp > ago(24h)
| where resultCode == '429'
| summarize Count = count() by bin(timestamp, 1h), name
| order by timestamp desc
```

## 7. アラート用: 1時間あたりの推定コストがしきい値を超えたチーム

`infra/modules/alerts.bicep` のログ検索アラートで使用しているクエリです。

```kusto
customMetrics
| where name in ('Prompt Tokens', 'Completion Tokens')
| extend Team = tostring(customDimensions['Team']),
         UsdIn  = todouble(customDimensions['UsdPerMillionInput']),
         UsdOut = todouble(customDimensions['UsdPerMillionOutput'])
| where isnotempty(Team)
| extend UnitPrice = iff(name == 'Prompt Tokens', UsdIn, UsdOut)
| summarize EstimatedUsd = sum(valueSum * UnitPrice / 1000000.0) by Team
| where EstimatedUsd > 1.0
```

> ログ検索アラートでは評価期間 (`windowSize`) が自動で適用されるため、
> クエリ内に `ago()` を書きません。書くと二重に期間が絞られます。

## 8. メトリクスの反映遅延を確認する

`timestamp` は集計バケットの開始時刻なので、遅延を測るときは
「時刻で絞る」のではなく「件数の増加を見る」のが確実です。

```kusto
customMetrics
| where timestamp > ago(2h)
| where isnotempty(tostring(customDimensions['Team']))
| summarize Count = count()
```

本検証環境での実測値: リクエスト送信からクエリで見えるまで **中央値 約126秒**（2試行）。
「即座に止める」ことを要件から外した根拠のひとつです。
