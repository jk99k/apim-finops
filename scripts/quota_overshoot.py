"""
quota_overshoot.py - トークン上限の「超過量」を定量化する検証スクリプト (検証項目 A3 / B6)

何を確かめるのか:
  llm-token-limit の tokens-per-minute は「ハードな上限」ではない。
  出力トークン数はリクエスト時点では確定しないため、
  上限に近づいた状態で来たリクエストは通してしまい、結果的に上限を超えて消費されうる。
  さらに同時実行では、複数リクエストが同時に「まだ残量がある」と判定されるため
  超過量が拡大する。

  このスクリプトは「どれだけ超えるのか」を実測値として記録する。

前提:
  - クォータ超過検証専用の API / サブスクリプション (quota-demo) を使う。
    本番相当の API のカウンタを消費しないため。
  - tokens-per-minute は分単位でリセットされるので、
    シナリオごとに次の分境界まで待ってから実行する。

使い方:
  uv run --with-requirements scripts/requirements.txt scripts/quota_overshoot.py \
      --gateway-url https://xxx.azure-api.net \
      --subscription-key <quota-demo のキー> \
      --deployment chat-gpt-5.6-sol \
      --tokens-per-minute 200 --concurrency 5
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


@dataclass
class RequestOutcome:
    index: int
    status_code: int
    tokens_consumed: Optional[int]
    remaining_tokens: Optional[int]
    total_tokens_from_body: Optional[int]
    elapsed_sec: float
    error: Optional[str] = None


def build_url(gateway_url: str, api_path: str, deployment: str, api_version: str) -> str:
    return (f"{gateway_url.rstrip('/')}/{api_path.strip('/')}"
            f"/deployments/{deployment}/chat/completions?api-version={api_version}")


def parse_int_header(headers: Any, name: str) -> Optional[int]:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def send_one(index: int, url: str, key: str, body: dict[str, Any]) -> RequestOutcome:
    started = time.time()
    try:
        resp = requests.post(
            url,
            headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"},
            json=body,
            timeout=120,
        )
    except requests.RequestException as e:
        return RequestOutcome(index, -1, None, None, None, round(time.time() - started, 2), str(e))

    total_tokens = None
    if resp.status_code == 200:
        try:
            total_tokens = resp.json().get("usage", {}).get("total_tokens")
        except ValueError:
            total_tokens = None

    return RequestOutcome(
        index=index,
        status_code=resp.status_code,
        tokens_consumed=parse_int_header(resp.headers, "x-tokens-consumed"),
        remaining_tokens=parse_int_header(resp.headers, "x-remaining-tokens"),
        total_tokens_from_body=total_tokens,
        elapsed_sec=round(time.time() - started, 2),
    )


def wait_for_counter_reset(seconds: int = 70) -> None:
    """tokens-per-minute のカウンタをリセットするために待機する。

    APIM の llm-token-limit のカウンタは壁時計の分境界ではなく、
    その counter-key で最初にカウントされた時点からの経過時間で更新される。
    そのため「次の分境界まで」ではなく「直前のリクエストから 60 秒以上」待つ必要がある。
    """
    print(f"  カウンタのリセット待ち ({seconds} 秒)...")
    time.sleep(seconds)


def summarize(scenario: str, tokens_per_minute: int,
              outcomes: list[RequestOutcome]) -> dict[str, Any]:
    succeeded = [o for o in outcomes if o.status_code == 200]
    throttled = [o for o in outcomes if o.status_code == 429]
    consumed_values = [o.total_tokens_from_body for o in succeeded if o.total_tokens_from_body]
    total_consumed = sum(consumed_values)
    overshoot = total_consumed - tokens_per_minute
    overshoot_ratio = (overshoot / tokens_per_minute * 100.0) if tokens_per_minute else 0.0

    return {
        "scenario": scenario,
        "tokens_per_minute_limit": tokens_per_minute,
        "requests_sent": len(outcomes),
        "requests_succeeded_200": len(succeeded),
        "requests_throttled_429": len(throttled),
        "other_status_codes": sorted({o.status_code for o in outcomes
                                      if o.status_code not in (200, 429)}),
        "actual_tokens_consumed": total_consumed,
        "overshoot_tokens": overshoot,
        "overshoot_percent": round(overshoot_ratio, 1),
        "per_request": [asdict(o) for o in outcomes],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def print_summary(summary: dict[str, Any]) -> None:
    print(f"\n--- {summary['scenario']} ---")
    print(f"  上限 (tokens-per-minute)      : {summary['tokens_per_minute_limit']}")
    print(f"  送信リクエスト数              : {summary['requests_sent']}")
    print(f"  成功 (200)                    : {summary['requests_succeeded_200']}")
    print(f"  スロットル (429)              : {summary['requests_throttled_429']}")
    if summary["other_status_codes"]:
        print(f"  その他ステータス              : {summary['other_status_codes']}")
    print(f"  実際に消費されたトークン数    : {summary['actual_tokens_consumed']}")
    print(f"  上限からの超過量              : {summary['overshoot_tokens']} トークン "
          f"({summary['overshoot_percent']}%)")


def main() -> int:
    parser = argparse.ArgumentParser(description="トークン上限の超過量を実測する")
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--api-path", default="openai-quota-demo",
                        help="クォータ検証専用 API の path")
    parser.add_argument("--api-version", default="2024-10-21")
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--subscription-key", required=True,
                        help="quota-demo サブスクリプションのキー")
    parser.add_argument("--tokens-per-minute", type=int, required=True,
                        help="ポリシーに設定した tokens-per-minute の値")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="同時実行シナリオの並列数")
    parser.add_argument("--max-completion-tokens", type=int, default=300,
                        help="1リクエストあたりの出力トークン上限 (超過を再現しやすくするため大きめ)")
    parser.add_argument("--skip-wait", action="store_true",
                        help="カウンタリセット待機をスキップする (連続実行時は結果が歪むので通常は使わない)")
    parser.add_argument("--reset-wait-sec", type=int, default=70,
                        help="シナリオ間でカウンタがリセットされるまで待つ秒数 (tokens-per-minute なら 60 秒超が必要)")
    args = parser.parse_args()

    url = build_url(args.gateway_url, args.api_path, args.deployment, args.api_version)
    body = {
        "messages": [{
            "role": "user",
            "content": "Please write a detailed explanation of what an API gateway does, in about 200 words.",
        }],
        "max_completion_tokens": args.max_completion_tokens,
        "model": args.deployment,
    }

    summaries: list[dict[str, Any]] = []

    # シナリオ1: 逐次実行 (1リクエストずつ)
    # 同時実行がなくても上限を超えることを示す。
    # (出力トークン数が事前に確定しないため、上限直前のリクエストは通ってしまう)
    print("=== シナリオ1: 逐次実行 ===")
    if not args.skip_wait:
        wait_for_counter_reset(args.reset_wait_sec)
    sequential: list[RequestOutcome] = []
    for i in range(1, 4):
        outcome = send_one(i, url, args.subscription_key, body)
        print(f"  req {i}: status={outcome.status_code}, "
              f"x-tokens-consumed={outcome.tokens_consumed}, "
              f"x-remaining-tokens={outcome.remaining_tokens}, "
              f"usage.total_tokens={outcome.total_tokens_from_body}")
        sequential.append(outcome)
        if outcome.status_code == 429:
            break
    s1 = summarize("逐次実行", args.tokens_per_minute, sequential)
    print_summary(s1)
    summaries.append(s1)

    # シナリオ2: 同時実行
    # 複数リクエストが同時に「まだ残量がある」と判定されるため、超過量が拡大する。
    print(f"\n=== シナリオ2: 同時実行 (並列数 {args.concurrency}) ===")
    if not args.skip_wait:
        wait_for_counter_reset(args.reset_wait_sec)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(send_one, i, url, args.subscription_key, body)
                   for i in range(1, args.concurrency + 1)]
        concurrent_outcomes = [f.result() for f in futures]
    concurrent_outcomes.sort(key=lambda o: o.index)
    for o in concurrent_outcomes:
        print(f"  req {o.index}: status={o.status_code}, "
              f"x-tokens-consumed={o.tokens_consumed}, "
              f"usage.total_tokens={o.total_tokens_from_body}")
    s2 = summarize(f"同時実行 (並列数 {args.concurrency})", args.tokens_per_minute, concurrent_outcomes)
    print_summary(s2)
    summaries.append(s2)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"quota-overshoot-{ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存しました: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
