// =============================================================================
// API Management (Developer SKU) + Foundry API 定義 (OpenAI Responses API 互換)
// + Application Insights ロガー/診断設定
// =============================================================================
@description('リージョン')
param location string

@description('リソース名のランダムサフィックス')
param resourceToken string

@description('APIM 発行者メールアドレス')
param publisherEmail string

@description('APIM 発行者名')
param publisherName string

@description('Foundry のエンドポイント (https://xxx.cognitiveservices.azure.com/)')
param foundryEndpoint string

@description('Application Insights のリソース ID')
param appInsightsId string

@description('Application Insights の Instrumentation Key')
@secure()
param appInsightsInstrumentationKey string

@description('モデル別の入出力単価表 (USD / 100万トークン)。書式: "model:input:output,..." (APIMポリシー内でのJSON解析がNamed value展開と相性が悪いためCSV形式)。単価は絶対にポリシー XML にハードコードせず、scripts/update_prices.py で Retail Prices API から更新する。ここでの値は初期値。')
param priceTableCsv string = 'chat-gpt-5.6-sol:5.0:30.0,__default__:30.0:120.0'

@description('承認済みモデル名一覧 (カンマ区切り)')
param allowedModelsCsv string = 'chat-gpt-5.6-sol'

@description('月次トークン上限 (llm-token-limit の token-quota に使用)')
param monthlyTokenQuota int = 1000000

@description('クォータ検証用 API の tokens-per-minute (レート制限)。実験に応じて切り替える')
param demoTokensPerMinute int = 1000

@description('クォータ検証用 API の token-quota (Hourly)。実験に応じて切り替える')
param demoTokenQuota int = 1000000

var apimServiceName = 'apim-llmops-${resourceToken}'
var apiName = 'foundry-responses-api'
var quotaDemoApiName = 'foundry-quota-demo-api'
var backendId = 'foundry-backend'
var loggerId = 'appinsights-logger'

resource apim 'Microsoft.ApiManagement/service@2025-09-01-preview' = {
  name: apimServiceName
  location: location
  sku: {
    name: 'Developer'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

// --- Named values: 単価表 / 承認済みモデル一覧 / 月次トークン上限 ---
// 単価はポリシー XML にハードコードせず、ここで一元管理する (フェーズ2)。
resource priceTable 'Microsoft.ApiManagement/service/namedValues@2025-09-01-preview' = {
  parent: apim
  name: 'llm-price-table'
  properties: {
    displayName: 'llm-price-table'
    value: priceTableCsv
    secret: false
  }
}

resource allowedModels 'Microsoft.ApiManagement/service/namedValues@2025-09-01-preview' = {
  parent: apim
  name: 'llm-allowed-models'
  properties: {
    displayName: 'llm-allowed-models'
    value: allowedModelsCsv
    secret: false
  }
}

resource monthlyQuota 'Microsoft.ApiManagement/service/namedValues@2025-09-01-preview' = {
  parent: apim
  name: 'llm-monthly-token-quota'
  properties: {
    displayName: 'llm-monthly-token-quota'
    value: string(monthlyTokenQuota)
    secret: false
  }
}

// クォータ検証専用 API の上限値。ポリシー XML を書き換えずに実験を切り替えられるよう
// Named value 経由にしている (scripts/set_quota_demo_limits.py で更新)。
resource demoTokensPerMinuteNv 'Microsoft.ApiManagement/service/namedValues@2025-09-01-preview' = {
  parent: apim
  name: 'llm-demo-tokens-per-minute'
  properties: {
    displayName: 'llm-demo-tokens-per-minute'
    value: string(demoTokensPerMinute)
    secret: false
  }
}

resource demoTokenQuotaNv 'Microsoft.ApiManagement/service/namedValues@2025-09-01-preview' = {
  parent: apim
  name: 'llm-demo-token-quota'
  properties: {
    displayName: 'llm-demo-token-quota'
    value: string(demoTokenQuota)
    secret: false
  }
}

// --- バックエンド: Foundry (Managed Identity 経由でアクセス) ---
resource backend 'Microsoft.ApiManagement/service/backends@2025-09-01-preview' = {
  parent: apim
  name: backendId
  properties: {
    url: '${foundryEndpoint}openai'
    protocol: 'http'
    circuitBreaker: {
      rules: [
        {
          failureCondition: {
            count: 3
            interval: 'PT1M'
            statusCodeRanges: [
              {
                min: 429
                max: 429
              }
              {
                min: 500
                max: 599
              }
            ]
          }
          name: 'foundryBreakerRule'
          tripDuration: 'PT1M'
        }
      ]
    }
  }
}

// --- OpenAI Responses API 互換 API 定義 ---
// Foundry の /openai/responses (Responses API) と /openai/deployments/{deployment-id}/chat/completions を
// 手動定義する。Bicep からの再現性を優先し、ポータルの Foundry インポートウィザードは使わない。
resource api 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = {
  parent: apim
  name: apiName
  properties: {
    displayName: 'Foundry OpenAI Responses API'
    description: 'Microsoft Foundry のモデルデプロイを OpenAI Responses API 互換で公開する'
    path: 'openai'
    protocols: [
      'https'
    ]
    subscriptionRequired: true
    // サブスクリプションキーの受け口は APIM 既定のままにする。
    // (一度変更すると、テンプレートから省略しても既定値には戻らないため明示する)
    subscriptionKeyParameterNames: {
      header: 'Ocp-Apim-Subscription-Key'
      query: 'subscription-key'
    }
    apiType: 'http'
    format: 'openapi+json'
    value: loadTextContent('../../policies/openai-responses-api.json')
  }
}

// API レベルのポリシー (allowlist / token-quota / metric) は phase2 で apply-policy.ps1 等から設定する。
// ここでは疎通確認用の最小ポリシー (backend 差し替えのみ) を設定する。
// API レベルのポリシー: allowlist / token-quota / emit-token-metric を含む統合ポリシー (フェーズ1+2)
resource apiPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'xml'
    value: loadTextContent('../../policies/main-policy.xml')
  }
  dependsOn: [
    priceTable
    allowedModels
    monthlyQuota
  ]
}

