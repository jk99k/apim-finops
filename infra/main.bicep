// =============================================================================
// APIM LLMOps 検証環境 - サブスクリプションスコープのエントリポイント
// =============================================================================
// このテンプレートは以下を作成する:
//   1. リソースグループ (既定: Japan East)
//   2. Microsoft Foundry (Cognitive Services, kind=AIServices) + モデルデプロイ 1つ
//   3. API Management (Developer SKU, System-assigned Managed Identity)
//   4. APIM Managed Identity への Foundry アクセス権 (Cognitive Services User)
//   5. Foundry モデルを OpenAI Responses API 互換ルートとして APIM に公開
//   6. Application Insights + Log Analytics + APIM 診断設定
//   7. APIM サブスクリプション 3つ (team-a / team-b / team-c)
//
// 利用した API バージョン (az provider show で確認済み):
//   Microsoft.ApiManagement/service        : 2025-09-01-preview (最新)
//   Microsoft.CognitiveServices/accounts    : 2026-07-01 (安定版)
//   Microsoft.Insights/components           : 2020-02-02
//   Microsoft.OperationalInsights/workspaces: 2025-07-01
// =============================================================================
targetScope = 'subscription'

@description('リソースをデプロイするリージョン')
param location string = 'japaneast'

@description('リソースグループ名')
param resourceGroupName string = 'rg-apim-llmops'

@description('環境名 (azd 用)')
param environmentName string = 'llmops'

@description('APIM 発行者情報: メールアドレス')
param publisherEmail string = 'admin@example.com'

@description('APIM 発行者情報: 組織名')
param publisherName string = 'JAZUG LLMOps Demo'

@description('デプロイするモデル名 (小さめのモデルで検証コストを抑える)')
param modelName string = 'gpt-5.6-sol'

@description('デプロイするモデルのバージョン')
param modelVersion string = '2026-07-09'

@description('モデルデプロイの capacity (TPM単位、1000トークン/分単位。検証用に最小構成)')
param modelCapacity int = 10

@description('コストアラートの通知先メールアドレス。空文字ならアクショングループを作らず、アラートルールのみ作成する。')
param alertEmailAddress string = ''

@description('チーム単位の推定コストしきい値 (USD)')
param estimatedCostThresholdUsd int = 1

@description('月次予算額 (Cost Management 予算アラート = 4層構成の「答え合わせ」層)')
param monthlyBudgetAmount int = 50

@description('単価表を自動更新する Function の実行スケジュール (NCRONTAB, UTC)。既定は毎日 UTC 18:00 (JST 翌3:00)。')
param priceSyncSchedule string = '0 0 18 * * *'

// ランダムサフィックス生成のためのシード
var resourceToken = toLower(uniqueString(subscription().id, resourceGroupName, environmentName))

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: {
    'azd-env-name': environmentName
  }
}

module foundry 'modules/foundry.bicep' = {
  scope: rg
  name: 'foundry-deployment'
  params: {
    location: location
    resourceToken: resourceToken
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
  }
}

module loganalytics 'modules/loganalytics.bicep' = {
  scope: rg
  name: 'loganalytics-deployment'
  params: {
    location: location
    resourceToken: resourceToken
  }
}

module appinsights 'modules/appinsights.bicep' = {
  scope: rg
  name: 'appinsights-deployment'
  params: {
    location: location
    resourceToken: resourceToken
    logAnalyticsWorkspaceId: loganalytics.outputs.workspaceId
  }
}

module apim 'modules/apim.bicep' = {
  scope: rg
  name: 'apim-deployment'
  params: {
    location: location
    resourceToken: resourceToken
    publisherEmail: publisherEmail
    publisherName: publisherName
    foundryEndpoint: foundry.outputs.endpoint
    appInsightsId: appinsights.outputs.appInsightsId
    appInsightsInstrumentationKey: appinsights.outputs.instrumentationKey
  }
}

module roleAssignment 'modules/roleassignment.bicep' = {
  scope: rg
  name: 'role-assignment-deployment'
  params: {
    foundryAccountName: foundry.outputs.accountName
    apimPrincipalId: apim.outputs.apimPrincipalId
  }
}

