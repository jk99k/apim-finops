"""公開前の機密情報スキャン

このリポジトリは公開されるため、以下が混入していないことを機械的に確認する。
  - APIM のサブスクリプションキー (32桁の16進文字列)
  - Azure のサブスクリプション ID / テナント ID (GUID)
  - ストレージの接続文字列、SAS トークン、Bearer トークン、秘密鍵
  - 実リソース名 (ランダムサフィックス付き)
  - 作業者のローカルパス

## 環境固有の文字列について

「自分のテナント名」「自分のアカウント名」のような、この環境でだけ機密になる文字列は
**このファイルに書かない**。検出パターンとして書いてしまうと、隠したい文字列そのものを
公開することになり本末転倒だから。

代わりに、リポジトリ直下の `.secretscan-patterns` に1行1正規表現で置く。
このファイルは .gitignore 済みで、存在すれば自動的に読み込まれる。

    # .secretscan-patterns の例 (# 始まりはコメント)
    MyTenantName\\d+
    my-resource-suffix

使い方:
    python scripts/scan_secrets.py             # 追跡対象ファイルのみ検査 (既定)
    python scripts/scan_secrets.py --all       # 作業ディレクトリ全体を検査
    python scripts/scan_secrets.py --history   # git の全 ref (過去の版) も検査

注意: 既定モードは `git ls-files` = インデックスの内容を見る。
`git add` していない新規ファイルは対象外なので、**必ず `git add` の後に実行すること**。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_PATTERNS_FILE = ROOT / ".secretscan-patterns"

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".azure"}

# 検出パターン: (名前, 正規表現, 深刻度)
# ここには「どの環境でも機密である形」だけを書く。特定の値は書かない。
PATTERNS = [
    ("APIMキー(32桁hex)", re.compile(r"\b[0-9a-f]{32}\b"), "CRITICAL"),
    ("GUID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "CRITICAL"),
    ("ストレージ接続文字列", re.compile(r"AccountKey\s*=\s*[A-Za-z0-9+/=]{20,}"), "CRITICAL"),
    ("SASトークン", re.compile(r"[?&]sig=[A-Za-z0-9%+/=]{20,}"), "CRITICAL"),
    ("計測キー", re.compile(r"InstrumentationKey\s*=\s*[0-9a-f-]{20,}", re.I), "CRITICAL"),
    ("Bearerトークン(JWT)", re.compile(r"eyJ[A-Za-z0-9_-]{20,}"), "CRITICAL"),
    ("秘密鍵", re.compile(r"BEGIN [A-Z0-9 ]*PRIVATE KEY"), "CRITICAL"),
    ("SSOトークン", re.compile(r"signin-sso\?token="), "CRITICAL"),
    ("実リソース名", re.compile(r"(apim|aif|appi|log|st|func|plan|ag|alert)-?(llmops|pricesync)-?[a-z0-9]{10,}", re.I), "HIGH"),
    ("ARMリソースID", re.compile(r"/subscriptions/[0-9a-f-]{30,}"), "HIGH"),
    ("メールアドレス", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "HIGH"),
    # バックスラッシュが1個の生パスと、2個にエスケープされた形の両方を拾う。
    # JSON や Python 文字列リテラルの中では必ずエスケープ形になるため。
    ("ローカルパス", re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}[^\\\s\"']+"), "MEDIUM"),
]

# 許可リスト: 誤検知として無視してよいもの
ALLOW = [
    "admin@example.com",           # ドキュメント上のプレースホルダー
    "dev.demo@example.com",        # 同上
    "foo@example.com",
    "bar@example.com",
    "your-name",
    "apim-llmops@",                # azd のテンプレート識別子 (メールではない)
    "223556219+Copilot@users.noreply.github.com",
    "00000000-0000-0000-0000-000000000000",
    # Azure の組み込みロール定義 ID は公開情報
    "a97b65f3-24c7-4388-baec-2e87135dc908",  # Cognitive Services User
    "312a565d-c81f-4fd8-895a-4e21e48d571c",  # API Management Service Contributor
    "b7e6dc6d-f1e8-4753-8033-0f276bb0955b",  # Storage Blob Data Owner
    "7f951dda-4ed3-4680-a7ca-43fe172d538d",  # AcrPull
]


def load_private_patterns() -> list[tuple[str, re.Pattern[str], str]]:
    """環境固有の検出パターンを外部ファイルから読む。無ければ空。"""
    if not PRIVATE_PATTERNS_FILE.exists():
        return []
    out = []
    for i, raw in enumerate(PRIVATE_PATTERNS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append((f"環境固有#{i}", re.compile(line, re.I), "CRITICAL"))
        except re.error as e:
            print(f"警告: {PRIVATE_PATTERNS_FILE.name}:{i} の正規表現が不正です: {e}", file=sys.stderr)
    return out


def target_files(scan_all: bool) -> list[Path]:
    """検査対象。既定では git が追跡しているファイル (= 実際に公開されるもの)。

    注意: git ls-files はインデックスの内容なので、`git add` していない新規ファイルは
    対象外になる。公開前チェックは必ず `git add` の後に実行すること。
    """
    if not scan_all:
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=ROOT, capture_output=True, check=True,
            )
            names = proc.stdout.decode("utf-8").split("\0")
            # ls-files はインデックスの内容なので、削除済みファイルが混ざりうる。
            # 実在するものだけに絞ってから空判定する (絞った結果が空ならフォールバック)。
            paths = sorted(p for p in (ROOT / n for n in names if n) if p.is_file())
            if paths:
                return paths
            print("注意: git の追跡ファイルが0件です。作業ディレクトリ全体を検査します。", file=sys.stderr)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("注意: git を実行できません。作業ディレクトリ全体を検査します。", file=sys.stderr)

    return sorted(
        p for p in ROOT.rglob("*")
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)
    )


def scan_history(patterns) -> int:
    """git のオブジェクトストアに残る過去の版を検査する。

    作業ディレクトリを直しても、古い版は git のオブジェクトストアに残る。
    ref から到達できないもの (commit --amend やブランチ削除で孤立した dangling
    オブジェクト) も残るため、ref を辿るのではなく **全 blob を直接** 走査する。
    """
    try:
        listing = subprocess.run(
            ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("履歴検査: git を実行できないためスキップします", file=sys.stderr)
        return 0

    blobs = [ln.split()[0] for ln in listing.splitlines()
             if len(ln.split()) == 2 and ln.split()[1] == "blob"]
    if not blobs:
        print("履歴検査: オブジェクトがありません")
        return 0

    hits = 0
    for sha in blobs:
        try:
            raw = subprocess.run(
                ["git", "cat-file", "blob", sha],
                cwd=ROOT, capture_output=True, check=True,
            ).stdout
        except subprocess.CalledProcessError:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern, severity in patterns:
                for m in pattern.finditer(line):
                    if is_allowed(m.group(0)):
                        continue
                    print(f"  [{severity}] [{name}] {m.group(0)[:57]}")
                    print(f"      blob {sha[:12]}:{lineno}")
                    print(f"      {line[:120]}")
                    hits += 1
                    break

    print(f"履歴検査: {len(blobs)} 個の blob (到達不能なものを含む) を検査し、{hits} 件を検出")
    return hits


def is_allowed(matched: str) -> bool:
    return any(a.lower() in matched.lower() for a in ALLOW)


def scan(scan_all: bool = False, with_history: bool = False) -> int:
    patterns = PATTERNS + load_private_patterns()
    private_count = len(patterns) - len(PATTERNS)
    if private_count:
        print(f"環境固有パターンを {private_count} 件読み込みました ({PRIVATE_PATTERNS_FILE.name})")
    else:
        print(f"注意: {PRIVATE_PATTERNS_FILE.name} が無いため、汎用パターンのみで検査します")

    files = target_files(scan_all)

    # 検査対象が0件なら「クリーン」ではなく異常。
    # 何も見ていないのに緑を返すのが、この種のツールで最も危険な壊れ方。
    if not files:
        print("異常: 検査対象が0件です。クリーンとは判定しません。", file=sys.stderr)
        return 2

    findings: list[tuple[str, str, int, str, str]] = []
    skipped_binary = 0

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_binary += 1
            continue
        except (PermissionError, OSError):
            continue

        # このスキャナ自身の PATTERNS 定義は検査から外さない。
        # 除外すると「検出器に秘密を書く」事故を自分で見逃すため。
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern, severity in patterns:
                for m in pattern.finditer(line):
                    if is_allowed(m.group(0)):
                        continue
                    findings.append((severity, rel, lineno, name, m.group(0)))

    print(f"検査対象: {len(files)} ファイル (バイナリ等でスキップ: {skipped_binary})")

    history_hits = 0
    if with_history:
        print("\n=== 履歴 (git の全 ref) ===")
        history_hits = scan_history(patterns)

    if not findings and not history_hits:
        print("\nクリーン: 機密情報は検出されませんでした")
        return 0

    if findings:
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
        findings.sort(key=lambda f: (order.get(f[0], 9), f[1], f[2]))

        print(f"\n検出: {len(findings)} 件")
        current = None
        for severity, rel, lineno, name, matched in findings:
            if severity != current:
                print(f"\n--- {severity} ---")
                current = severity
            shown = matched if len(matched) <= 60 else matched[:57] + "..."
            print(f"  {rel}:{lineno}  [{name}]  {shown}")

    return 1


if __name__ == "__main__":
    sys.exit(scan(
        scan_all="--all" in sys.argv,
        with_history="--history" in sys.argv,
    ))
