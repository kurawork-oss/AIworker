"""Entry point for the scheduled run: ``python -m aiworker.cloud``.

    python -m aiworker.cloud run            # GitHub Actions: every business in AIWORKER_BUSINESSES
    python -m aiworker.cloud run --only stock_images
    python -m aiworker.cloud demo --out D   # offline: memory store, writes previews to D

Configuration comes from the environment (GitHub Secrets in production):

    SUPABASE_URL, SUPABASE_SECRET_KEY, AIWORKER_OWNER_ID
    GEMINI_API_KEY                       (AIWORKER_LLM=gemini, the default)
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN  (画像販売; AIWORKER_IMAGE=cloudflare)
    HATENA_ID, HATENA_BLOG_ID, HATENA_API_KEY    (A8 -> はてなブログ; optional: 未接続 until set)
    VOICEVOX_URL, VOICEVOX_SPEAKER               (youtube_shorts; its own workflow, shorts.yml)

Output is one line of counts per business. Actions logs are public on a
public repository, so no titles, prompts, URLs or keys are ever printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..core.errors import ConfigError
from ..generators.llm import LLMProvider
from ..generators.llm import build_provider as build_llm
from ..imagegen.providers import build_provider as build_images
from .articles import AffiliatePipeline, NotePipeline
from .base import BusinessPipeline
from .pipeline import PREVIEWS, StockPipeline
from .shorts import ShortsPipeline
from .store import MemoryStore, RestStore
from .tts import ToneTTS, VoicevoxTTS

DEFAULT_BUSINESSES = "stock_images,note_articles,affiliate"


def _pipeline(code: str, store: RestStore, llm: LLMProvider) -> BusinessPipeline:
    if code == "stock_images":
        # Built here, not up front: a missing image key must not stop note or A8.
        images = build_images(os.environ.get("AIWORKER_IMAGE", "cloudflare"),
                              os.environ.get("AIWORKER_IMAGE_MODEL", ""))
        return StockPipeline(store, llm, images)
    if code == "note_articles":
        return NotePipeline(store, llm)
    if code == "affiliate":
        return AffiliatePipeline(store, llm)
    if code == "youtube_shorts":
        images = build_images(os.environ.get("AIWORKER_IMAGE", "cloudflare"),
                              os.environ.get("AIWORKER_IMAGE_MODEL", ""))
        tts = ToneTTS() if os.environ.get("AIWORKER_TTS") == "tone" else VoicevoxTTS.from_env()
        return ShortsPipeline(store, llm, images, tts)
    raise ConfigError(f"unknown business '{code}'")


def _run(args: argparse.Namespace) -> int:
    try:
        store = RestStore.from_env()
        llm = build_llm(os.environ.get("AIWORKER_LLM", "gemini"),
                        os.environ.get("AIWORKER_LLM_MODEL", ""))
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2
    codes = args.only or os.environ.get("AIWORKER_BUSINESSES", DEFAULT_BUSINESSES)
    broken = False
    for code in [c.strip() for c in codes.split(",") if c.strip()]:
        try:
            report = _pipeline(code, store, llm).run()
        except ConfigError as exc:
            print(f"{code}: 設定エラー: {exc}")
            broken = True
            continue
        print(report.summary())
        broken = broken or report.config_error
    # A wrong or missing key fails the workflow, so GitHub emails the owner;
    # a busy service does not -- the next run retries it.
    return 1 if broken else 0


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
    run = sub.add_parser("run", help="scheduled run against Supabase")
    run.add_argument("--only", default="", help="comma-separated business codes")
    demo = sub.add_parser("demo", help="offline run into a folder")
    demo.add_argument("--out", default="var/demo")
    demo.add_argument("--count", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "demo":
        return _demo(args)
    if args.command is None:
        args.only = ""
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
