#!/usr/bin/env python3
"""Render every screen of the approval UI against a throwaway database.

    python3 scripts/check_web_ui.py

The unit tests cover the UI's behaviour; this covers the thing they cannot --
that every route still returns a page after a change to routing, templates or
the shell. It is the cheapest possible guard against shipping a 500 on a screen
nobody happened to write a test for.

Writes only to a temp directory.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

ROUTES = ["/", "/flow", "/swipe", "/chat", "/outbox", "/reports", "/settings",
          "/item/1", "/healthz"]


def build_fixture(work: Path) -> Path:
    config = {
        "timezone": "Asia/Tokyo",
        "dry_run": True,
        "state_dir": str(work),
        "db_path": str(work / "check.db"),
        "log_dir": str(work / "logs"),
        "platforms": {
            "x": {"enabled": True, "publisher": "manual", "daily_limit": 5,
                  "weekly_limit": 20, "min_interval_minutes": 60,
                  "active_hours": [0, 24]},
        },
        "generation": {"provider": "mock", "themes": ["UI確認用テーマ", "二つ目のテーマ"]},
        "notify": {"channels": [], "min_level": "critical"},
    }
    path = work / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    def cli(*args: str) -> None:
        subprocess.run([sys.executable, "-m", "aiworker", "-c", str(path), "-q", *args],
                       check=True, capture_output=True, cwd=ROOT,
                       env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                            "AIWORKER_ACTOR": "ui-check"})

    cli("generate", "--channel", "social_post", "--count", "3")
    # One blocked item, so the blocked-item paths render too.
    cli("review", "edit", "1", "--body",
        "この方法なら絶対に稼げます。詳細はこちら https://px.a8.net/x/abc")
    return path


def main() -> int:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("FastAPI が入っていません: pip install -r requirements-web.txt", file=sys.stderr)
        return 2

    from aiworker.webui.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        config = build_fixture(work)
        client = TestClient(create_app(str(config)))

        failures = []
        for route in ROUTES:
            response = client.get(route)
            if response.status_code != 200:
                failures.append(f"{route} -> HTTP {response.status_code}")
                continue
            if route != "/healthz" and "</html>" not in response.text:
                failures.append(f"{route} -> 不完全なHTML")
                continue
            print(f"  ok  {route}  ({len(response.text):,} bytes)")

        if failures:
            print("\n画面の描画に失敗しました:\n", file=sys.stderr)
            for f in failures:
                print(f"  ✗ {f}", file=sys.stderr)
            return 1

    print(f"\n{len(ROUTES)}画面すべて描画できました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
