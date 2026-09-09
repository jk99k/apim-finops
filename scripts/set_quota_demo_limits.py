"""set_quota_demo_limits.py - クォータ検証用 API の上限値を切り替える

quota-demo API の llm-token-limit は上限値を Named values 経由で読むため、
ポリシー XML を書き換えずに 2 つの実験を切り替えられる。

  ratelimit モード (検証項目 B6: レート制限の超過量を測る)
    tokens-per-minute を小さく、token-quota を十分大きくする。
    -> 同時実行で tokens-per-minute を超えて消費されることを観測できる。
    -> 超過時は 429 Too Many Requests。

  quota モード (検証項目 A3: クォータ超過で 403 が返ることを見る)
    tokens-per-minute を十分大きく、token-quota を小さくする。
    -> レート制限に邪魔されず、クォータ超過の 403 Forbidden に到達できる。

使い方:
  uv run --with-requirements scripts/requirements.txt scripts/set_quota_demo_limits.py \
      --resource-group rg-apim-llmops --apim-name <APIM名> --mode quota
"""
from __future__ import annotations

import argparse
import os
import subprocess

import requests

ARM_ENDPOINT = "https://management.azure.com"
APIM_API_VERSION = "2025-09-01-preview"

MODES = {
    # tokens-per-minute, token-quota
    "ratelimit": (1000, 1000000),
    "quota": (100000, 500),
}


def run_az(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=120,
                         shell=(os.name == "nt"))
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} に失敗しました: {out.stderr.strip()}")
    return out.stdout.strip()


def set_named_value(subscription_id: str, resource_group: str, apim_name: str,
                    name: str, value: str, token: str) -> None:
    url = (f"{ARM_ENDPOINT}/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
           f"/providers/Microsoft.ApiManagement/service/{apim_name}"
           f"/namedValues/{name}?api-version={APIM_API_VERSION}")
    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"properties": {"displayName": name, "value": value, "secret": False}},
        timeout=120,
    )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"{name} の更新に失敗しました: {resp.status_code} "
                           f"{resp.content.decode('utf-8-sig', errors='replace')[:500]}")
    print(f"  {name} = {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="quota-demo API の上限値を切り替える")
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--apim-name", required=True)
    parser.add_argument("--mode", required=True, choices=sorted(MODES),
                        help="ratelimit: B6用 / quota: A3用")
    parser.add_argument("--subscription-id")
    args = parser.parse_args()

    tpm, quota = MODES[args.mode]
    token = run_az(["az", "account", "get-access-token", "--resource", ARM_ENDPOINT,
                    "--query", "accessToken", "-o", "tsv"])
    subscription_id = args.subscription_id or run_az(
        ["az", "account", "show", "--query", "id", "-o", "tsv"])

    print(f"モード '{args.mode}' に切り替えます:")
    set_named_value(subscription_id, args.resource_group, args.apim_name,
                    "llm-demo-tokens-per-minute", str(tpm), token)
    set_named_value(subscription_id, args.resource_group, args.apim_name,
                    "llm-demo-token-quota", str(quota), token)
    print("\n注意: llm-token-limit のカウンタは上限値を変えてもリセットされません。"
          "\n直前の実験で消費した分が残っている場合は、期間が明けるまで待ってください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
