"""⑤ AI画像販売: the scheduled run that turns a theme into an upload-ready image.

One run, start to finish, with nobody's computer switched on:

    plan a variant -> Gemini writes the prompt and stock metadata
      -> policy screen ............ blocked (never rendered: saves quota)
      -> render (Cloudflare FLUX) . retried next run if the service is busy
      -> upscale to >= 4MP, preview, inspect (blank / duplicate / size)
      -> upload preview + final to private storage
      -> blocked | 確認待ち | auto-approved via approve_item()
                                   -> アップロード待ち (the owner's to-do list)

The rules shared by every business (stops, volume, approval) are in base.py.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from ..core.errors import TransientError
from ..generators.diversity import Variant
from ..generators.llm import LLMProvider, LLMRequest
from ..generators.prompts import spec_for
from ..imagegen.postprocess import (
    PostprocessError,
    inspect,
    make_preview,
    upscale_for_stock,
)
from ..imagegen.providers import ImageProvider, ImageRequest
from .base import TZ, BusinessPipeline, RunReport, iso  # noqa: F401  (re-exported)
from .store import Row, Store

BUSINESS_CODE = "stock_images"
KIND = "stock_image"
PREVIEWS, FINALS = "previews", "finals"

# Subjects that sell steadily on stock sites and carry the least risk for AI
# output: no people (anatomy, likeness), no text (spelling), no brands.
DEFAULT_THEMES = [
    "ビジネス・テクノロジーの抽象背景",
    "日本の四季の背景素材",
    "和柄・和風テクスチャ",
    "食材や料理のフラットレイ（人物なし）",
    "自然の風景（人物・建物の特定なし）",
    "在宅ワークのデスク周り（人物なし）",
    "水彩・紙・布などのテクスチャ",
    "ミニマルな幾何学パターン",
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "themes": DEFAULT_THEMES,
    "per_run": 2,            # new candidates per scheduled run
    "daily_generation_cap": 10,
    "max_backlog": 30,       # 確認待ち + アップロード待ち
    "max_image_attempts": 3,
    "retries_per_run": 3,
    "image_width": 1024,
    "image_height": 1024,
}

REQUIRED_NEGATIVES = ("text", "watermark", "logo", "brand", "real person")
# Adobe Stock's CSV upload: at most 49 keywords (ranked by the first ten), a
# title of at most 70 characters with no commas, a category number 1-21.
MAX_KEYWORDS = 49
MAX_TITLE = 70
CLEANUP_STATUSES = ("published", "rejected", "cancelled", "failed")


def _category(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 21 else None


class StockPipeline(BusinessPipeline):
    """The account starts on approval *auto*: every finished image is looked
    at by the owner anyway when they upload it by hand (Adobe forbids
    automated submission), so a second review step would be the same look
    twice. Anything a check flags still waits for a person."""

    CODE = BUSINESS_CODE
    NAME = "AI画像販売"
    KIND = KIND
    PLATFORM = "adobe_stock"
    ACCOUNT_DEFAULTS = {
        "approval_policy": "auto", "publish_method": "manual", "terms_status": "conditional",
        "terms_source_url": "https://helpx.adobe.com/stock/contributor/help/generative-ai-content.html",
    }
    DEFAULTS = DEFAULT_SETTINGS

    def __init__(self, store: Store, llm: LLMProvider, images: ImageProvider, *,
                 rng: random.Random | None = None, now: dt.datetime | None = None,
                 worker_id: str = "github-actions"):
        super().__init__(store, llm, rng=rng, now=now, worker_id=worker_id)
        self.images = images
        self.spec = spec_for("stock_asset")

    @property
    def sample(self) -> bool:
        return super().sample or bool(getattr(self.images, "is_sample", False))

    def work(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        self._cleanup(biz, report)
        self._retry(biz, acc, cfg, report)
        self._revise(biz, acc, cfg, report)
        for variant in self.plan(biz, cfg, self.room(biz, acc, cfg)):
            self._create(biz, acc, cfg, variant, report)

    # -- writing the brief -------------------------------------------------
    def _brief(self, variant: Variant, extra: str = "") -> tuple[str, str, dict[str, Any]]:
        prompt = self.spec.prompt_template.format(
            theme=variant.theme, angle=variant.angle, tone=variant.tone) + extra
        raw = self.llm.complete(LLMRequest(
            prompt=prompt, system=self.spec.system, schema=self.spec.schema,
            context={"theme": variant.theme, "angle": variant.angle, "tone": variant.tone,
                     "channel": "stock_asset"},
            max_tokens=self.spec.max_tokens, temperature=self.spec.temperature,
            seed=self.rng.randint(0, 2**31 - 1)))
        image_prompt = str(raw.get("prompt") or "").strip()
        if not image_prompt:
            raise ValueError("model output has no image prompt")
        negative = str(raw.get("negative_prompt") or "")
        missing = [t for t in REQUIRED_NEGATIVES if t not in negative.lower()]
        negative = ", ".join(p for p in [negative.strip(" ,"), *missing] if p)

        keywords: list[str] = []
        for k in raw.get("keywords") or []:
            k = str(k).strip().lower()
            if k and k not in keywords:
                keywords.append(k)
        meta = {
            "theme": variant.theme, "angle": variant.angle, "tone": variant.tone,
            "prompt": image_prompt, "negative_prompt": negative,
            "keywords": keywords[:MAX_KEYWORDS],
            "description": str(raw.get("description") or "").strip(),
            "ai_generated": True,
            "category": _category(raw.get("category")),
            "generator": self.llm.name, "image_backend": self.images.name,
            "image_attempts": 0,
        }
        title = " ".join(str(raw.get("title") or "").replace(",", " ").split())
        if len(title) > MAX_TITLE:
            title = title[:MAX_TITLE].rsplit(" ", 1)[0]
        body = str(raw.get("body") or "").strip()
        return title, body, meta

    # -- new items ---------------------------------------------------------
    def _create(self, biz: Row, acc: Row, cfg: dict[str, Any], variant: Variant,
                report: RunReport) -> None:
        try:
            title, body, meta = self._brief(variant)
        except (TransientError, ValueError) as exc:
            report.errors.append(f"文章生成: {exc}")
            return
        pol = self.screen(title, body, meta, cfg, "stock_asset")
        row = {"business_id": biz["id"], "account_id": acc["id"], "kind": KIND,
               "title": title, "body": body, "meta": meta, "is_sample": self.sample,
               "policy_report": pol.as_dict()}
        report.created += 1
        if pol.blocking:
            self.store.insert_item({**row, "status": "blocked", "blocking": True})
            report.blocked += 1
            return
        item = self.store.insert_item({**row, "status": "image_queued"})
        self._render(item, biz, acc, cfg, report)

    # -- leftovers from earlier runs ---------------------------------------
    def _retry(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        queued = self.store.items(biz["id"], statuses=("image_queued",),
                                  limit=int(cfg["retries_per_run"]))
        for item in reversed(queued):  # oldest first
            report.retried += 1
            self._render(item, biz, acc, cfg, report)

    def _revise(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        for item in self.revision_requests(biz, cfg):
            old = item.get("meta") or {}
            variant = Variant(old.get("theme", ""), old.get("angle", ""), old.get("tone", ""))
            try:
                title, body, meta = self._brief(
                    variant, self.revision_extra(item, str(old.get("prompt", ""))))
            except (TransientError, ValueError) as exc:
                report.errors.append(f"修正の文章生成: {exc}")
                continue
            meta["revisions"] = int(old.get("revisions", 0)) + 1
            meta["revision_note"] = item.get("revision_note", "")
            pol = self.screen(title, body, meta, cfg, "stock_asset")
            draft = self.store.update_item(
                item["id"], {"title": title, "body": body, "meta": meta, "status": "draft",
                             "policy_report": pol.as_dict(), "blocking": pol.blocking,
                             "is_sample": self.sample},
                expect_status="needs_revision")
            if draft is None:
                continue  # the owner did something else with it meanwhile
            report.revised += 1
            if pol.blocking:
                self.store.update_item(item["id"], {"status": "blocked"}, expect_status="draft")
                report.blocked += 1
                continue
            queued = self.store.update_item(item["id"], {"status": "image_queued"},
                                            expect_status="draft")
            if queued is not None:
                self._render(queued, biz, acc, cfg, report)

    # -- image, files, verdict ---------------------------------------------
    def _render(self, item: Row, biz: Row, acc: Row, cfg: dict[str, Any],
                report: RunReport) -> None:
        meta = dict(item.get("meta") or {})
        request = ImageRequest(prompt=meta.get("prompt", ""),
                               negative_prompt=meta.get("negative_prompt", ""),
                               width=int(cfg["image_width"]), height=int(cfg["image_height"]),
                               seed=self.rng.randint(0, 2**31 - 1))
        try:
            result = self.images.generate(request)
            failure = "" if result.ok else (result.message or "画像生成に失敗しました")
        except TransientError as exc:
            result, failure = None, str(exc)
        if failure:
            self._image_failed(item, meta, cfg, failure, report)
            return

        try:
            final = upscale_for_stock(result.data)
        except PostprocessError as exc:
            self._image_failed(item, meta, cfg, str(exc), report, permanent=True)
            return
        recent = [i["quality_report"].get("dhash") for i in self.store.items(biz["id"], limit=200)
                  if i["id"] != item["id"] and (i.get("quality_report") or {}).get("dhash")]
        quality = inspect(final, recent_hashes=recent)
        preview = make_preview(final)

        base = f"{self.store.owner_id}/{item['id']}"
        self.store.upload(PREVIEWS, f"{base}/preview.jpg", preview, "image/jpeg")
        self.store.upload(FINALS, f"{base}/final.jpg", final, "image/jpeg")
        checked = self.store.update_item(
            item["id"], {"preview_path": f"{base}/preview.jpg", "final_path": f"{base}/final.jpg",
                         "quality_report": quality.as_dict(), "is_sample": self.sample,
                         "status": "checking"},
            expect_status="image_queued")
        if checked is None:  # cancelled from the phone while we were drawing
            self.store.remove(PREVIEWS, [f"{base}/preview.jpg"])
            self.store.remove(FINALS, [f"{base}/final.jpg"])
            return

        if not quality.ok:
            self.store.update_item(item["id"], {"status": "blocked", "blocking": True},
                                   expect_status="checking")
            report.blocked += 1
            return
        self.verdict(checked, acc, report)

    def _image_failed(self, item: Row, meta: dict[str, Any], cfg: dict[str, Any], why: str,
                      report: RunReport, *, permanent: bool = False) -> None:
        meta["image_attempts"] = int(meta.get("image_attempts", 0)) + 1
        meta["last_image_error"] = why[:300]
        give_up = permanent or meta["image_attempts"] >= int(cfg["max_image_attempts"])
        changes: dict[str, Any] = {"meta": meta}
        if give_up:
            changes["status"] = "failed"
            report.failed += 1
        else:
            report.errors.append(f"画像生成（次回再試行）: {why[:120]}")
        self.store.update_item(item["id"], changes, expect_status="image_queued")

    # -- storage hygiene ---------------------------------------------------
    def _cleanup(self, biz: Row, report: RunReport) -> None:
        """Delete full-size files nobody will upload any more.

        Finals are ~1-2 MB; the free storage tier is 1 GB. Previews stay as
        the record of what was made.
        """
        done = [i for i in self.store.items(biz["id"], statuses=CLEANUP_STATUSES, limit=200)
                if i.get("final_path")]
        if not done:
            return
        self.store.remove(FINALS, [i["final_path"] for i in done])
        for i in done:
            self.store.update_item(i["id"], {"final_path": ""})
            report.cleaned += 1