module subscriptions 'modules/subscriptions.bicep' = {
  scope: rg
  name: 'subscriptions-deployment'
  params: {
    apimServiceName: apim.outputs.apimServiceName
    apiName: apim.outputs.apiName
    quotaDemoApiName: apim.outputs.quotaDemoApiName
  }
}

module alerts 'modules/alerts.bicep' = {
  scope: rg
  name: 'alerts-deployment'
  params: {
    location: location
    resourceToken: resourceToken
    appInsightsId: appinsights.outputs.appInsightsId
    alertEmailAddress: alertEmailAddress
    estimatedCostThresholdUsd: estimatedCostThresholdUsd
  }
}

// 答え合わせ層: Cost Management 予算アラート。
// スコープはリソースグループ (この検証環境の実費だけを見るため)。
module budget 'modules/budget.bicep' = {
  scope: rg
  name: 'budget-deployment'
  params: {
    budgetName: 'budget-llmops-${resourceToken}'
    budgetAmount: monthlyBudgetAmount
    contactEmails: empty(alertEmailAddress) ? [] : [alertEmailAddress]
  }
}

// コスト可視化ワークブック。
// sourceId に APIM を指定することで「APIM > 監視 > ブック」に表示され、
// コスト確認のために別リソースを開かせずに済む。
module workbook 'modules/workbook.bicep' = {
  scope: rg
  name: 'workbook-deployment'
  params: {
    location: location
    apimResourceId: apim.outputs.apimResourceId
    appInsightsId: appinsights.outputs.appInsightsId
  }
}

// 単価表の定期同期 Function。
// 単価は改定されるため、手動更新では必ず陳腐化する。
// Managed Identity で APIM の Named value を書き換えるため、資格情報を保存しない。
module pricesync 'modules/pricesync.bicep' = {
  scope: rg
  name: 'pricesync-deployment'
  params: {
    location: location
    resourceToken: resourceToken
    apimServiceName: apim.outputs.apimServiceName
    apimResourceId: apim.outputs.apimResourceId
    appInsightsConnectionString: appinsights.outputs.connectionString
    scheduleExpression: priceSyncSchedule
  }
}

// 開発者ポータル用の製品。API を製品に紐づけないとポータルに現れない。
module product 'modules/product.bicep' = {
  scope: rg
  name: 'product-deployment'
  params: {
    apimServiceName: apim.outputs.apimServiceName
    apiName: apim.outputs.apiName
  }
}

output RESOURCE_GROUP_NAME string = rg.name
output APIM_SERVICE_NAME string = apim.outputs.apimServiceName
output APIM_GATEWAY_URL string = apim.outputs.gatewayUrl
output APIM_API_NAME string = apim.outputs.apiName
output APIM_QUOTA_DEMO_API_PATH string = apim.outputs.quotaDemoApiPath
output APIM_QUOTA_DEMO_SUBSCRIPTION_ID string = subscriptions.outputs.quotaDemoSubscriptionId
output FOUNDRY_ACCOUNT_NAME string = foundry.outputs.accountName
output FOUNDRY_ENDPOINT string = foundry.outputs.endpoint
output MODEL_DEPLOYMENT_NAME string = foundry.outputs.modelDeploymentName
output APP_INSIGHTS_NAME string = appinsights.outputs.appInsightsName
output SUBSCRIPTION_KEYS array = subscriptions.outputs.subscriptionKeys
output ESTIMATED_COST_ALERT_NAME string = alerts.outputs.estimatedCostAlertName
output QUOTA_EXCEEDED_ALERT_NAME string = alerts.outputs.quotaExceededAlertName
output BUDGET_CREATED bool = budget.outputs.budgetCreated
output COST_WORKBOOK_ID string = workbook.outputs.workbookId
output PRICE_SYNC_FUNCTION_APP_NAME string = pricesync.outputs.functionAppName
output PRICE_SYNC_STORAGE_NAME string = pricesync.outputs.storageAccountName
