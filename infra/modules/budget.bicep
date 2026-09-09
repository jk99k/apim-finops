// =============================================================================
// Cost Management 予算アラート (4層構成の「答え合わせ」層)
//
// この層の位置づけ:
//   天井 (llm-token-limit)      : 確実だが粗い。トークン数でしか制限できない
//   計測 (emit-token-metric)    : 単価表 x トークン数による「推定」。反映は約2分
//   検知 (ログ検索アラート)      : 推定値に対する閾値超過の通知
//   答え合わせ (この予算アラート) : 唯一の「本物の金額」。ただし反映は最も遅い
//
//   上の3層はいずれも自前の単価表に基づく推定であり、割引契約や課金上の丸めを反映しない。
//   実際に請求される金額と突き合わせられるのは Cost Management だけなので、
//   最終的な答え合わせをこの層で行う。
//
// 注意事項 (公式ドキュメントで確認済みの制約):
//   - startDate は月初でなければならない。過去日は timeGrain の期間内に限る。
//   - threshold は金額ではなく「予算額に対するパーセント」(0-1000)。
//   - 通知は 1 予算につき最大 5 個。
//   - contactEmails はサブスクリプション/リソースグループスコープでは必須
//     (contactGroups を指定する場合も、ここでは通知先を明示するため両方に対応する)。
//   - thresholdType: 'Actual' は実績額、'Forecasted' は予測額に対する判定。
//     LLM の利用は増え方が急なので、実績 (Actual) だけでなく予測 (Forecasted) でも
//     早めに気付けるようにしておく。
//   - 予算は「通知するだけ」であり、支出を止めるものではない。
//     止めるのはあくまで APIM 側の天井 (llm-token-limit)。
// =============================================================================

@description('予算名')
param budgetName string

@description('月次予算額 (USD ではなく課金通貨建て。Azure の予算は請求通貨で評価される)')
param budgetAmount int

@description('予算超過通知の宛先メールアドレス。空の場合は予算リソース自体を作成しない。')
param contactEmails array = []

@description('予算の開始日 (月初、ISO 8601)。既定は現在のデプロイ時刻から当月1日を生成する。')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01T00:00:00Z'

@description('実績額に対する通知しきい値 (予算額に対する%)')
param actualThresholdPercent int = 80

@description('予測額に対する通知しきい値 (予算額に対する%)')
param forecastedThresholdPercent int = 100

var hasContacts = !empty(contactEmails)

resource budget 'Microsoft.Consumption/budgets@2024-08-01' = if (hasContacts) {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      // 実績額が予算の一定割合を超えたとき (事後の確定情報)
      actualGreaterThanThreshold: {
        enabled: true
        operator: 'GreaterThan'
        threshold: actualThresholdPercent
        thresholdType: 'Actual'
        contactEmails: contactEmails
      }
      // 予測額が予算を超える見込みになったとき (早期警告)
      forecastedGreaterThanThreshold: {
        enabled: true
        operator: 'GreaterThan'
        threshold: forecastedThresholdPercent
        thresholdType: 'Forecasted'
        contactEmails: contactEmails
      }
    }
  }
}

output budgetCreated bool = hasContacts
output budgetResourceName string = hasContacts ? budget.name : ''
output budgetStartDateUsed string = budgetStartDate
