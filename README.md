# APIM で始める FinOps for AI

Azure API Management (APIM) を AI Gateway として使い、
**開発者の AI 利用料をチーム別・個人別に把握し、必要なら止められるようにする**検証リポジトリです。

JAZUG (Japan Azure User Group) 勉強会での発表用に、構成・ポリシー・検証結果を
再現可能な形で公開しています。数値はすべて、この検証環境で実際に取得したものです。

結論から書くと、**「金額で即座に止める」ことはできませんでした。**
なぜできないのか、ではどうしたのかを、実測値とともに記録しています。

> - 発表スライドの骨子: `docs/JAZUG-LLMOps-Copilot-context.md`
> - データが流れる全経路の解説: `docs/architecture-data-flow.md`
> - 検証結果のまとめ: `docs/verification-summary.md`

## アーキテクチャ概要

```
[Codex CLI など OpenAI 互換クライアント]
        │ OpenAI 互換 (Chat Completions / Responses API)
        ▼
[Azure API Management (Developer SKU)]
    - モデル allowlist ポリシー
    - llm-token-limit (天井)
    - llm-emit-token-metric (計測)
        │ Managed Identity 認証
        ▼
[Microsoft Foundry (Cognitive Services, kind=AIServices)]
    - モデルデプロイ (既定: gpt-5.6-sol, Japan East)
        │
        ▼
[Application Insights] → [Azure Monitor ログアラート] / [Cost Management 予算アラート]
```

APIM には 2 つの API を公開しています。

| API (path) | 用途 | ポリシー |
| --- | --- | --- |
| `openai` | 本番相当。チーム別サブスクリプション (team-a/b/c) から利用 | `policies/main-policy.xml` |
| `openai-quota-demo` | トークン上限超過の実地検証専用 (`quota-demo` サブスクリプション) | `policies/quota-demo-policy.xml` |

上限超過の検証を本番相当の API で行うと、`llm-token-limit` のカウンタは期間が来るまで
戻せないため本番側の統制まで巻き添えになります。そのため API とサブスクリプションを分離しています。

## 3層 + 検算

コスト統制を 1 つの仕組みで完結させず、性質の違う層に分けて持ちます。

| 層 | 実装 | 特性 | 実装箇所 |
| --- | --- | --- | --- |
| 天井 | `llm-token-limit` | 確実・粗い・追加コスト0。トークン数でしか制限できない | `policies/main-policy.xml` |
| 計測 | `llm-emit-token-metric` → Application Insights | 単価表 × トークン数の**推定**。反映に約2分 | `policies/main-policy.xml` |
| 検知 | Azure Monitor ログ検索アラート (KQL で金額換算) | 推定値に対する閾値通知 | `infra/modules/alerts.bicep` |

上の3層はすべて **APIM を通ったリクエスト**を数え、自前の単価表で金額に換算しています。
つまり「自分が測れた分」しか見えません。

| | 実装 | 位置づけ | 実装箇所 |
| --- | --- | --- | --- |
| **検算** | Cost Management 予算アラート | **上3層とは別系統**。APIM を通らず Azure の課金基盤から直接来る | `infra/modules/budget.bicep` |

### なぜ検算が要るのか (実際に19倍ずれた)

推定値と実請求を突き合わせたところ、**約19倍の乖離**がありました。

| | 金額 |
| --- | --- |
| APIM の計測にもとづく推定 | **$0.069** |
| 実請求 (Cost Management) | **$1.30** |

原因は単価表でも計算式でもありませんでした。
上限超過の検証用に作った別 API に `llm-emit-token-metric` と診断設定を
**付け忘れていた**ため、出力トークンの 99.6% が集計から漏れていたのです。

Foundry 側が数えた実トークン数に**同じ単価表**を掛けたら、実請求と 5% 以内で一致しました。
仕組みは正しく、**入力データだけが欠けていた**ことになります。

**計測漏れは、計測の仕組みでは検出できません。**
自分が測った数字だけを見ていても、測っていない場所には気づけない。
だから APIM を通らない経路の数字と突き合わせる必要があります。

なお予算アラートは**通知するだけで支出を止めません**。止めるのは APIM 側の天井です。

## リポジトリ構成