// --- クォータ超過検証 (A3/B6) 専用 API ---
// 本番用 API とカウンタを分離するため、別 path・別ポリシーで公開する。
// token-quota のカウンタは一度消費するとリセットできないため、
// 本番 API 側の上限を書き換えて検証してはいけない。
resource quotaDemoApi 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' = {
  parent: apim
  name: quotaDemoApiName
  properties: {
    displayName: 'Foundry OpenAI API (quota demo)'
    description: 'トークン上限超過の実地検証専用。極小のトークン上限を設定している。'
    path: 'openai-quota-demo'
    protocols: [
      'https'
    ]
    subscriptionRequired: true
    apiType: 'http'
    format: 'openapi+json'
    value: loadTextContent('../../policies/openai-responses-api.json')
  }
}

resource quotaDemoApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2025-09-01-preview' = {
  parent: quotaDemoApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: loadTextContent('../../policies/quota-demo-policy.xml')
  }
  dependsOn: [
    demoTokensPerMinuteNv
    demoTokenQuotaNv
  ]
}

// --- Application Insights ロガー (llm-emit-token-metric の送信先) ---
resource appInsightsLogger 'Microsoft.ApiManagement/service/loggers@2025-09-01-preview' = {
  parent: apim
  name: loggerId
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsightsId
    credentials: {
      instrumentationKey: appInsightsInstrumentationKey
    }
    isBuffered: true
  }
}

// --- API 診断設定: Application Insights へのログ/メトリクス送信を有効化 ---
resource apiDiagnostic 'Microsoft.ApiManagement/service/apis/diagnostics@2025-09-01-preview' = {
  parent: api
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    verbosity: 'information'
    logClientIp: true
    httpCorrelationProtocol: 'W3C'
    metrics: true
  }
}

// クォータ検証用 API にも同じ診断設定を付ける。
// これが無いと、この API を通った利用がメトリクスにもログにも残らない。
// 「計測していないつもりの経路」が課金だけ発生する状態になり、
// 推定コストと実請求が大きく乖離する原因になる (実際に踏んだ)。
resource quotaDemoApiDiagnostic 'Microsoft.ApiManagement/service/apis/diagnostics@2025-09-01-preview' = {
  parent: quotaDemoApi
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    alwaysLog: 'allErrors'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    verbosity: 'information'
    logClientIp: true
    httpCorrelationProtocol: 'W3C'
    metrics: true
  }
}

output apimServiceName string = apim.name
output apimResourceId string = apim.id
output apimPrincipalId string = apim.identity.principalId
output gatewayUrl string = apim.properties.gatewayUrl
output apiName string = api.name
output quotaDemoApiName string = quotaDemoApi.name
output quotaDemoApiPath string = quotaDemoApi.properties.path
output backendId string = backend.name
output loggerName string = appInsightsLogger.name
output priceTableName string = priceTable.name
output allowedModelsName string = allowedModels.name
output monthlyQuotaName string = monthlyQuota.name
