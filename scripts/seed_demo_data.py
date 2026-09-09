"""seed_demo_data.py - スクリーンショット用のデモデータを投入する

ダッシュボードや Application Insights の画面を撮るとき、データが 1〜2 件だと
グラフが成立しない。チーム別・個人別の差が見える程度の量を流す。

方針:
  - チームごと、利用者ごとに異なる量を流し、ランキングに差が出るようにする
  - 使うトークン量は最小限に抑える (検証コストを増やしすぎない)
  - allowlist 違反や上限超過も混ぜ、「遮断された回数」のパネルにも数字を入れる

使い方:
  uv run --with-requirements scripts/requirements.txt scripts/seed_demo_data.py \
      --gateway-url https://xxx.azure-api.net \
      --key-team-a <キー> --key-team-b <キー> --key-team-c <キー>
"""
from __future__ import annotations

import argparse
import time
from typing import Optional

import requests

# チームごとの利用パターン。
# (利用者名, リクエスト数, 1回あたりの出力トークン上限)
# 実際の組織でも「よく使う人」と「たまに使う人」がいるので、差をつける。
TEAM_PROFILES = {
    "team-a": [("alice", 6, 120), ("bob", 4, 80), ("carol", 2, 40)],
    "team-b": [("dave", 3, 100), ("erin", 2, 60)],
    "team-c": [("frank", 2, 50)],
}

PROMPTS = [
    "Explain what an API gateway does in two sentences.",
    "List three benefits of centralizing LLM access.",
    "What is token-based pricing? Answer briefly.",
    "Summarize the idea of cost allocation in one sentence.",
]


def chat(url: str, key: str, user: Optional[str], prompt: str,
         max_tokens: int, model: str) -> tuple[int, Optional[int]]:
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"}
    if user:
        headers["X-User-Id"] = user
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens,
        "model": model,
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=120)
    except requests.RequestException as e:
        print(f"    例外: {e}")
        return (-1, None)
    tokens = None
    if r.status_code == 200:
        try:
            tokens = r.json().get("usage", {}).get("total_tokens")
        except ValueError:
            pass
    return (r.status_code, tokens)


def main() -> int:
    p = argparse.ArgumentParser(description="デモ用データを投入する")
    p.add_argument("--gateway-url", required=True)
    p.add_argument("--api-path", default="openai")
    p.add_argument("--api-version", default="2024-10-21")
    p.add_argument("--model", default="chat-gpt-5.6-sol")
    p.add_argument("--key-team-a", required=True)
    p.add_argument("--key-team-b", required=True)
    p.add_argument("--key-team-c", required=True)
    p.add_argument("--sleep", type=float, default=0.8,
                   help="リクエスト間の待機秒数 (レート制限を避ける)")
    args = p.parse_args()

    url = (f"{args.gateway_url.rstrip('/')}/{args.api_path}"
           f"/deployments/{args.model}/chat/completions?api-version={args.api_version}")
    keys = {
        "team-a": args.key_team_a,
        "team-b": args.key_team_b,
        "team-c": args.key_team_c,
    }

    total_tokens = 0
    total_requests = 0

    for team, profiles in TEAM_PROFILES.items():
        key = keys[team]
        print(f"\n=== {team} ===")
        for user, count, max_tokens in profiles:
            used = 0
            ok = 0
            for i in range(count):
                status, tokens = chat(url, key, user,
                                      PROMPTS[i % len(PROMPTS)], max_tokens, args.model)
                total_requests += 1
                if status == 200 and tokens:
                    used += tokens
                    ok += 1
                elif status != 200:
                    print(f"    {user}: status={status}")
                time.sleep(args.sleep)
            total_tokens += used
            print(f"  {user:<8} {ok}/{count} 件成功 / {used} トークン")

    # 「名乗らない利用」も混ぜる。ダッシュボード上で unassigned が出ることを示すため。
    print("\n=== 識別ヘッダーなし (unassigned) ===")
    used = 0
    for i in range(3):
        status, tokens = chat(url, keys["team-a"], None,
                              PROMPTS[i % len(PROMPTS)], 60, args.model)
        total_requests += 1
        if status == 200 and tokens:
            used += tokens
        time.sleep(args.sleep)
    total_tokens += used
    print(f"  unassigned  {used} トークン")

    # 許可していないモデルを呼び、遮断のカウントを作る
    print("\n=== allowlist 違反 (403 を発生させる) ===")
    blocked = 0
    for i in range(4):
        status, _ = chat(url, keys["team-b"], "dave",
                         "hello", 20, "gpt-not-approved")
        total_requests += 1
        if status == 403:
            blocked += 1
        time.sleep(args.sleep)
    print(f"  403 が {blocked}/4 件")

    print(f"\n--- 合計 ---")
    print(f"リクエスト数: {total_requests}")
    print(f"消費トークン: {total_tokens}")
    print(f"推定コスト  : 出力単価$30/1M で概算 ${total_tokens * 30 / 1_000_000:.4f} 未満")
    print("\nメトリクスの反映まで約2分かかります。画面を撮る前に待ってください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
