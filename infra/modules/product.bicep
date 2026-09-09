// =============================================================================
// 開発者ポータル用の製品 (Product) 定義
//
// 製品は「API をまとめて、利用申請の単位にする」ための入れ物。
// 開発者ポータルでは製品単位で API が一覧・購読される。
//
// なぜ必要か:
//   API を作っただけでは開発者ポータルに現れない。
//   製品に紐づけて初めて「使う人が見つけられる」状態になる。
//   これが AI Gateway の3つ目の顔 (ディスカバリ層) にあたる。
//
// 設計:
//   承認を必須にしている (approvalRequired: true)。
//   誰でも自由にキーを発行できると、統制の入口が崩れるため。
//   「使いたい人が申請し、管理者が承認してキーが払い出される」流れを再現する。
// =============================================================================

@description('APIM サービス名')
param apimServiceName string

@description('製品に含める API 名')
param apiName string

resource apim 'Microsoft.ApiManagement/service@2025-09-01-preview' existing = {
  name: apimServiceName
}

resource llmProduct 'Microsoft.ApiManagement/service/products@2025-09-01-preview' = {
  parent: apim
  name: 'llm-access'
  properties: {
    displayName: 'LLM Access'
    description: 'チーム単位で LLM を利用するための製品。利用にはサブスクリプションキーが必要で、発行には承認が必要です。トークンの上限と利用量の計測が自動的に適用されます。'
    terms: 'この製品を通じた利用はチーム単位で計測され、月次のトークン上限が適用されます。'
    subscriptionRequired: true
    approvalRequired: true
    state: 'published'
  }
}

resource llmProductApi 'Microsoft.ApiManagement/service/products/apis@2025-09-01-preview' = {
  parent: llmProduct
  name: apiName
}

// --- 製品の公開範囲 ---
// 製品を作っただけでは administrators にしか見えない。
// 開発者ポータルで「使いたい人が自分で見つける」ためには、
// 明示的にグループへ紐づける必要がある。
//
//   developers : サインイン済みの利用者
//   guests     : 未サインインの訪問者 (製品の存在だけ見える。購読はできない)
//
// guests に見せるのは、社内の人が「そもそも何が使えるのか」を
// アカウントを作る前に把握できるようにするため。
// 実際に使うには結局サインインと承認が必要なので、統制は緩まない。
resource developersGroup 'Microsoft.ApiManagement/service/products/groups@2025-09-01-preview' = {
  parent: llmProduct
  name: 'developers'
}

resource guestsGroup 'Microsoft.ApiManagement/service/products/groups@2025-09-01-preview' = {
  parent: llmProduct
  name: 'guests'
}

output productName string = llmProduct.name
