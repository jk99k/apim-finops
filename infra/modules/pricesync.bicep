// =============================================================================
// 単価表の定期同期 Function (Timer Trigger / Flex Consumption)
//
// 何をするか:
//   Azure Retail Prices API から対象モデルの単価を取得し、
//   APIM の Named value (llm-price-table) を毎日更新する。
//   単価は改定されるため、手動運用では確実に陳腐化する。
//
// なぜ Functions を選んだか:
//   コンテナイメージもレジストリも不要で、Python のコードをそのまま置ける。
//   実行時のみ課金され、常駐しない。
//
// 鍵を持たない構成:
//   - Function App は System-assigned Managed Identity を持ち、
//     APIM の Named value を書き換える権限をロール割り当てで得る。
//   - ストレージへの接続も接続文字列ではなく Managed Identity を使う
//     (AzureWebJobsStorage__accountName 形式)。
//   結果として、アプリ設定にシークレットが 1 つも存在しない。
// =============================================================================

@description('リージョン')
param location string

@description('リソース名のランダムサフィックス')
param resourceToken string

@description('更新対象の APIM サービス名')
param apimServiceName string

@description('更新対象の APIM のリソース ID (ロール割り当てのスコープ)')
param apimResourceId string

@description('Application Insights の接続文字列')
@secure()
param appInsightsConnectionString string

@description('実行スケジュール (NCRONTAB, 6フィールド)。既定は毎日 UTC 18:00 = JST 翌 3:00。')
param scheduleExpression string = '0 0 18 * * *'

@description('更新対象の Named value 名')
param priceNamedValue string = 'llm-price-table'

var functionAppName = 'func-pricesync-${resourceToken}'
var planName = 'plan-pricesync-${resourceToken}'
// ストレージアカウント名は英小文字と数字のみ・24文字以内
var storageAccountName = take('stpricesync${replace(resourceToken, '-', '')}', 24)
var deploymentContainerName = 'app-package'

// API Management Service Contributor: Named value の読み書きに必要
var apimContributorRoleId = '312a565d-c81f-4fd8-895a-4e21e48d571c'
// Storage Blob Data Owner: Flex Consumption のコード配置とランタイム利用に必要
var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'

// --- ストレージ ---
// Functions の動作に必須 (コードの配置先、タイマーの実行状態の保持)。
// 共有キー認証は無効化し、Managed Identity 経由のみを許可する。
resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    // 共有キー認証は無効。アクセスは Managed Identity (Entra ID 認証) のみに限定する。
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    // publicNetworkAccess は指定していない。
    // Flex Consumption のストレージは環境のネットワーク要件に合わせて設定が変わるため、
    // 値を固定せず既定に委ねる。閉域が必要な場合は Private Endpoint を併せて構成すること。
    networkAcls: {
      // Functions ランタイムなどの Azure サービスからの到達を許可する
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storage
  name: 'default'
}

// Flex Consumption はコードパッケージをこのコンテナーに置く
resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

// --- Flex Consumption プラン ---
// 公式ガイダンスに従い Y1 (従来の従量課金) ではなく FC1 を使う。
resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: planName
  location: location
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  kind: 'functionapp'
  properties: {
    reserved: true // Linux
  }
}

resource functionApp 'Microsoft.Web/sites@2024-11-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
    siteConfig: {
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsightsConnectionString
        }
        {
          name: 'PRICE_SYNC_SCHEDULE'
          value: scheduleExpression
        }
        {
          name: 'AZURE_SUBSCRIPTION_ID'
          value: subscription().subscriptionId
        }
        {
          name: 'APIM_RESOURCE_GROUP'
          value: resourceGroup().name
        }
        {
          name: 'APIM_NAME'
          value: apimServiceName
        }
        {
          name: 'PRICE_NAMED_VALUE'
          value: priceNamedValue
        }
      ]
    }
  }
}

// --- ロール割り当て: Function -> ストレージ ---
// Flex Consumption はコードの取得と実行状態の保持にストレージを使う。
// 共有キーを無効にしているため、この割り当てが無いと起動できない。
resource storageBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageBlobDataOwnerRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource apim 'Microsoft.ApiManagement/service@2025-09-01-preview' existing = {
  name: apimServiceName
}

// --- ロール割り当て: Function -> APIM ---
// スコープを APIM リソースに限定し、サブスクリプション全体には広げない。
resource apimContributorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apimResourceId, functionApp.id, apimContributorRoleId)
  scope: apim
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', apimContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output functionAppPrincipalId string = functionApp.identity.principalId
output storageAccountName string = storage.name
