"""price_sync - 単価表の同期ロジック

Azure Retail Prices API から単価を取得し、APIM の Named value を更新する。
このモジュールは以下の2箇所から共有される:
  - functions/function_app.py (定期実行)
  - scripts/update_prices.py  (手動実行 / --dry-run での確認)

同じ処理を二重に持たないために切り出している。

設計上の判断:
  - meterName とモデルデプロイ名の対応付けは自動判別できないため、
    meter-mapping.json で人間が明示的に管理する。
    (Retail Prices API の meterName は "5.6 sol ShortCo Inp Std Gl 1M Tokens" のような
     独自表記で、モデル名から機械的に導出できない)
  - 単価を解決できないモデルがあれば更新を中止する。
    黙って 0 円にすると過小計上になり、予算超過に気付けなくなるため。
  - __default__ (最高単価のフォールバック) は自動更新の対象外とし、常に安全側を維持する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests

RETAIL_PRICES_ENDPOINT = "https://prices.azure.com/api/retail/prices"
ARM_ENDPOINT = "https://management.azure.com"
APIM_API_VERSION = "2025-09-01-preview"

MAPPING_PATH = Path(__file__).resolve().parent / "meter-mapping.json"


class PriceSyncError(Exception):
    """単価の解決または Named value の更新に失敗したことを表す。"""


@dataclass
class SyncResult:
    new_value: str
    previous_value: Optional[str]
    changed: bool
    items_fetched: int


def load_mapping(path: Path = MAPPING_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def fetch_retail_prices(service_name: str, region: str) -> list[dict[str, Any]]:
    """Retail Prices API から対象リージョン・サービスの価格を全ページ取得する。

    1 リクエストあたり最大 1,000 レコードのため NextPageLink を辿る必要がある。
    この API は認証不要。
    """
    items: list[dict[str, Any]] = []
    url: Optional[str] = RETAIL_PRICES_ENDPOINT
    params: Optional[dict[str, str]] = {
        "$filter": f"serviceName eq '{service_name}' and armRegionName eq '{region}'"
    }
    while url:
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("Items", []))
        url = data.get("NextPageLink")
        params = None  # NextPageLink はクエリを含んだ完全な URL
    return items


def find_price(items: list[dict[str, Any]], meter_name: str) -> Optional[float]:
    """meterName の完全一致で単価 (USD / 100万トークン) を返す。

    unitOfMeasure が '1K' の場合は 1M 換算に補正する。
    予約分などを拾わないよう、従量課金 (Consumption) のみを対象にする。
    """
    for item in items:
        if item.get("meterName") != meter_name:
            continue
        if item.get("type") != "Consumption":
            continue
        price = item.get("retailPrice")
        if price is None:
            continue
        unit = str(item.get("unitOfMeasure", "")).strip()
        if unit.startswith("1M"):
            return float(price)
        if unit.startswith("1K"):
            return float(price) * 1000.0
        # 想定外の単位は、誤った金額を出さないよう採用しない
        return None
    return None


def build_price_table_csv(mapping: dict[str, Any],
                          items: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Named value に書き込む CSV 文字列と、解決できなかった項目の一覧を返す。

    APIM のポリシー式内で JSON を解析すると Named value 展開時に
    引用符が XML と衝突するため、意図的に CSV 形式にしている。
    書式: "model:usdPerMillionInput:usdPerMillionOutput,..."
    """
    rows: list[str] = []
    errors: list[str] = []
    for entry in mapping["mappings"]:
        model = entry["apimModelName"]
        price_in = find_price(items, entry["meterNameInput"])
        price_out = find_price(items, entry["meterNameOutput"])
        if price_in is None or price_out is None:
            errors.append(
                f"{model}: 単価を解決できませんでした "
                f"(input meter='{entry['meterNameInput']}' -> {price_in}, "
                f"output meter='{entry['meterNameOutput']}' -> {price_out})"
            )
            continue
        rows.append(f"{model}:{price_in}:{price_out}")

    fallback = mapping.get("fallback")
    if fallback:
        rows.append(
            f"{fallback['apimModelName']}:"
            f"{fallback['usdPerMillionInput']}:{fallback['usdPerMillionOutput']}"
        )
    return ",".join(rows), errors


def _named_value_url(subscription_id: str, resource_group: str,
                     apim_name: str, named_value: str) -> str:
    return (
        f"{ARM_ENDPOINT}/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{apim_name}"
        f"/namedValues/{named_value}?api-version={APIM_API_VERSION}"
    )


def get_named_value(subscription_id: str, resource_group: str, apim_name: str,
                    named_value: str, token: str) -> Optional[str]:
    """現在の Named value を取得する。取得できなければ None。

    差分の表示にのみ使うため、取得に失敗しても処理は続行する。
    """
    url = _named_value_url(subscription_id, resource_group, apim_name, named_value)
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if resp.status_code != 200:
        return None
    try:
        body = json.loads(resp.content.decode("utf-8-sig"))
    except ValueError:
        return None
    return body.get("properties", {}).get("value")


def update_named_value(subscription_id: str, resource_group: str, apim_name: str,
                       named_value: str, value: str, token: str) -> None:
    url = _named_value_url(subscription_id, resource_group, apim_name, named_value)
    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"properties": {"displayName": named_value, "value": value, "secret": False}},
        timeout=120,
    )
    if resp.status_code not in (200, 201, 202):
        raise PriceSyncError(
            f"Named value の更新に失敗しました: {resp.status_code} "
            f"{resp.content.decode('utf-8-sig', errors='replace')[:500]}"
        )


def _default_token_provider() -> str:
    """Managed Identity / 開発者資格情報から ARM トークンを取得する。

    Azure 上では Function App の Managed Identity が、
    ローカルでは az CLI のログイン状態が使われる。
    どちらの場合も資格情報をコードや設定に持たない。
    """
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    return credential.get_token(f"{ARM_ENDPOINT}/.default").token


def sync_price_table(subscription_id: str, resource_group: str, apim_name: str,
                     named_value: str = "llm-price-table",
                     token_provider: Optional[Callable[[], str]] = None,
                     mapping_path: Path = MAPPING_PATH) -> SyncResult:
    """単価を取得し、変化があれば Named value を更新する。"""
    mapping = load_mapping(mapping_path)
    items = fetch_retail_prices(mapping["serviceName"], mapping["armRegionName"])
    csv_value, errors = build_price_table_csv(mapping, items)

    if errors:
        raise PriceSyncError(
            "単価を解決できないモデルがあるため更新を中止しました。"
            "meter-mapping.json の meterName を実データに合わせて修正してください: "
            + " / ".join(errors)
        )

    provider = token_provider or _default_token_provider
    token = provider()

    previous = get_named_value(subscription_id, resource_group, apim_name,
                               named_value, token)
    if previous == csv_value:
        return SyncResult(new_value=csv_value, previous_value=previous,
                          changed=False, items_fetched=len(items))

    update_named_value(subscription_id, resource_group, apim_name,
                       named_value, csv_value, token)
    return SyncResult(new_value=csv_value, previous_value=previous,
                      changed=True, items_fetched=len(items))
