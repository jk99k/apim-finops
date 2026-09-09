// =============================================================================
// Microsoft Foundry (Cognitive Services, kind=AIServices) + モデルデプロイ
// =============================================================================
@description('リージョン')
param location string

@description('リソース名のランダムサフィックス')
param resourceToken string

@description('デプロイするモデル名')
param modelName string

@description('デプロイするモデルのバージョン')
param modelVersion string

@description('モデルデプロイの capacity (TPM単位)')
param modelCapacity int

var accountName = 'aif-llmops-${resourceToken}'
var modelDeploymentName = 'chat-${modelName}'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

// モデルデプロイ (GlobalStandard: リージョン制約が緩く、検証コストを抑えられる)
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
  }
}

output accountName string = foundryAccount.name
output accountId string = foundryAccount.id
output endpoint string = foundryAccount.properties.endpoint
output modelDeploymentName string = modelDeployment.name
