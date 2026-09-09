"""単価表の定期同期 Function

Azure Retail Prices API から対象モデルの単価を取得し、
APIM の Named value (llm-price-table) を更新する。

なぜ定期実行が必要か:
  モデルの単価は改定される。手動更新の運用では必ず陳腐化し、
  古い単価で計算した「推定コスト」を正しい数字だと誤認することになる。

設計方針:
  - 価格取得と Named value 更新のロジックは price_sync パッケージに切り出し、
    scripts/update_prices.py と共有している (同じ処理を二重に持たない)。
  - 認証は Managed Identity (DefaultAzureCredential)。
    接続文字列やシークレットをアプリ設定に置かない。
  - 単価が引けなかった場合は Named value を更新せず例外にする。
    黙って古い値のまま進めると過小計上になり、予算超過に気付けなくなるため。
"""
import logging
import os

import azure.functions as func

from price_sync import PriceSyncError, sync_price_table

app = func.FunctionApp()


@app.timer_trigger(
    # NCRONTAB 形式 (秒 分 時 日 月 曜日)。既定は毎日 UTC 18:00 = JST 翌 3:00。
    schedule="0 0 18 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def price_sync(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("実行予定時刻を過ぎてからの起動です (past due)")

    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    resource_group = os.environ["APIM_RESOURCE_GROUP"]
    apim_name = os.environ["APIM_NAME"]
    named_value = os.environ.get("PRICE_NAMED_VALUE", "llm-price-table")

    try:
        result = sync_price_table(
            subscription_id=subscription_id,
            resource_group=resource_group,
            apim_name=apim_name,
            named_value=named_value,
        )
    except PriceSyncError as e:
        # 単価が解決できない状態で古い値を残すのは危険なため、失敗として記録する。
        # Application Insights に例外として上がり、監視対象になる。
        logging.error("単価表の同期に失敗しました: %s", e)
        raise

    if result.changed:
        logging.info(
            "単価表を更新しました。旧値=%s 新値=%s (価格 %d 件を取得)",
            result.previous_value, result.new_value, result.items_fetched,
        )
    else:
        logging.info(
            "単価表に変更はありませんでした。現在値=%s (価格 %d 件を取得)",
            result.new_value, result.items_fetched,
        )