| パス | 内容 |
| --- | --- |
| `infra/main.bicep` | サブスクリプションスコープのエントリポイント |
| `infra/modules/apim.bicep` | APIM 本体・API 定義・Named values・ポリシー適用・診断設定 |
| `infra/modules/alerts.bicep` | 推定コスト / 429 検知のログ検索アラート |
| `infra/modules/budget.bicep` | Cost Management 予算アラート (答え合わせ層) |
| `infra/modules/workbook.bicep` | コスト可視化ワークブック (APIM の「監視 > ブック」に表示) |
| `infra/modules/pricesync.bicep` | 単価表を毎日同期する Function (Flex Consumption) |
| `functions/` | 単価同期 Function 本体。同期ロジックは `price_sync` に集約 |
| `dashboards/cost-workbook.json` | ワークブックの表示定義 |
| `policies/main-policy.xml` | allowlist + token-quota + emit-token-metric |
| `policies/quota-demo-policy.xml` | 上限超過検証専用 (上限値は Named values 経由で切替) |
| `scripts/verify.py` | 検証項目 A1–A5 / B1–B8 の自動検証 |
| `scripts/quota_overshoot.py` | トークン上限の「超過量」の定量化 |
| `scripts/set_quota_demo_limits.py` | 検証モード切替 (ratelimit=429用 / quota=403用) |
| `scripts/update_prices.py` | 単価表の手動更新 / `--dry-run` での差分確認 |
| `scripts/apply_policy.py` | 検証サイクル用にポリシーだけを即時適用する補助ツール |
| `docs/kql-queries.md` | 可視化・アラート用 KQL クエリ集 |

## 前提条件

- Azure CLI (`az`) がインストール済み、対象テナント/サブスクリプションにログイン済み
  - このリポジトリは検証時、Azure CLI で既にログイン済みのテナント/サブスクリプションをそのまま利用します。
  - **テナント名・サブスクリプションID・組織固有の情報はこのリポジトリ・Bicep・スクリプトのいずれにもハードコードしません。** 実行時にログイン中のコンテキストを使うか、環境変数/コマンドライン引数で都度指定してください。
