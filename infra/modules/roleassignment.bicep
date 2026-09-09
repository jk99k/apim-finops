// =============================================================================
// APIM Managed Identity に Foundry へのアクセス権 (Cognitive Services User) を付与
// =============================================================================
@description('Foundry アカウント名 (同一リソースグループ内)')
param foundryAccountName string

@description('APIM の System-assigned Managed Identity の principalId')
param apimPrincipalId string

// 組み込みロール "Cognitive Services User": a97b65f3-24c7-4388-baec-2e87135dc908
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, apimPrincipalId, cognitiveServicesUserRoleId)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
  }
}
