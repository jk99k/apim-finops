// =============================================================================
// コスト可視化ワークブック
//
// sourceId に APIM のリソース ID を指定することで、
// Azure ポータルの「API Management > 監視 > ブック」の一覧に直接表示される。
// コスト管理のために別のリソースを開かせない (運用の入口を1つにする) のが狙い。
//
// 負債を作らないための方針:
//   - 追加リソースは Microsoft.Insights/workbooks 1つだけ。課金対象の実体を増やさない。
//   - クエリ本体は docs/kql-queries.md と同じものを使い、定義を二重管理しない。
//   - 表示定義は JSON ファイルに外出しし、Bicep には埋め込まない (差分が読める状態を保つ)。
//   - 名前は決定論的に生成する (再デプロイで重複を作らない)。
// =============================================================================

@description('リージョン')
param location string

@description('ワークブックを表示する APIM のリソース ID (監視 > ブック に出す先)')
param apimResourceId string

@description('クエリ対象の Application Insights リソース ID')
param appInsightsId string

@description('ワークブックの表示名')
param workbookDisplayName string = 'LLM 利用料の可視化 (チーム別 / 個人別)'

// 同じスコープで再デプロイしても同じ名前になるよう、決定論的に GUID を生成する
var workbookName = guid(apimResourceId, 'llmops-cost-workbook')

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: workbookName
  location: location
  kind: 'shared'
  properties: {
    displayName: workbookDisplayName
    category: 'workbook'
    sourceId: apimResourceId
    serializedData: replace(
      loadTextContent('../../dashboards/cost-workbook.json'),
      '__APP_INSIGHTS_ID__',
      appInsightsId
    )
  }
}

output workbookId string = workbook.id
output workbookName string = workbook.name
