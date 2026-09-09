"""apply_policy.py - ポリシー XML を APIM の API に直接適用する (検証時の反映用)

az CLI の `az apim api policy` は XML の一部表現で問題を起こすことがあるため、
ARM REST API に直接 PUT する。本番の構成管理は Bicep 側 (infra/) が正であり、
このスクリプトは検証サイクルを速く回すための補助ツール。

使い方:
  uv run --with-requirements scripts/requirements.txt scripts/apply_policy.py \
      --resource-group rg-apim-llmops --apim-name <APIM名> \
      --api-name foundry-responses-api --policy-file policies/main-policy.xml
"""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import requests

ARM_ENDPOINT = "https://management.azure.com"
APIM_API_VERSION = "2025-09-01-preview"


def run_az(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=120,
                         shell=(os.name == "nt"))
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} に失敗しました: {out.stderr.strip()}")
    return out.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="APIM の API ポリシーを適用する")
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--apim-name", required=True)
    parser.add_argument("--api-name", required=True)
    parser.add_argument("--policy-file", required=True)
    parser.add_argument("--subscription-id")
    args = parser.parse_args()

    xml = Path(args.policy_file).read_text(encoding="utf-8")
    token = run_az(["az", "account", "get-access-token", "--resource", ARM_ENDPOINT,
                    "--query", "accessToken", "-o", "tsv"])
    subscription_id = args.subscription_id or run_az(
        ["az", "account", "show", "--query", "id", "-o", "tsv"])

    url = (f"{ARM_ENDPOINT}/subscriptions/{subscription_id}/resourceGroups/{args.resource_group}"
           f"/providers/Microsoft.ApiManagement/service/{args.apim_name}"
           f"/apis/{args.api_name}/policies/policy?api-version={APIM_API_VERSION}")
    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"properties": {"format": "xml", "value": xml}},
        timeout=120,
    )
    body = resp.content.decode("utf-8-sig", errors="replace")
    if resp.status_code not in (200, 201):
        print(f"適用に失敗しました: {resp.status_code}")
        print(body[:2000])
        return 1
    print(f"ポリシーを適用しました: {args.api_name} <- {args.policy_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
