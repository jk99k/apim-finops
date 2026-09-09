"""overshoot_scaling.py - 「超過量は予算に比例するのか、絶対値で頭打ちなのか」を実測する

背景:
  quota_overshoot.py では上限 1000 トークンに対して約 61% 超過した。
  しかしこの「%」は上限を極小にしたから大きく見えているだけではないか?

仮説:
  超過量は「同時に残量ありと判定された件数 x 1リクエストの最大消費量」で決まり、
  上限の大きさには依存しない (絶対値で頭打ちになる)。
  だとすれば上限を大きくするほど、超過の「割合」は小さくなるはずである。

検証方法:
  上限を変えて同じ実験を行い、超過量(絶対値)と超過率(%)がどう動くかを見る。
  各条件で「残量をほぼ使い切るまで逐次リクエスト」→「N件同時リクエスト」の順に実行し、
  同時実行分がどれだけ上限を超えて通るかを測る。

使い方:
  uv run --with-requirements scripts/requirements.txt scripts/overshoot_scaling.py \
      --gateway-url https://xxx.azure-api.net --deployment chat-gpt-5.6-sol \
      --subscription-key <quota-demo のキー> --limits 1000 4000
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
ARM_ENDPOINT = "https://management.azure.com"
APIM_API_VERSION = "2025-09-01-preview"


def run_az(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=120,
                         shell=(os.name == "nt"))
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} に失敗しました: {out.stderr.strip()}")
    return out.stdout.strip()


def set_limit(subscription_id: str, rg: str, apim: str, value: int, token: str) -> None:
    """quota-demo の tokens-per-minute を変更する。"""
    name = "llm-demo-tokens-per-minute"
    url = (f"{ARM_ENDPOINT}/subscriptions/{subscription_id}/resourceGroups/{rg}"
           f"/providers/Microsoft.ApiManagement/service/{apim}"
           f"/namedValues/{name}?api-version={APIM_API_VERSION}")
    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"properties": {"displayName": name, "value": str(value), "secret": False}},
        timeout=120,
    )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"上限の変更に失敗しました: {resp.status_code} "
                           f"{resp.content.decode('utf-8-sig', errors='replace')[:400]}")


def send(url: str, key: str, body: dict[str, Any]) -> tuple[int, Optional[int], Optional[int]]:
    """(status_code, usage.total_tokens, x-remaining-tokens) を返す。"""
    try:
        r = requests.post(url, headers={"Ocp-Apim-Subscription-Key": key,
                                        "Content-Type": "application/json"},
                          json=body, timeout=120)
    except requests.RequestException:
        return (-1, None, None)
    total = None
    if r.status_code == 200:
        try:
            total = r.json().get("usage", {}).get("total_tokens")
        except ValueError:
            total = None
    remaining = None
    raw = r.headers.get("x-remaining-tokens")
    if raw is not None:
        try:
            remaining = int(raw)
        except ValueError:
            remaining = None
    return (r.status_code, total, remaining)


def run_scenario(url: str, key: str, body: dict[str, Any], limit: int,
                 concurrency: int, drain_margin: int) -> dict[str, Any]:
    """残量をほぼ使い切ってから同時リクエストを撃ち、超過量を測る。"""
    consumed = 0
    drain_requests = 0

    # 1) 残量が drain_margin を下回るまで逐次で消費する
    while True:
        status, total, remaining = send(url, key, body)
        if status == 200 and total:
            consumed += total
            drain_requests += 1
        elif status in (429, 403):
            # 使い切る前に上限に当たった場合はここで打ち切る
            break
        else:
            break
        if remaining is not None and remaining <= drain_margin:
            break
        if drain_requests > 200:  # 安全弁
            break

    remaining_before_burst = remaining

    # 2) 残量わずかの状態で同時リクエストを撃つ
    burst: list[tuple[int, Optional[int], Optional[int]]] = []
    if remaining_before_burst is not None and remaining_before_burst > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(send, url, key, body) for _ in range(concurrency)]
            burst = [f.result() for f in futures]

    burst_consumed = sum(t for (s, t, _) in burst if s == 200 and t)
    burst_success = sum(1 for (s, _, _) in burst if s == 200)
    burst_throttled = sum(1 for (s, _, _) in burst if s == 429)

    total_consumed = consumed + burst_consumed
    overshoot = total_consumed - limit

    return {
        "limit": limit,
        "concurrency": concurrency,
        "drain_requests": drain_requests,
        "drain_consumed": consumed,
        "remaining_before_burst": remaining_before_burst,
        "burst_success_200": burst_success,
        "burst_throttled_429": burst_throttled,
        "burst_consumed": burst_consumed,
        "total_consumed": total_consumed,
        "overshoot_tokens": overshoot,
        "overshoot_percent": round(overshoot / limit * 100.0, 1) if limit else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="超過量が上限の大きさに依存するかを実測する")
    p.add_argument("--gateway-url", required=True)
    p.add_argument("--api-path", default="openai-quota-demo")
    p.add_argument("--api-version", default="2024-10-21")
    p.add_argument("--deployment", required=True)
    p.add_argument("--subscription-key", required=True)
    p.add_argument("--resource-group", required=True)
    p.add_argument("--apim-name", required=True)
    p.add_argument("--subscription-id")
    p.add_argument("--limits", type=int, nargs="+", default=[1000, 4000],
                   help="試す tokens-per-minute の値")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--max-completion-tokens", type=int, default=300)
    p.add_argument("--drain-margin", type=int, default=400,
                   help="この残量を下回ったら同時リクエストに移る")
    p.add_argument("--reset-wait-sec", type=int, default=75)
    args = p.parse_args()

    url = (f"{args.gateway_url.rstrip('/')}/{args.api_path.strip('/')}"
           f"/deployments/{args.deployment}/chat/completions?api-version={args.api_version}")
    body = {
        "messages": [{"role": "user",
                      "content": "Please write a detailed explanation of what an API gateway does, in about 200 words."}],
        "max_completion_tokens": args.max_completion_tokens,
        "model": args.deployment,
    }

    token = run_az(["az", "account", "get-access-token", "--resource", ARM_ENDPOINT,
                    "--query", "accessToken", "-o", "tsv"])
    subscription_id = args.subscription_id or run_az(
        ["az", "account", "show", "--query", "id", "-o", "tsv"])

    results = []
    for i, limit in enumerate(args.limits):
        print(f"\n=== 上限 {limit} トークン / 同時 {args.concurrency} 件 ===")
        set_limit(subscription_id, args.resource_group, args.apim_name, limit, token)
        print(f"  上限を {limit} に変更。カウンタのリセットを待機 ({args.reset_wait_sec}秒)...")
        time.sleep(args.reset_wait_sec)

        r = run_scenario(url, args.subscription_key, body, limit,
                         args.concurrency, args.drain_margin)
        results.append(r)
        print(f"  逐次で消費: {r['drain_consumed']} トークン ({r['drain_requests']}件)")
        print(f"  同時実行前の残量: {r['remaining_before_burst']}")
        print(f"  同時実行: 成功{r['burst_success_200']}件 / 429が{r['burst_throttled_429']}件 "
              f"→ {r['burst_consumed']} トークン")
        print(f"  合計消費: {r['total_consumed']} / 上限 {limit}")
        print(f"  超過: {r['overshoot_tokens']} トークン ({r['overshoot_percent']}%)")

        if i < len(args.limits) - 1:
            print(f"  次の条件までカウンタのリセットを待機 ({args.reset_wait_sec}秒)...")
            time.sleep(args.reset_wait_sec)

    print("\n--- まとめ ---")
    print(f"{'上限':>8} {'超過(トークン)':>14} {'超過率':>8}")
    for r in results:
        print(f"{r['limit']:>8} {r['overshoot_tokens']:>14} {str(r['overshoot_percent']) + '%':>8}")
    print("\n超過量(絶対値)がほぼ一定で、超過率だけが下がるなら、")
    print("『超過量は上限の大きさに依存せず、同時実行数 x 1リクエストの大きさで決まる』と言える。")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"overshoot-scaling-{ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存しました: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
