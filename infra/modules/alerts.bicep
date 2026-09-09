// =============================================================================
// ログ検索アラート (フェーズ5)
//   Application Insights の customMetrics を KQL で評価し、
//   チーム単位の推定コストがしきい値を超えたら通知する。
//
// 設計上の注意:
//   - このアラートは「請求額」ではなく「単価表 × トークン数の推定値」を見る。
//     実請求額は Cost Management 側でしか確定しないため、名前と説明で明示する。
//   - 評価期間はアラート側の windowSize で決まるため、クエリ内に ago() は書かない。
//   - 通知先 (アクショングループ) はメールアドレスをパラメータで受け取り、
//     テンプレートにハードコードしない。
// =============================================================================
@description('リージョン')
param location string

@description('リソース名のランダムサフィックス')
param resourceToken string

@description('Application Insights のリソース ID (アラートのスコープ)')
param appInsightsId string

@description('アラート通知先のメールアドレス。空文字の場合、アクショングループを作らず通知なしでアラートのみ作成する。')
param alertEmailAddress string = ''

@description('チーム単位の推定コストしきい値 (USD)。この値を超えるとアラートが発報する。')
param estimatedCostThresholdUsd int = 1

@description('アラートの評価期間 / 評価頻度 (ISO 8601 duration)')
param evaluationWindow string = 'PT1H'

var createActionGroup = !empty(alertEmailAddress)
var actionGroupName = 'ag-llmops-${resourceToken}'
var costAlertName = 'alert-llmops-estimated-cost-${resourceToken}'
var quotaAlertName = 'alert-llmops-quota-429-${resourceToken}'

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (createActionGroup) {
  name: actionGroupName
  location: 'global'
  properties: {
    groupShortName: 'llmops'
    enabled: true
    emailReceivers: [
      {
        name: 'primary'
        emailAddress: alertEmailAddress
        useCommonAlertSchema: true
      }
    ]
  }
}

// --- 推定コストしきい値アラート ---
// customMetrics の Prompt/Completion Tokens に単価ディメンションを掛けて USD 換算する。
// Total Tokens を含めると Prompt + Completion と二重計上になるため除外している。
resource estimatedCostAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: costAlertName
  location: location
  properties: {
    displayName: 'LLM 推定コストがしきい値を超過 (実請求額ではなく単価表ベースの推定値)'
    description: '単価表 Named value × トークン数で算出した推定コストがチーム単位でしきい値を超えた場合に発報する。実際の請求額は Cost Management で確認すること。'
    severity: 2
    enabled: true
    evaluationFrequency: evaluationWindow
    windowSize: evaluationWindow
    scopes: [
      appInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: '''
customMetrics
| where name in ('Prompt Tokens', 'Completion Tokens')
| extend Team = tostring(customDimensions['Team']),
         UsdIn  = todouble(customDimensions['UsdPerMillionInput']),
         UsdOut = todouble(customDimensions['UsdPerMillionOutput'])
| where isnotempty(Team)
| extend UnitPrice = iff(name == 'Prompt Tokens', UsdIn, UsdOut)
| summarize EstimatedUsd = sum(valueSum * UnitPrice / 1000000.0) by Team
'''
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'EstimatedUsd'
          dimensions: [
            {
              name: 'Team'
              operator: 'Include'
              values: [
                '*'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: estimatedCostThresholdUsd
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: createActionGroup ? {
      actionGroups: [
        actionGroup.id
      ]
    } : {}
  }
}

// --- トークン上限超過 (429) 検知アラート ---
// llm-token-limit が上限超過を検知すると 429 を返す。
// 「上限に当たっている = 統制が効いている」ことの可視化であり、障害通知ではない点に注意。
resource quotaExceededAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: quotaAlertName
  location: location
  properties: {
    displayName: 'LLM トークン上限超過 (429) が発生'
    description: 'llm-token-limit によりリクエストが 429 で拒否された。統制が機能している証跡であり、必要なら上限の見直しを検討する。'
    severity: 3
    enabled: true
    evaluationFrequency: evaluationWindow
    windowSize: evaluationWindow
    scopes: [
      appInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: '''
requests
| where resultCode == '429'
| summarize Count = count()
'''
          timeAggregation: 'Total'
          metricMeasureColumn: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: createActionGroup ? {
      actionGroups: [
        actionGroup.id
      ]
    } : {}
  }
}

output estimatedCostAlertName string = estimatedCostAlert.name
output quotaExceededAlertName string = quotaExceededAlert.name
output actionGroupCreated bool = createActionGroup
