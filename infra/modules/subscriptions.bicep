// =============================================================================
// APIM サブスクリプション
//   - team-a / team-b / team-c : 本番想定の API に紐づくチーム別サブスクリプション
//   - quota-demo               : クォータ超過検証 (A3/B6) 専用。本番カウンタを汚さないため分離する
// =============================================================================
@description('APIM サービス名')
param apimServiceName string

@description('紐づける API 名')
param apiName string

@description('クォータ超過検証用 API 名')
param quotaDemoApiName string

resource apim 'Microsoft.ApiManagement/service@2025-09-01-preview' existing = {
  name: apimServiceName
}

resource api 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' existing = {
  parent: apim
  name: apiName
}

resource quotaDemoApi 'Microsoft.ApiManagement/service/apis@2025-09-01-preview' existing = {
  parent: apim
  name: quotaDemoApiName
}

var teams = [
  'team-a'
  'team-b'
  'team-c'
]

resource subscriptions 'Microsoft.ApiManagement/service/subscriptions@2025-09-01-preview' = [
  for team in teams: {
    parent: apim
    name: team
    properties: {
      displayName: team
      scope: api.id
      state: 'active'
      allowTracing: true
    }
  }
]

resource quotaDemoSubscription 'Microsoft.ApiManagement/service/subscriptions@2025-09-01-preview' = {
  parent: apim
  name: 'quota-demo'
  properties: {
    displayName: 'quota-demo'
    scope: quotaDemoApi.id
    state: 'active'
    allowTracing: true
  }
}

output subscriptionKeys array = [
  for (team, i) in teams: {
    name: team
    subscriptionId: subscriptions[i].name
  }
]

output quotaDemoSubscriptionId string = quotaDemoSubscription.name
