"""Entry point for the scheduled run: ``python -m aiworker.cloud``.

    python -m aiworker.cloud run            # GitHub Actions: Supabase + Gemini + Cloudflare
    python -m aiworker.cloud demo --out D   # offline: memory store, writes previews to D

Configuration comes from the environment (GitHub Secrets in production):

    SUPABASE_URL, SUPABASE_SECRET_KEY, AIWORKER_OWNER_ID
    GEMINI_API_KEY                       (AIWORKER_LLM=gemini, the default)
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN  (AIWORKER_IMAGE=cloudflare, the default)

Output is one line of counts. Actions logs are public on a public
repository, so no titles, prompts, URLs or keys are ever printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..core.errors import ConfigError
from ..generators.llm import build_provider as build_llm
from ..imagegen.providers import build_provider as build_images
from .pipeline import PREVIEWS, StockPipeline
from .store import MemoryStore, RestStore


def _run(_: argparse.Namespace) -> int:
    try:
        store = RestStore.from_env()
        llm = build_llm(os.environ.get("AIWORKER_LLM", "gemini"),
                        os.environ.get("AIWORKER_LLM_MODEL", ""))
        images = build_images(os.environ.get("AIWORKER_IMAGE", "cloudflare"),
                              os.environ.get("AIWORKER_IMAGE_MODEL", ""))
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2
    report = StockPipeline(store, llm, images).run()
    print(report.summary())
    # A wrong or missing key fails the workflow, so GitHub emails the owner;
    # a busy service does not -- the next run retries it.
    return 1 if any(e.startswith("設定エラー") for e in report.errors) else 0


def _demo(args: argparse.Namespace) -> int:
    """Everything but the network: see what a run produces, for free."""
    store = MemoryStore()
    llm = build_llm(os.environ.get("AIWORKER_LLM", "mock"))
    images = build_images(os.environ.get("AIWORKER_IMAGE", "mock"))
    pipe = StockPipeline(store, llm, images)
    biz, _ = pipe.ensure_setup()
    for acc in store.accounts_.values():
        acc["daily_limit"] = args.count
    biz_row = store.businesses[biz["id"]]
    biz_row["settings"] = {"per_run": args.count}
    report = pipe.run()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for item in store.items_.values():
        if item["preview_path"]:
            data, _ = store.files[(PREVIEWS, item["preview_path"])]
            (out / f"{item['id'][:8]}_{item['status']}.jpg").write_bytes(data)
    print(report.summary())
    print(f"プレビュー: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aiworker.cloud")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="scheduled run against Supabase")
    demo = sub.add_parser("demo", help="offline run into a folder")
    demo.add_argument("--out", default="var/demo")
    demo.add_argument("--count", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "demo":
        return _demo(args)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
