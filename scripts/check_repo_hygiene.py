#!/usr/bin/env python3
"""Repository hygiene checks. Run locally or in CI:

    python3 scripts/check_repo_hygiene.py

Three things this project can get wrong in a way tests would not catch:

1. **A credential in the history.** The whole system is built around API keys
   for platforms whose accounts it is trying to protect. A key in a commit is
   the highest-severity failure available here, and `git rm` does not undo it.
2. **Operational state committed.** `var/aiworker.db` holds the approval
   history and every draft; `config/config.yaml` and `config/policy/*.yaml`
   hold whatever the operator put there.
3. **Unsafe defaults shipped.** `config.example.yaml` is what every new
   install starts from. If it ever ships with `dry_run: false` or a live
   publisher, a fresh clone starts posting.

Exits non-zero on any finding, and says which file and why.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

#: Paths that must never be tracked. Exact paths, or prefixes ending in "/".
FORBIDDEN_TRACKED = [
    (".env", "実際の秘密情報。.env.example だけを追跡すること"),
    ("var/", "実行時の状態（DB・ログ・下書き）"),
    ("config/config.yaml", "実運用の設定。config.example.yaml だけを追跡すること"),
    # `pip install .` leaves these behind, and they are stale copies of src/ --
    # a reader (or a tool) can easily end up looking at the wrong one.
    ("build/", "ビルド生成物（src/ の古いコピー）"),
    ("dist/", "ビルド生成物"),
]
#: Tracked paths that look forbidden but are the templates we do ship.
ALLOWED_TEMPLATES = {".env.example"}
FORBIDDEN_SUFFIX = [
    (".db", "SQLite の実データ"),
    (".db-wal", "SQLite の実データ"),
    (".db-shm", "SQLite の実データ"),
]

#: Credential shapes. Each is (name, pattern). Kept narrow enough not to fire
#: on documentation that merely names the variable.
SECRET_PATTERNS = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("Slack webhook", re.compile(r"hooks\.slack\.com/services/T[A-Za-z0-9/]{20,}")),
    ("Discord webhook", re.compile(r"discord(app)?\.com/api/webhooks/\d{17,}/[\w\-]{50,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

#: `NAME=value` where the value is present and looks like a real credential.
ASSIGNED_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|WEBHOOK_URL))\s*[=:]\s*"
    r"[\"']?([^\s\"'#]{16,})"
)
#: Values that are obviously placeholders rather than credentials.
PLACEHOLDER = re.compile(
    r"(?i)^(\$\{|<|your|xxx|placeholder|changeme|example|dummy|test|fake|none|null|"
    r"https://example|https://discord\.com/api/webhooks/xxxxx)"
)

SKIP_DIRS = {".git", "var", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                         check=True)
    return [line for line in out.stdout.splitlines() if line]


def check_tracked_paths(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        if path in ALLOWED_TEMPLATES:
            continue
        for prefix, why in FORBIDDEN_TRACKED:
            hit = path == prefix or (prefix.endswith("/") and path.startswith(prefix))
            if hit:
                problems.append(f"{path}: 追跡してはいけません（{why}）")
        for suffix, why in FORBIDDEN_SUFFIX:
            if path.endswith(suffix):
                problems.append(f"{path}: 追跡してはいけません（{why}）")
        if (path.startswith("config/policy/") and path.endswith(".yaml")
                and not path.endswith(".example.yaml")):
            problems.append(f"{path}: 実際の禁止リストは追跡しないこと"
                            f"（*.example.yaml のみ追跡）")
    return problems


def check_secrets(files: list[str]) -> list[str]:
    problems = []
    for path in files:
        full = ROOT / path
        if not full.is_file() or any(part in SKIP_DIRS for part in full.parts):
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to scan
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    problems.append(f"{path}:{lineno}: {name} らしき文字列")
            m = ASSIGNED_SECRET.search(line)
            if m and not PLACEHOLDER.match(m.group(2)):
                # This file documents the *names*; empty assignments are the point.
                problems.append(
                    f"{path}:{lineno}: {m.group(1)} に値が入っています"
                    f"（秘密情報は .env に置き、YAML からは ${{ENV:...}} で参照）"
                )
    return problems


def check_example_config_defaults() -> list[str]:
    """The example config is what every new install starts from."""
    problems = []
    path = ROOT / "config" / "config.example.yaml"
    if not path.exists():
        return [f"{path} が見つかりません"]
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if cfg.get("dry_run") is not True:
        problems.append("config.example.yaml: dry_run は true のまま配布すること")
    if cfg.get("require_human_approval") is False:
        problems.append("config.example.yaml: require_human_approval を false にしないこと")
    for name, platform in (cfg.get("platforms") or {}).items():
        publisher = (platform or {}).get("publisher", "dryrun")
        if publisher not in {"dryrun", "manual"}:
            problems.append(
                f"config.example.yaml: platforms.{name}.publisher が "
                f"'{publisher}'。新規インストールが即座に投稿を始めてしまいます"
            )
    return problems


def main() -> int:
    files = tracked_files()
    findings = (check_tracked_paths(files) + check_secrets(files)
                + check_example_config_defaults())
    if findings:
        print("リポジトリ衛生チェック: 問題あり\n")
        for f in findings:
            print(f"  ✗ {f}")
        print(f"\n{len(findings)}件。修正してからコミットしてください。")
        return 1
    print(f"リポジトリ衛生チェック: 問題なし（{len(files)}ファイルを検査）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