- Azure Developer CLI (`azd`) (任意。`azd up` で一括デプロイする場合)
- Bicep CLI (`az bicep` は Azure CLI に同梱)
- Python 実行環境: [uv](https://docs.astral.sh/uv/) を使用（フェーズ3以降の検証・価格更新スクリプト用）

## デプロイ手順

### 方法A: azd を使う場合

```powershell
azd auth login   # 既にログイン済みなら不要
azd up
```

### 方法B: Azure CLI を直接使う場合

```powershell
az deployment sub create `
  --location japaneast `
  --name apim-llmops-deploy `
  --template-file infra/main.bicep `
  --parameters environmentName=<任意の環境名> location=japaneast `
               publisherEmail=<自分のメールアドレス> `
               publisherName="<組織名>"
```

デプロイには APIM (Developer SKU) のプロビジョニングに **30〜45分程度** かかります。

### デプロイ後の確認

```powershell
az deployment sub show --name apim-llmops-deploy --query properties.outputs
```

`APIM_GATEWAY_URL` / `MODEL_DEPLOYMENT_NAME` / `SUBSCRIPTION_KEYS` などが出力されます。

## 破棄手順

```powershell
az group delete --name rg-apim-llmops --yes --no-wait
```

（`azd down` でも同様に削除できます）

## CLI からの接続

APIM は OpenAI 互換エンドポイントとして公開しています。
デプロイ出力の `SUBSCRIPTION_KEYS` にはサブスクリプション名と ID のみが含まれます
(キーは出力しません)。キーの値は次のように取得してください。

```powershell
az apim subscription show --resource-group <rg> --service-name <apim> `
  --sid team-a --query primaryKey -o tsv
```

### 実測した結果

| CLI | 結果 | 理由 |
| --- | --- | --- |
| **Codex CLI** | ✅ 接続できた | OpenAI 互換形式で、ヘッダー名を指定できる |
| Claude Code | ❌ 接続できない | **Anthropic Messages 形式** (`/v1/messages`) を送るため 404 |
| GitHub Copilot CLI | ❌ 接続できない | `Authorization: Bearer` を送るが、APIM のキー照合と噛み合わない |

「どの CLI でも使える」とは言えませんでした。正確には
**「OpenAI 互換の形式を話すクライアントなら繋がる」**です。

### Codex CLI (接続確認済み)

`~/.codex/config.toml` にプロバイダーを追加します。

```toml
[model_providers.apim-llmops]
name = "APIM LLMOps Gateway"
base_url = "<APIM_GATEWAY_URL>/openai"
wire_api = "responses"
query_params = { "api-version" = "2025-04-01-preview" }
env_http_headers = { "Ocp-Apim-Subscription-Key" = "APIM_LLMOPS_KEY" }
```

```powershell
$env:APIM_LLMOPS_KEY = "<team-a のサブスクリプションキー>"

codex exec --skip-git-repo-check `
  -c model_provider=apim-llmops `
  -c model=chat-gpt-5.6-sol `
  "hello"
```

接続時に判明した点:

- 新しい Codex CLI は `wire_api = "chat"` を廃止しており、**Responses API のみ**対応
- `/responses` は **`api-version=2025-04-01-preview`** が必要 (`2024-10-21` では 404)
- 任意のヘッダー名を送れる `env_http_headers` があるため、APIM の
  `Ocp-Apim-Subscription-Key` にそのまま対応できる

### Claude Code が接続できない理由

Claude Code は Anthropic Messages 形式を送ります。

```
POST <APIM_GATEWAY_URL>/openai/v1/messages?beta=true
```

本リポジトリの API は OpenAI 互換のみを公開しているため 404 になります。
**認証エラーではなく、パスが存在しない**という状態です。

APIM の Anthropic 対応は「**Claude モデルをバックエンドに置ける**」という意味であり、
「Anthropic 形式のクライアントを受けられる」ではありません。
Claude Code を通すには、Anthropic 形式を受ける API を別途用意する必要があります。

なお `llm-token-limit` / `llm-emit-token-metric` の Anthropic Messages API 対応は
**v2 ティア限定**のため、本リポジトリ (Developer SKU) では計測が効かない可能性があります。
ここは未検証です。

### GitHub Copilot CLI が接続できない理由

`Authorization: Bearer <key>` を送りますが、APIM のサブスクリプションキー照合は
**ヘッダー値の完全一致**で行われるため、`Bearer ` 接頭辞があると 401 になります。

| 送り方 | 結果 |
| --- | --- |
| `Ocp-Apim-Subscription-Key: <key>` | 200 |
| `Authorization: <key>` (接頭辞なし) | 200 |
| `?subscription-key=<key>` | 200 |
| **`Authorization: Bearer <key>`** | **401** |

キー検証はポリシーより前に実行されるため、グローバルスコープのポリシーで
接頭辞を取り除くこともできませんでした (実測で確認)。

回避するには OAuth/JWT 検証を正式に導入するか、キー検証を無効化して自前検証するしかありません。
後者は `counter-key` と Team ディメンションも同時に壊すため採用していません。

## コスト可視化

### ポータルで見る

**API Management → 監視 → ブック → 「LLM 利用料の可視化」**

ワークブックの `sourceId` を APIM に向けているため、APIM のページ内から直接開けます。
コストを見るために別のリソースを開く必要はありません。

表示内容:

| セクション | 内容 |
| --- | --- |
| 全体サマリ | 推定コスト合計 / 総トークン数 |
| チーム別 | サブスクリプション単位の内訳と推移 |
| 個人別 | `X-User-Id` 単位の内訳 (`unassigned` = 未申告) |
| モデル別 | モデルごとの内訳 |
| 遮断された回数 | 403 / 429 の発生状況 |

### 集計の単位: チームと個人を分けている

| | 単位 | 何で決まるか | 詐称 | 用途 |
| --- | --- | --- | --- | --- |
| **統制** (止める) | チーム | APIM サブスクリプションキーの保持 | できない | `llm-token-limit` の `counter-key` |
| **可視化** (見る) | 個人 | リクエストヘッダー `X-User-Id` | **できる** | 按分・気づき |

個人単位では**上限をかけていません**。`X-User-Id` は自己申告なので統制の根拠にできないことと、
個人ごとの上限は運用が回らなくなりやすいためです。

クライアント側の設定例:

```powershell
# CLI から個人を名乗る (可視化のためのラベル。認証ではない)
curl -H "Ocp-Apim-Subscription-Key: <チームのキー>" `
     -H "X-User-Id: your-name" ...
```

ヘッダーを送らない場合は `unassigned` に集計されます。ここが大きい場合、
CLI 側の設定が配布できていないことを意味します。

## 単価表の自動同期

モデルの単価は改定されます。手動更新の運用では必ず陳腐化し、
古い単価で計算した「推定コスト」を正しい数字だと誤認することになります。

そこで **Azure Functions (Timer Trigger)** で毎日同期しています。

```
Retail Prices API ──> Function (毎日 UTC 18:00) ──> APIM の Named value
                       Managed Identity で認証        llm-price-table
```

- **シークレットを一切保存しません**。APIM への書き込みもストレージへの接続も Managed Identity 経由です
  (ストレージは共有キー認証自体を無効化しています)
- 単価が引けなかった場合は**更新せず例外にします**。黙って古い値を残すと過小計上になり、
  予算超過に気付けなくなるためです
- `__default__` (最高単価のフォールバック) は自動更新の対象外で、常に安全側を維持します

### 手動で確認・更新する

```powershell
# 差分だけ確認する (更新しない)
uv run --with-requirements scripts/requirements.txt scripts/update_prices.py `
  --resource-group rg-apim-llmops --apim-name <APIM名> --dry-run

# 実際に更新する
uv run --with-requirements scripts/requirements.txt scripts/update_prices.py `
  --resource-group rg-apim-llmops --apim-name <APIM名>
```

同期ロジックは `functions/price_sync` にあり、Function と手動スクリプトで共有しています
(同じ処理を二重に持たないため)。

### 自動化できなかった部分

`functions/price_sync/meter-mapping.json` の**対応表は人間が維持する必要があります**。

Retail Prices API 上の品名 (`meterName`) は `5.6 sol ShortCo Inp Std Gl 1M Tokens` のような
独自表記で、モデルデプロイ名から機械的に導出できません。
新しいモデルを追加したら、この対応表にも追記が必要です。

### Function のコードをデプロイする

インフラ (`az deployment sub create`) は Function App の「器」だけを作ります。
コード本体は Azure Functions Core Tools で配置します。

```powershell
# Core Tools を入れていない場合
npm install -g azure-functions-core-tools@4 --unsafe-perm true

cd functions
func azure functionapp publish <FUNCTION_APP_NAME> --python --build remote
```

`FUNCTION_APP_NAME` はデプロイ出力の `PRICE_SYNC_FUNCTION_APP_NAME` を参照してください。

`--build remote` を付けると依存関係が Azure 側でインストールされます。
zip を手作りして配置する方法は、Windows で作った zip のパス区切りが原因で
サブパッケージを読み込めなくなるため推奨しません。

## 検証結果 (実機検証済み)

Azure 上に実際にデプロイした検証環境 (Japan East) で取得した実測値です。
数値はすべてこの検証環境で新たに取得したものです。

### A. 必須項目

| # | 検証項目 | 実測結果 | 判定 |
| --- | --- | --- | --- |
| A1 | APIM 経由で Foundry を叩ける | HTTP 200 | 成功 |
| A2 | `counter-key` がサブスクリプション単位で分離される | team-a と team-b で残量が独立 | 成功 |
| A3 | トークン上限超過時のステータス | レート制限超過 → **429** / クォータ超過 → **403 Quota Exceeded** | 成功 |
| A4 | カスタムディメンションが Application Insights に届く | `Team`/`Model`/入力単価/出力単価 が付与された状態で記録 | 成功 |
| A5 | KQL で金額に換算できる | チーム別の推定 USD を算出 | 成功 |

### B. 挙動確認

| # | 検証項目 | 実測結果 | 判定 |
| --- | --- | --- | --- |
| B1 | ストリーミング時に応答本文から `usage` が取れるか | 取れない (実地確認) | 成功 |
| B2 | `tokens-consumed-variable-name` でストリーミング時もトークン数が取れるか | ポリシー変数はレスポンスから確認できず | **未検証** |
| B3 | Named values をポリシーから参照できるか | allowlist 経由で間接確認 | 成功 |
| B4 | 未知モデルが安全側に倒れるか | allowlist が先に 403 で遮断（`__default__` には到達しない） | 成功 |
| B5 | Retail Prices API から単価が引けるか | `Foundry Models` で 12 件の該当 meter を取得 | 成功 |
| B6 | 同時実行でトークン制限を超過するか | **並列5で上限1000に対し1611トークン消費 = 61.1%超過、全て200** | 成功 |
| B7 | allowlist で未承認モデルを 403 にできるか | HTTP 403 `model_not_approved` | 成功 |
| B8 | メトリクスの反映時間 | **中央値 126.4秒**（2試行: 126.2s / 126.6s） | 成功 |

### 発表の核心: トークン上限は「超える」

トークン消費量は応答が返るまで確定しないため、上限判定はどうしても後追いになります。

| シナリオ | 上限 (tokens-per-minute) | 実消費 | 超過 |
| --- | --- | --- | --- |
| 逐次 3 リクエスト | 1000 | 961 | なし |
| **同時 5 リクエスト** | 1000 | **1579** | **+579 (57.9%)** |
| **同時 5 リクエスト (再実行)** | 1000 | **1611** | **+611 (61.1%)** |

逐次なら上限内に収まるのに、同時実行では 6 割ほど超えます。
これは実装の不備ではなく、「消費量が事前に分からない」ことによる原理的な帰結です。
だからこそ「即座に止める」を要件から外し、天井・計測・検知・答え合わせの 4 層に分けています。

### 単価 (Retail Prices API から自動取得)

| モデル | 入力 (USD/1M) | 出力 (USD/1M) |
| --- | --- | --- |
| `chat-gpt-5.6-sol` | 5.0 | 30.0 |

単価はコードに書かず、`scripts/update_prices.py` が Retail Prices API から取得して
Named value `llm-price-table` を更新します。

### 自動検証スクリプト

```powershell
uv run --with-requirements scripts/requirements.txt scripts/verify.py `
  --gateway-url <APIM_GATEWAY_URL> --deployment chat-gpt-5.6-sol `
  --subscription-key <team-aのキー> `
  --quota-test-subscription-key <quota-demoのキー> `
  --app-insights-app-id <Application Insights の App ID>
```

`--app-insights-api-key` を省略した場合、`az` CLI の Azure AD トークンで
Application Insights のクエリ API を呼び出します。

### トークン上限の超過量の定量化

```powershell
# 実験1: レート制限の超過量を測る (429)
uv run --with-requirements scripts/requirements.txt scripts/set_quota_demo_limits.py `
  --resource-group rg-apim-llmops --apim-name <APIM名> --mode ratelimit

uv run --with-requirements scripts/requirements.txt scripts/quota_overshoot.py `
  --gateway-url <APIM_GATEWAY_URL> --deployment chat-gpt-5.6-sol `
  --subscription-key <quota-demo のキー> `
  --tokens-per-minute 1000 --concurrency 5

# 実験2: クォータ超過で 403 が返ることを見る
uv run --with-requirements scripts/requirements.txt scripts/set_quota_demo_limits.py `
  --resource-group rg-apim-llmops --apim-name <APIM名> --mode quota
```

結果は `results/quota-overshoot-<timestamp>.json` と
`results/verification-<timestamp>.jsonl` に保存されます。
未検証・失敗した項目は「未検証」「失敗」として明記し、成功したことにはしません。

### ポリシー実装上の注意 (ハマりどころ)

APIM のポリシー XML は属性値の中に C# 式を書くため、XML エスケープを誤ると
`ValidationError` (400) や実行時 500 エラーになります。本リポジトリでは以下を徹底しています。

- ジェネリクス `<T>` は `&lt;T&gt;` にエスケープする。
- C# の論理演算子 `&&` は `&amp;&amp;` にエスケープする。
- 属性値の中の C# 文字列リテラルは、XML の属性区切り文字 (`"`) と衝突するため
  `&quot;` を使う。なお **C# ではシングルクォートは文字リテラル**なので、
  文字列の代用にはできない (`Too many characters in character literal` になる)。
- `llm-emit-token-metric` は **inbound セクションでのみ使用可能**
  ([公式ドキュメント](https://learn.microsoft.com/azure/api-management/llm-emit-token-metric-policy)参照)。outbound には置けない。
  「usage が確定してから outbound で計測する」という直感は誤りで、
  トークン数はポリシーエンジン内部で応答から取得される。
- カスタムディメンションは **1ポリシーあたり最大5個**。
  本リポジトリは `Team`/`SubscriptionId`/`Model`/入力単価/出力単価 で上限ちょうど。
- Named values に JSON 文字列を格納し `JObject.Parse("{{name}}")` のように
  ポリシー内で展開すると、値の中の `"` が XML パーサーと衝突する。
  本リポジトリでは単価表・allowlist を JSON ではなくカンマ区切り (CSV) 文字列で
  Named value に格納し、ポリシー内で `string.Split` する方式に変更した。

### 検証・計測まわりのハマりどころ

- **レート制限とクォータは別物**。`tokens-per-minute` 超過は **429**、
  `token-quota` 超過は **403**。両方を同じ API で試す場合、レート制限が先に効いて
  403 に到達できないことがある。本リポジトリでは上限値を Named values 経由にして
  `scripts/set_quota_demo_limits.py` でモードを切り替えられるようにしている。
- **`llm-token-limit` のカウンタは値を変えてもリセットされない**。
  期間が明けるまで消費分は戻らないため、上限超過の検証は
  本番相当の API とは別の API・別サブスクリプションで行う。
- **`customMetrics` の `timestamp` は集計バケットの開始時刻**
  (`_MS.AggregationIntervalMs` 参照) で、リクエスト送信時刻より前になり得る。
  反映遅延を測るときに「送信時刻より新しいレコード」で探すと永久に見つからない。
  件数の増加を見る方式にする。
- **`Total Tokens` は `Prompt Tokens` + `Completion Tokens` と重複する**。
  金額計算で3つとも合計すると二重計上になる。
- **Retail Prices API の `serviceName` は `Azure OpenAI` ではなく `Foundry Models`**。
  さらに `meterName` は `5.6 sol ShortCo Inp Std Gl 1M Tokens` のような独自表記で、
  モデルデプロイ名から機械的に導出できない。
  `functions/price_sync/meter-mapping.json` で人手による対応付けが必要 (自動化できない部分)。
- **PowerShell から `curl.exe -d` に文字列を直接渡すと BOM 混入で 400/500 になる**ことがある。
  BOM なし UTF-8 のファイルに書き出して `--data-binary "@file"` で渡すのが確実。
- **Functions のデプロイは Core Tools を使う**。Windows の `Compress-Archive` で作った zip は
  パス区切りがバックスラッシュになり、Linux 上のランタイムがサブパッケージを
  ディレクトリとして認識できず `ModuleNotFoundError` になる。

## 既知の制約・未検証事項

正直に区別するため、実測できていないものはここに明記します。

- **B2 (`tokens-consumed-variable-name`) は未検証**。ポリシー変数はレスポンスから
  直接確認できず、診断ログ経由でのトレース確認が必要なため。
- **`token-quota-period="Monthly"` での 403 は未実測**。月次カウンタは一度消費すると
  期間が明けるまで戻せないため、検証は `Hourly` で行った。
  ステータスコードの挙動 (403) は同じだが、期間の違いは明記しておく。
- **`__default__` フォールバックは実際には発動しない**。allowlist が先に 403 で
  遮断するため。単価表には allowlist をすり抜けた場合の保険として残している。
- **Claude Code からの接続は未検証** (下記 CLI セクション参照)。

### 設計上の限界 (公開前レビューで判明)

この構成は検証を目的としたもので、そのまま本番に置けるものではありません。
公開前のセキュリティレビューで指摘された点を、修正せず正直に記録します。

- **allowlist はリクエストボディの `model` しか検査していない。**
  この API には `POST /openai/deployments/{deployment-id}/chat/completions` という
  URL でデプロイ先を指定する経路もあり、そちらは allowlist を通らずバックエンドへ
  転送される。つまり**キーの保持者は、未承認のモデルデプロイを URL 経由で呼べてしまう**。
  さらに `llm-emit-token-metric` の `Model` ディメンションはボディ側の値を記録するため、
  この経路を使われると**課金の帰属も誤る**。
  本番で使うなら `context.Request.MatchedParameters["deployment-id"]` も
  allowlist 照合に含めるか、URL 経由の経路自体を公開しないこと。

- **検証専用 API `openai-quota-demo` には allowlist が無い。**
  上限超過 (429/403) を短時間で再現するためだけのもので、素通しでバックエンドに
  転送する。`infra/main.bicep` から無条件でデプロイされ、専用サブスクリプションも
  常時有効なため、本番構成に緩い経路が同居する形になっている。
  本番では条件デプロイにするか、同じ allowlist を適用すること。

- **Foundry でローカル認証 (API キー) を有効のままにしている**
  (`disableLocalAuth: false` かつ `publicNetworkAccess: 'Enabled'`)。
  この構成は「開発者に Foundry の資格情報を配らず、ゲートウェイだけが
  マネージド ID で持つ」という前提に立っているが、アカウントキーが1つ漏れれば
  APIM を完全に迂回して直接モデルを呼べる。その経路には allowlist もトークン上限も
  計測も効かない。しかも APIM に付与している `Cognitive Services User` は
  `accounts/listKeys/action` を含むため、ゲートウェイ側からキーを取得することもできる。
  本番では `disableLocalAuth: true` にし、ロールを `Cognitive Services OpenAI User` に
  絞り、可能なら Private Endpoint で閉じること。

- **単価同期 Function の権限が過剰。**
  Named value を1つ書き換えるだけなのに `API Management Service Contributor` を
  付与している。このロールは**全サブスクリプションキーの列挙とポリシーの書き換え**を
  含むため、Function が侵害されると統制そのものを外されうる。
  本番では Named value の読み書きだけを持つカスタムロールに絞ること。

- **全サブスクリプションで `allowTracing: true` にしている。**
  APIM のリクエストトレースはポリシー実行の全過程を記録し、そこには
  ポリシーがバックエンド向けに設定した `Authorization` ヘッダー
  (マネージド ID のアクセストークン) が含まれる。
  現行 APIM ではトレース取得に管理プレーン発行のトークンが別途必要なため
  キー保持者が単独で悪用できるわけではないが、検証用途で不要な設定を
  既定で有効にしている。本番では `false` にすること。

- **Function 用ストレージがネットワーク的に開いている**
  (`networkAcls.defaultAction: 'Allow'`、Private Endpoint なし)。
  `allowSharedKeyAccess: false` と `allowBlobPublicAccess: false` により
  匿名・共有キー経路は塞いであるが、Entra ID 認証の攻撃面はインターネット全体に
  開いた状態になる。本番では `defaultAction: 'Deny'` + `bypass: 'AzureServices'` を
  既定にし、必要に応じて Private Endpoint を構成すること。

## 公開に関する注意

このリポジトリは公開されます。特定の顧客名・案件名・社内限りの実測値・他社製品との比較は一切含めません。
数値はすべてこの検証環境で新たに取得したものだけを使っています。
Named values (単価表・allowlist・月次トークン上限) は `infra/modules/apim.bicep` 内で一元管理し、ポリシー XML に金額をハードコードしません。
通知先メールアドレスなどの個人・組織情報はテンプレートに書かず、デプロイ時パラメータで渡します。

### 公開前スキャン

`scripts/scan_secrets.py` で、キー・GUID・接続文字列・トークン・ローカルパスなどの
混入を機械的に確認できます。

```bash
python scripts/scan_secrets.py             # git が追跡しているファイルを検査
python scripts/scan_secrets.py --all       # 作業ディレクトリ全体を検査
python scripts/scan_secrets.py --history   # git の全 ref (過去の版) も検査
```

既定モードは `git ls-files` (インデックスの内容) を見ます。
`git add` していない新規ファイルは対象外なので、**必ず `git add` の後に実行してください**。

`--history` は「作業ディレクトリは直したが、古い版が git のオブジェクトストアに
残っている」状態を検出します。ファイルを書き換えても過去のコミットからは
`git show <ref>:<path>` でそのまま取り出せるため、公開前に一度は実行してください。
ref から到達できない dangling オブジェクト (`commit --amend` やブランチ削除で
孤立したもの) も含めて全 blob を走査します。

「自分のテナント名」のような**その環境でだけ機密になる文字列**は、
このスクリプトに書いてはいけません。検出パターンとして書いた瞬間、
隠したい文字列そのものを公開することになります。
代わりにリポジトリ直下の `.secretscan-patterns` (`.gitignore` 済み) に
1行1正規表現で置くと、起動時に自動で読み込まれます。
