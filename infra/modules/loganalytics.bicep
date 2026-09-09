// =============================================================================
// Log Analytics Workspace (Application Insights のバッキング)
// =============================================================================
@description('リージョン')
param location string

@description('リソース名のランダムサフィックス')
param resourceToken string

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: 'log-llmops-${resourceToken}'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

output workspaceId string = logAnalytics.id
output workspaceName string = logAnalytics.name
// Container Apps 環境がログ送信先として要求する Workspace ID (顧客 ID)
output workspaceCustomerId string = logAnalytics.properties.customerId
