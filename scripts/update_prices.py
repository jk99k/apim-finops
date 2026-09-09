"""update_prices.py - 単価表 Named value の手動更新 / 確認

Azure Retail Prices API から対象モデルの実単価を取得し、
APIM の Named value `llm-price-table` を更新する。

定期実行は functions/ の Timer Trigger が担当する。
このスクリプトは以下の用途で使う:
  - 初回セットアップ時の投入
  - --dry-run による差分確認 (どんな値が入るかを見る)
  - 定期実行が失敗したときの手動リカバリ

同期ロジックは functions/price_sync と共有している (処理を二重に持たない)。

使い方:
  uv run --with-requirements scripts/requirements.txt scripts/update_prices.py \
      --resource-group rg-apim-llmops --apim-name <APIM名> --dry-run
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# functions/price_sync を共有モジュールとして読み込む
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "functions"))

from price_sync import (  # noqa: E402
    ARM_ENDPOINT,
    PriceSyncError,
    build_price_table_csv,
    fetch_retail_prices,
    get_named_value,
    load_mapping,
    sync_price_table,
)


def get_arm_token_via_az_cli() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", ARM_ENDPOINT,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, timeout=120, shell=(os.name == "nt"),
    )
    if out.returncode != 0:
        raise RuntimeError(f"az account get-access-token に失敗しました: {out.stderr.strip()}")
    return out.stdout.strip()


def get_subscription_id() -> str:
    from_env = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if from_env:
        return from_env
    out = subprocess.run(
        ["az", "account", "show", "--query", "id", "-o", "tsv"],
        capture_output=True, text=True, timeout=120, shell=(os.name == "nt"),
    )
    if out.returncode != 0:
        raise RuntimeError(
            "サブスクリプション ID を特定できませんでした。"
            "--subscription-id を指定するか、az login を行ってください: "
            f"{out.stderr.strip()}"
        )
    return out.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retail Prices API から単価表 Named value を更新する")
    parser.add_argument("--resource-group", required=True, help="APIM のリソースグループ名")
    parser.add_argument("--apim-name", required=True, help="APIM サービス名")
    parser.add_argument("--named-value", default="llm-price-table",
                        help="更新対象の Named value 名")
    parser.add_argument("--subscription-id", help="Azure サブスクリプション ID (省略時は az CLI の既定)")
    parser.add_argument("--dry-run", action="store_true",
                        help="更新せず、生成される値と現在値の差分のみ表示する")
    args = parser.parse_args()

    mapping = load_mapping()
    service_name = mapping["serviceName"]
    region = mapping["armRegionName"]

    print(f"Retail Prices API から取得中: serviceName='{service_name}', region='{region}'")

    if args.dry_run:
        # 更新せずに、生成される値と現在の値を並べて見せる
        items = fetch_retail_prices(service_name, region)
        print(f"  取得件数: {len(items)}")
        csv_value, errors = build_price_table_csv(mapping, items)
        for err in errors:
            print(f"[エラー] {err}", file=sys.stderr)
        if errors:
            print("単価を解決できないモデルがあります。"
                  "meter-mapping.json の meterName を実データに合わせて修正してください。",
                  file=sys.stderr)
            return 1

        token = get_arm_token_via_az_cli()
        subscription_id = args.subscription_id or get_subscription_id()
        current = get_named_value(subscription_id, args.resource_group,
                                  args.apim_name, args.named_value, token)
        print(f"\n現在の値: {current}")
        print(f"生成された値: {csv_value}")
        print("\n変更あり" if current != csv_value else "\n変更なし")
        print("--dry-run のため Named value は更新しませんでした。")
        return 0

    try:
        subscription_id = args.subscription_id or get_subscription_id()
        result = sync_price_table(
            subscription_id=subscription_id,
            resource_group=args.resource_group,
            apim_name=args.apim_name,
            named_value=args.named_value,
            token_provider=get_arm_token_via_az_cli,
        )
    except PriceSyncError as e:
        print(f"[エラー] {e}", file=sys.stderr)
        return 1

    print(f"  価格 {result.items_fetched} 件を取得")
    if result.changed:
        print(f"\n旧値: {result.previous_value}")
        print(f"新値: {result.new_value}")
        print(f"\nNamed value '{args.named_value}' を更新しました。")
    else:
        print(f"\n現在値: {result.new_value}")
        print("変更がなかったため更新しませんでした。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
