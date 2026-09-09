"""
verify.py - APIM LLMOps 検証スクリプト (フェーズ3)

コンテキスト文書「6. 検証項目リスト」の A1〜A5, B1〜B8 を自動検証する。

方針:
  - 検証で使うトークン量は最小限にする (コスト削減)
  - レート制限に引っかかったら適切に待機する
  - 失敗した検証項目は「失敗」として明記し、成功したことにしない
  - 未検証/実測できなかった項目は結果に明記する (資料に書かない判断は呼び出し側で行う)

使い方:
  python scripts/verify.py --gateway-url https://xxx.azure-api.net --api-path openai \
      --subscription-key <team-aのキー> --deployment chat-gpt-5.6-sol \
      --app-insights-app-id <AppInsightsのApp ID> --app-insights-api-key <クエリAPIキー>

出力:
  - 標準出力に Markdown 表のサマリ
  - results/verification-<timestamp>.jsonl に各項目の詳細記録
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


@dataclass
class CheckResult:
    check_id: str
    description: str
    expected: str
    actual: str
    passed: Optional[bool]  # None = 未検証
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def status_label(self) -> str:
        if self.passed is None:
            return "未検証"
        return "成功" if self.passed else "失敗"


class Verifier:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.gateway_url = args.gateway_url.rstrip("/")
        self.api_path = args.api_path.strip("/")
        self.deployment = args.deployment
        self.api_version = args.api_version
        self.results: list[CheckResult] = []
        self._aad_token: Optional[str] = None

    # --- ヘルパー ---
    def _chat_url(self) -> str:
        return f"{self.gateway_url}/{self.api_path}/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    def _quota_demo_chat_url(self) -> str:
        path = self.args.quota_demo_api_path.strip("/")
        return f"{self.gateway_url}/{path}/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    def _quota_demo_body(self) -> dict[str, Any]:
        # 上限に早く到達させるため、通常検証より多めの出力トークンを要求する
        return {
            "messages": [{
                "role": "user",
                "content": "Please write a detailed explanation of what an API gateway does, in about 200 words.",
            }],
            "max_completion_tokens": 300,
            "model": self.deployment,
        }

    def _headers(self, subscription_key: str) -> dict[str, str]:
        return {
            "Ocp-Apim-Subscription-Key": subscription_key,
            "Content-Type": "application/json",
        }

    def _minimal_body(self, stream: bool = False, model: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": "1+1=? 数字のみ答えて"}],
            "max_completion_tokens": 5,
            "stream": stream,
        }
        if model:
            body["model"] = model
        return body

    def record(self, result: CheckResult) -> None:
        self.results.append(result)
        print(f"[{result.status_label()}] {result.check_id}: {result.description}")

    # --- A系: 必須検証 ---
    def check_a1_reachability(self) -> None:
        try:
            resp = requests.post(
                self._chat_url(),
                headers=self._headers(self.args.subscription_key),
                json=self._minimal_body(model=self.deployment),
                timeout=30,
            )
            passed = resp.status_code == 200
            self.record(CheckResult(
                check_id="A1",
                description="Foundry を APIM に取り込み、Codex CLI 相当のリクエストから叩ける",
                expected="200 が返る",
                actual=f"{resp.status_code} {resp.text[:200]}",
                passed=passed,
                detail={"status_code": resp.status_code},
            ))
        except requests.RequestException as e:
            self.record(CheckResult(
                check_id="A1", description="Foundry を APIM に取り込み、Codex CLI 相当のリクエストから叩ける",
                expected="200 が返る", actual=f"例外: {e}", passed=False,
            ))

    def check_a2_counter_key_isolation(self) -> None:
        if not self.args.subscription_key_b:
            self.record(CheckResult(
                check_id="A2", description="counter-key=subscription.id がサブスクリプション単位で分離される",
                expected="サブスクリプション単位でカウントが分離される",
                actual="未検証: --subscription-key-b が指定されていない", passed=None,
            ))
            return
        try:
            r1 = requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key),
                                json=self._minimal_body(model=self.deployment), timeout=30)
            r2 = requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key_b),
                                json=self._minimal_body(model=self.deployment), timeout=30)
            remaining_a = r1.headers.get("x-remaining-quota-tokens")
            remaining_b = r2.headers.get("x-remaining-quota-tokens")
            passed = remaining_a is not None and remaining_b is not None and remaining_a != remaining_b
            self.record(CheckResult(
                check_id="A2", description="counter-key=subscription.id がサブスクリプション単位で分離される",
                expected="サブスクリプション単位でカウントが分離される",
                actual=f"team-a remaining={remaining_a}, team-b remaining={remaining_b}",
                passed=passed,
                detail={"remaining_a": remaining_a, "remaining_b": remaining_b},
            ))
        except requests.RequestException as e:
            self.record(CheckResult(check_id="A2", description="counter-key 分離", expected="分離される",
                                     actual=f"例外: {e}", passed=False))

    def check_a3_quota_exceeded(self) -> None:
        """上限超過時に期待どおりのステータスが返ることを検証する。

        レート制限とクォータは別物で、返るステータスも異なる:
          tokens-per-minute (レート制限) 超過 -> 429 Too Many Requests
          token-quota       (クォータ)   超過 -> 403 Forbidden

        quota-demo API の上限は Named values 経由なので、
        scripts/set_quota_demo_limits.py でモードを切り替えてから実行する。
          --mode ratelimit で実行するなら --quota-demo-expected-status 429
          --mode quota     で実行するなら --quota-demo-expected-status 403
        """
        expected_status = self.args.quota_demo_expected_status
        if not self.args.quota_test_subscription_key:
            self.record(CheckResult(
                check_id="A3", description="トークン上限超過時に期待どおりのステータスが返る",
                expected=f"上限到達後は {expected_status}",
                actual="未検証: --quota-test-subscription-key が指定されていない (quota-demo 専用サブスクリプションが必要)",
                passed=None,
            ))
            return
        last_status = None
        statuses = []
        for _ in range(10):
            resp = requests.post(self._quota_demo_chat_url(),
                                 headers=self._headers(self.args.quota_test_subscription_key),
                                 json=self._quota_demo_body(), timeout=120)
            last_status = resp.status_code
            statuses.append(resp.status_code)
            if resp.status_code == expected_status:
                break
            time.sleep(1)
        passed = last_status == expected_status
        self.record(CheckResult(
            check_id="A3", description="トークン上限超過時に期待どおりのステータスが返る",
            expected=f"上限到達後は {expected_status}",
            actual=f"ステータス遷移: {statuses}", passed=passed,
            detail={"statuses": statuses, "expected_status": expected_status},
        ))

    def check_a4_metric_reaches_app_insights(self) -> None:
        if not self._app_insights_available():
            self.record(CheckResult(
                check_id="A4", description="emit-token-metric のカスタムディメンションが Application Insights に届く",
                expected="customDimensions に Team/Model/UsdPerMillion が入る",
                actual="未検証: --app-insights-app-id が指定されていない", passed=None,
            ))
            return
        requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key),
                      json=self._minimal_body(model=self.deployment), timeout=30)
        query = (
            "customMetrics | where timestamp > ago(1h) "
            "| where isnotempty(tostring(customDimensions['Team'])) "
            "| project timestamp, name, valueSum, "
            "Team = tostring(customDimensions['Team']), "
            "Model = tostring(customDimensions['Model']), "
            "UsdIn = tostring(customDimensions['UsdPerMillionInput']), "
            "UsdOut = tostring(customDimensions['UsdPerMillionOutput']) "
            "| order by timestamp desc | take 5"
        )
        found = self._run_app_insights_query(query)
        # Team / Model / 入力単価 / 出力単価 がすべて非空であることまで確認する
        has_all_dimensions = bool(found) and all(
            row[3] and row[4] and row[5] and row[6] for row in found
        )
        self.record(CheckResult(
            check_id="A4", description="emit-token-metric のカスタムディメンションが Application Insights に届く",
            expected="customDimensions に Team/Model/入力単価/出力単価 が入る",
            actual=(f"取得行数={len(found)}, 先頭行={found[0]}" if found else "クエリ結果なし/クエリ失敗"),
            passed=has_all_dimensions if found is not None else None,
            detail={"rows": found[:5] if found else []},
        ))

    def check_a5_kql_cost_conversion(self) -> None:
        if not self._app_insights_available():
            self.record(CheckResult(
                check_id="A5", description="KQL で金額に換算できる",
                expected="チーム別の推定 USD が算出できる",
                actual="未検証: Application Insights 接続情報が未指定", passed=None,
            ))
            return
        # 入力トークンと出力トークンは単価が異なるため、name に応じて単価を選び分ける。
        query = (
            "customMetrics | where timestamp > ago(24h) "
            "| where name in ('Prompt Tokens', 'Completion Tokens') "
            "| extend Team = tostring(customDimensions['Team']), "
            "UsdIn = todouble(customDimensions['UsdPerMillionInput']), "
            "UsdOut = todouble(customDimensions['UsdPerMillionOutput']) "
            "| where isnotempty(Team) "
            "| extend UnitPrice = iff(name == 'Prompt Tokens', UsdIn, UsdOut) "
            "| summarize Tokens = sum(valueSum), "
            "EstimatedUsd = sum(valueSum * UnitPrice / 1000000.0) by Team "
            "| order by EstimatedUsd desc"
        )
        found = self._run_app_insights_query(query)
        self.record(CheckResult(
            check_id="A5", description="KQL で金額に換算できる", expected="チーム別の推定 USD が算出できる",
            actual=(f"チーム別集計={found}" if found else "クエリ結果なし/クエリ失敗"),
            passed=bool(found) if found is not None else None,
            detail={"rows": found or []},
        ))

    def _app_insights_auth_headers(self) -> Optional[dict[str, str]]:
        """API キーがあればそれを、なければ az CLI の Azure AD トークンを使う。"""
        if self.args.app_insights_api_key:
            return {"X-Api-Key": self.args.app_insights_api_key}
        if self._aad_token is None:
            try:
                out = subprocess.run(
                    ["az", "account", "get-access-token",
                     "--resource", "https://api.applicationinsights.io",
                     "--query", "accessToken", "-o", "tsv"],
                    capture_output=True, text=True, timeout=60, shell=(os.name == "nt"),
                )
                if out.returncode != 0:
                    return None
                self._aad_token = out.stdout.strip()
            except (subprocess.SubprocessError, OSError):
                return None
        return {"Authorization": f"Bearer {self._aad_token}"}

    def _app_insights_available(self) -> bool:
        return bool(self.args.app_insights_app_id)

    def _run_app_insights_query(self, query: str) -> Optional[list]:
        headers = self._app_insights_auth_headers()
        if headers is None:
            return None
        try:
            url = f"https://api.applicationinsights.io/v1/apps/{self.args.app_insights_app_id}/query"
            resp = requests.get(url, headers=headers, params={"query": query}, timeout=60)
            if resp.status_code != 200:
                return None
            data = resp.json()
            rows = data.get("tables", [{}])[0].get("rows", [])
            return rows
        except (requests.RequestException, ValueError):
            return None

    # --- B系: 挙動確認 ---
    def check_b1_streaming_usage(self) -> None:
        try:
            resp = requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key),
                                  json=self._minimal_body(stream=True, model=self.deployment), timeout=30, stream=True)
            body_text = ""
            for chunk in resp.iter_content(chunk_size=None):
                body_text += chunk.decode("utf-8", errors="ignore")
            has_usage_in_body = '"usage"' in body_text
            remaining_header = resp.headers.get("x-remaining-quota-tokens")
            self.record(CheckResult(
                check_id="B1", description="ストリーミング時に応答本文から usage が取れるか",
                expected="取れないことの実地確認",
                actual=f"body内usage有無={has_usage_in_body}, remaining-quota-header={remaining_header}",
                passed=(not has_usage_in_body),
                detail={"has_usage_in_body": has_usage_in_body, "remaining_header": remaining_header},
            ))
        except requests.RequestException as e:
            self.record(CheckResult(check_id="B1", description="ストリーミング時のusage取得", expected="取れない",
                                     actual=f"例外: {e}", passed=None))

    def check_b2_tokens_consumed_variable(self) -> None:
        # tokens-consumed-variable-name はレスポンスヘッダーには出ないため、
        # 現状の APIM 診断ログ (App Insights) 側で変数値を確認する必要がある。ここでは未検証として記録。
        self.record(CheckResult(
            check_id="B2", description="tokens-consumed-variable-name でストリーミング時もトークン数が取れるか",
            expected="取れれば計算式を統一できる",
            actual="未検証: ポリシー変数は診断ログ経由でのみ確認可能。手動でのトレース確認が必要", passed=None,
        ))

    def check_b3_named_value_price_table(self) -> None:
        # ポリシーが Named values を正しく参照しているかは、allowlist違反や metric の値から間接的に確認する。
        resp = requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key),
                              json=self._minimal_body(model="not-a-real-model"), timeout=30)
        passed = resp.status_code == 403 and "model_not_approved" in resp.text
        self.record(CheckResult(
            check_id="B3", description="Named values の llm-allowed-models をポリシーから参照できるか (allowlist経由で間接確認)",
            expected="{{llm-price-table}} 等が参照できる",
            actual=f"status={resp.status_code}, body={resp.text[:200]}", passed=passed,
        ))

    def check_b4_unknown_model_fallback(self) -> None:
        """未知モデルが安全側に倒れるかを確認する。

        実装上の重要な帰結:
          allowlist が inbound の最初にあるため、単価表に無いモデルは
          そもそも 403 で弾かれ、__default__ 単価にフォールバックする経路には到達しない。
          つまり「未知モデルが最高単価で計上される」のではなく「そもそも通らない」。
          これはより安全側だが、コンテキスト文書が想定した挙動とは異なるため、
          __default__ は allowlist をすり抜けた場合の保険として残している。
        """
        resp = requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key),
                             json=self._minimal_body(model="totally-unknown-model-xyz"), timeout=30)
        blocked = resp.status_code == 403 and "model_not_approved" in resp.text
        self.record(CheckResult(
            check_id="B4", description="未知モデルが安全側に倒れるか (403 で遮断 / __default__ 最高単価)",
            expected="安全側に倒れる",
            actual=(f"status={resp.status_code}: allowlist が先に遮断するため "
                    f"__default__ フォールバックには到達しない (より安全側)"),
            passed=blocked,
            detail={"status_code": resp.status_code,
                    "note": "__default__ は allowlist をすり抜けた場合の保険として単価表に残している"},
        ))

    def check_b5_retail_prices_lookup(self) -> None:
        # Retail Prices API 上の serviceName は 'Azure OpenAI' ではなく 'Foundry Models'。
        # meterName は "5.6 sol ShortCo Inp Std Gl 1M Tokens" のような独自表記で、
        # モデルデプロイ名から機械的に導出できない (対応表を人手で維持する必要がある)。
        try:
            resp = requests.get(
                "https://prices.azure.com/api/retail/prices",
                params={"$filter": "serviceName eq 'Foundry Models' and armRegionName eq 'japaneast'"},
                timeout=60,
            )
            items = resp.json().get("Items", []) if resp.status_code == 200 else []
            model_family = self.args.price_meter_keyword
            matched = [i for i in items if model_family.lower() in str(i.get("meterName", "")).lower()]
            self.record(CheckResult(
                check_id="B5", description="Retail Prices API から対象モデルの単価が引けるか (meterName との対応)",
                expected="meterName との対応が取れるか",
                actual=(f"1ページ目取得件数={len(items)}, '{model_family}' を含む meter={len(matched)}件, "
                        f"例={[ (m.get('meterName'), m.get('retailPrice')) for m in matched[:3] ]}"),
                passed=len(matched) > 0,
                detail={"matched_sample": [
                    {"meterName": m.get("meterName"), "retailPrice": m.get("retailPrice"),
                     "unitOfMeasure": m.get("unitOfMeasure")} for m in matched[:5]
                ]},
            ))
        except (requests.RequestException, ValueError) as e:
            self.record(CheckResult(check_id="B5", description="Retail Prices API 単価取得", expected="取得できる",
                                     actual=f"例外: {e}", passed=False))

    def check_b6_concurrency_quota_exceed(self) -> None:
        """同時実行でトークン上限をどれだけ超過するかを実測する (発表の核心)。

        より詳細な逐次/同時実行の比較は scripts/quota_overshoot.py で行う。
        ここでは同時実行時の超過量のみを記録する。
        """
        if not self.args.quota_test_subscription_key:
            self.record(CheckResult(
                check_id="B6", description="同時実行でトークン制限を超過するか (発表の核心)",
                expected="超過する。超過量を数値で記録する",
                actual="未検証: --quota-test-subscription-key が指定されていない (quota-demo 専用サブスクリプションが必要)",
                passed=None,
            ))
            return
        import concurrent.futures

        # 直前のチェック (A3) でカウンタを消費しているため、リセットされるまで待つ。
        # llm-token-limit のカウンタは最初にカウントされた時点から 60 秒で更新される。
        print("  (B6) tokens-per-minute カウンタのリセット待ち...")
        time.sleep(self.args.quota_reset_wait_sec)

        n_concurrent = self.args.concurrency
        url = self._quota_demo_chat_url()
        body = self._quota_demo_body()

        def do_request():
            return requests.post(url, headers=self._headers(self.args.quota_test_subscription_key),
                                 json=body, timeout=120)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_concurrent) as ex:
            futures = [ex.submit(do_request) for _ in range(n_concurrent)]
            responses = [f.result() for f in futures]

        success_count = sum(1 for r in responses if r.status_code == 200)
        throttled_count = sum(1 for r in responses if r.status_code == 429)

        consumed = 0
        for r in responses:
            if r.status_code != 200:
                continue
            try:
                consumed += r.json().get("usage", {}).get("total_tokens", 0) or 0
            except ValueError:
                pass

        limit = self.args.quota_demo_tokens_per_minute
        overshoot = consumed - limit
        overshoot_pct = round(overshoot / limit * 100.0, 1) if limit else None
        passed = overshoot > 0

        self.record(CheckResult(
            check_id="B6", description="同時実行でトークン制限を超過するか (発表の核心)",
            expected="超過する。超過量を数値で記録する",
            actual=(f"並列数={n_concurrent}, 成功={success_count}, 429={throttled_count}, "
                    f"上限={limit}トークン, 実消費={consumed}トークン, "
                    f"超過={overshoot}トークン ({overshoot_pct}%)"),
            passed=passed,
            detail={
                "n_concurrent": n_concurrent,
                "success_count": success_count,
                "throttled_count": throttled_count,
                "tokens_per_minute_limit": limit,
                "actual_tokens_consumed": consumed,
                "overshoot_tokens": overshoot,
                "overshoot_percent": overshoot_pct,
                "status_codes": [r.status_code for r in responses],
            },
        ))

    def check_b7_model_allowlist_blocks(self) -> None:
        resp = requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key),
                              json=self._minimal_body(model="gpt-unapproved-model"), timeout=30)
        passed = resp.status_code == 403
        self.record(CheckResult(
            check_id="B7", description="モデル allowlist ポリシーで未承認モデルを 403 にできるか",
            expected="403 が返る", actual=f"status={resp.status_code}", passed=passed,
        ))

    def check_b8_metric_latency(self) -> None:
        """メトリクスが Application Insights のクエリで見えるようになるまでの時間を実測する。

        注意 (実測で判明したハマりどころ):
          customMetrics の timestamp は「集計バケットの開始時刻」であり
          (_MS.AggregationIntervalMs 参照)、リクエストを送った時刻より前になり得る。
          そのため「送信時刻より新しいレコード」を条件にすると永久にマッチしない。
          ここでは件数の増加を検知する方式で反映時間を測る。
        """
        if not self._app_insights_available():
            self.record(CheckResult(
                check_id="B8", description="メトリクス/ログの反映時間",
                expected="数分かかることの実測 (複数回試行の中央値)",
                actual="未検証: Application Insights 接続情報が未指定", passed=None,
            ))
            return

        count_query = ("customMetrics | where timestamp > ago(2h) "
                       "| where isnotempty(tostring(customDimensions['Team'])) "
                       "| summarize Count = count()")

        def current_count() -> Optional[int]:
            rows = self._run_app_insights_query(count_query)
            if not rows:
                return None
            try:
                return int(rows[0][0])
            except (IndexError, TypeError, ValueError):
                return None

        latencies: list[float] = []
        for trial in range(self.args.b8_trials):
            baseline = current_count()
            if baseline is None:
                break
            sent_at = time.time()
            requests.post(self._chat_url(), headers=self._headers(self.args.subscription_key),
                          json=self._minimal_body(model=self.deployment), timeout=30)
            found_at = None
            deadline = sent_at + self.args.b8_timeout_sec
            while time.time() < deadline:
                time.sleep(10)
                now_count = current_count()
                if now_count is not None and now_count > baseline:
                    found_at = time.time()
                    break
            if found_at:
                latency = round(found_at - sent_at, 1)
                latencies.append(latency)
                print(f"  (B8) 試行{trial + 1}: {latency} 秒で反映を確認")
            else:
                print(f"  (B8) 試行{trial + 1}: {self.args.b8_timeout_sec} 秒以内に反映を確認できず")

        median_latency = statistics.median(latencies) if latencies else None
        self.record(CheckResult(
            check_id="B8", description="メトリクス/ログの反映時間",
            expected="数分かかることの実測 (複数回試行の中央値)",
            actual=(f"試行成功={len(latencies)}/{self.args.b8_trials}, 中央値={median_latency}秒, 各試行={latencies}"
                    if latencies else f"{self.args.b8_timeout_sec}秒以内に反映が確認できなかった"),
            passed=median_latency is not None,
            detail={"latencies_sec": latencies, "median_sec": median_latency,
                    "timeout_sec": self.args.b8_timeout_sec},
        ))

    def run_all(self) -> None:
        self.check_a1_reachability()
        self.check_a2_counter_key_isolation()
        self.check_a3_quota_exceeded()
        self.check_a4_metric_reaches_app_insights()
        self.check_a5_kql_cost_conversion()
        self.check_b1_streaming_usage()
        self.check_b2_tokens_consumed_variable()
        self.check_b3_named_value_price_table()
        self.check_b4_unknown_model_fallback()
        self.check_b5_retail_prices_lookup()
        self.check_b6_concurrency_quota_exceed()
        self.check_b7_model_allowlist_blocks()
        self.check_b8_metric_latency()

    def save_jsonl(self) -> Path:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = RESULTS_DIR / f"verification-{ts}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for r in self.results:
                f.write(json.dumps({
                    "check_id": r.check_id,
                    "description": r.description,
                    "expected": r.expected,
                    "actual": r.actual,
                    "status": r.status_label(),
                    "detail": r.detail,
                    "timestamp": r.timestamp,
                }, ensure_ascii=False) + "\n")
        return path

    def print_summary_markdown(self) -> None:
        print("\n## 検証結果サマリ\n")
        print("| # | 検証項目 | 期待結果 | 実測結果 | 判定 |")
        print("|---|---|---|---|---|")
        for r in self.results:
            actual_short = r.actual.replace("\n", " ")[:80]
            print(f"| {r.check_id} | {r.description} | {r.expected} | {actual_short} | {r.status_label()} |")


def main() -> None:
    parser = argparse.ArgumentParser(description="APIM LLMOps 検証スクリプト")
    parser.add_argument("--gateway-url", required=True, help="APIM Gateway URL (例: https://xxx.azure-api.net)")
    parser.add_argument("--api-path", default="openai", help="API の path (Bicep の api.properties.path)")
    parser.add_argument("--api-version", default="2024-10-21", help="Azure OpenAI API version")
    parser.add_argument("--deployment", required=True, help="モデルデプロイ名 (例: chat-gpt-5.6-sol)")
    parser.add_argument("--subscription-key", required=True, help="team-a 等の通常検証用サブスクリプションキー")
    parser.add_argument("--subscription-key-b", help="A2用: 別チームのサブスクリプションキー")
    parser.add_argument("--quota-test-subscription-key", help="A3/B6用: quota-demo API 用のサブスクリプションキー")
    parser.add_argument("--quota-demo-api-path", default="openai-quota-demo",
                        help="A3/B6用: クォータ超過検証専用 API の path")
    parser.add_argument("--quota-demo-tokens-per-minute", type=int, default=1000,
                        help="A3/B6用: quota-demo ポリシーに設定した tokens-per-minute の値")
    parser.add_argument("--quota-reset-wait-sec", type=int, default=70,
                        help="A3/B6 の間でカウンタがリセットされるまで待つ秒数")
    parser.add_argument("--quota-demo-expected-status", type=int, default=429,
                        help="A3用: 上限超過で期待するステータス (ratelimitモード=429 / quotaモード=403)")
    parser.add_argument("--concurrency", type=int, default=10, help="B6の同時リクエスト数")
    parser.add_argument("--app-insights-app-id", help="Application Insights App ID (A4/A5/B8用)")
    parser.add_argument("--app-insights-api-key", help="Application Insights クエリ API キー (省略時は az CLI の Azure AD トークンを使用)")
    parser.add_argument("--b8-trials", type=int, default=3, help="B8の試行回数")
    parser.add_argument("--b8-timeout-sec", type=int, default=420,
                        help="B8で1試行あたり反映を待つ最大秒数")
    parser.add_argument("--price-meter-keyword", default="5.6 sol",
                        help="B5用: Retail Prices API の meterName に含まれるモデル識別キーワード")
    args = parser.parse_args()

    verifier = Verifier(args)
    verifier.run_all()
    path = verifier.save_jsonl()
    verifier.print_summary_markdown()
    print(f"\n結果を保存しました: {path}")


if __name__ == "__main__":
    main()
