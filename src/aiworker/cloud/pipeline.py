"""⑤ AI画像販売: the scheduled run that turns a theme into an upload-ready image.

One run, start to finish, with nobody's computer switched on:

    plan a variant -> Gemini writes the prompt and stock metadata
      -> policy screen ............ blocked (never rendered: saves quota)
      -> render (Cloudflare FLUX) . retried next run if the service is busy
      -> upscale to >= 4MP, preview, inspect (blank / duplicate / size)
      -> upload preview + final to private storage
      -> blocked | 確認待ち | auto-approved via approve_item()
                                   -> アップロード待ち (the owner's to-do list)

Rules this module keeps (the database keeps the rest, whatever this code does):

* It never writes ``approved``. Auto-approval is a call to the same
  ``approve_item`` the phone uses, which refuses unless the owner set the
  account to auto, the item is clean, and it is not a sample.
* A person's revision request always comes back to that person, never
  auto-approved.
* Volume is bounded three ways: the account's daily limit (the owner's
  publication budget), a daily generation cap (the free tiers' budget) and
  a backlog cap (no point making more than the owner can upload).
* Logs carry counts, not content: on a public repository, Actions logs are
  public.
"""

from __future__ import annotations

import datetime as dt
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import PolicyConfig
from ..core.errors import ConfigError, TransientError
from ..core.models import ContentItem
from ..generators.diversity import Variant, choose_variants
from ..generators.llm import LLMProvider, LLMRequest
from ..generators.prompts import spec_for
from ..guard import policy as policy_guard
from ..imagegen.postprocess import (
    PostprocessError,
    inspect,
    make_preview,
    upscale_for_stock,
)
from ..imagegen.providers import ImageProvider, ImageRequest
from .store import Row, Store, StoreError

TZ = ZoneInfo("Asia/Tokyo")
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
    "angles": [],            # empty: the generator's defaults
    "tones": [],
    "per_run": 2,            # new candidates per scheduled run
    "daily_generation_cap": 10,
    "max_backlog": 30,       # 確認待ち + アップロード待ち
    "max_image_attempts": 3,
    "retries_per_run": 3,
    "revisions_per_run": 3,
    "image_width": 1024,
    "image_height": 1024,
    "banned_terms": [],
}

REQUIRED_NEGATIVES = ("text", "watermark", "logo", "brand", "real person")
# Adobe Stock's CSV upload: at most 49 keywords (ranked by the first ten), a
# title of at most 70 characters with no commas, a category number 1-21.
MAX_KEYWORDS = 49
MAX_TITLE = 70
CLEANUP_STATUSES = ("published", "rejected", "cancelled", "failed")


@dataclass
class RunReport:
    stopped: str = ""
    created: int = 0
    blocked: int = 0
    to_review: int = 0
    auto_approved: int = 0
    revised: int = 0
    retried: int = 0
    failed: int = 0
    cleaned: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"stopped": self.stopped, "created": self.created, "blocked": self.blocked,
                "to_review": self.to_review, "auto_approved": self.auto_approved,
                "revised": self.revised, "retried": self.retried, "failed": self.failed,
                "cleaned": self.cleaned, "errors": len(self.errors)}

    def summary(self) -> str:
        if self.stopped:
            return f"停止: {self.stopped}"
        return (f"新規 {self.created} / 確認待ち {self.to_review} / "
                f"自動承認 {self.auto_approved} / ブロック {self.blocked} / "
                f"修正 {self.revised} / 再試行 {self.retried} / 失敗 {self.failed} / "
                f"整理 {self.cleaned} / エラー {len(self.errors)}")


def _category(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 21 else None


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.UTC).isoformat()


class StockPipeline:
    def __init__(self, store: Store, llm: LLMProvider, images: ImageProvider, *,
                 rng: random.Random | None = None, now: dt.datetime | None = None,
                 worker_id: str = "github-actions"):
        self.store = store
        self.llm = llm
        self.images = images
        self.rng = rng or random.Random()
        self.now = now or dt.datetime.now(TZ)
        self.worker_id = worker_id
        # Anything made with a stand-in generator is test output, end to end.
        self.sample = bool(getattr(images, "is_sample", False)) or llm.name == "mock"
        self.spec = spec_for("stock_asset")

    # -- setup -------------------------------------------------------------
    def ensure_setup(self) -> tuple[Row, Row]:
        """The business and its Adobe Stock account, created on first run.

        The account starts on approval *auto*: every finished image is looked
        at by the owner anyway when they upload it by hand (Adobe forbids
        automated submission), so a second review step would be the same look
        twice. Anything a check flags still waits for a person.
        """
        biz = self.store.business(BUSINESS_CODE)
        if biz is None:
            biz = self.store.create_business({
                "code": BUSINESS_CODE, "name": "AI画像販売", "stage": "verifying",
                "settings": {}})
        accounts = [a for a in self.store.accounts(biz["id"]) if a["platform"] == "adobe_stock"]
        acc = accounts[0] if accounts else self.store.create_account({
            "business_id": biz["id"], "platform": "adobe_stock",
            "approval_policy": "auto", "publish_method": "manual",
            "terms_status": "conditional",
            "terms_source_url": "https://helpx.adobe.com/stock/contributor/help/generative-ai-content.html",
        })
        return biz, acc

    def settings(self, biz: Row) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in (biz.get("settings") or {}).items() if v not in (None, "")})
        return merged

    # -- the run -----------------------------------------------------------
    def run(self) -> RunReport:
        report = RunReport()
        if self.store.controls().get("global_stop"):
            report.stopped = "全体停止中"
            return report
        self.store.heartbeat(self.worker_id, {"llm": self.llm.name, "image": self.images.name})
        biz, acc = self.ensure_setup()
        if biz.get("stage") == "stopped":
            report.stopped = "事業が停止中"
            return report
        if acc.get("status") != "active":
            report.stopped = "アカウントが停止中"
            return report
        # 要件 受入条件7: terms nobody has checked (or that forbid this use)
        # stop the work, rather than producing things that cannot be used.
        if acc.get("terms_status") in ("unverified", "forbidden"):
            report.stopped = "規約が未確認または不可のアカウント"
            return report
        cfg = self.settings(biz)

        try:
            self._cleanup(biz, report)
            self._retry(biz, acc, cfg, report)
            self._revise(biz, acc, cfg, report)
            for variant in self._plan(biz, acc, cfg):
                self._create(biz, acc, cfg, variant, report)
        except ConfigError as exc:
            # A missing or wrong key will not fix itself on the next item.
            report.errors.append(f"設定エラー: {exc}")

        level = "error" if report.errors else "info"
        self.store.log(level, "pipeline", f"{biz['name']}: {report.summary()}",
                       {**report.as_dict(), "sample": self.sample,
                        "first_error": report.errors[0][:300] if report.errors else ""})
        return report

    # -- how many ----------------------------------------------------------
    def _plan(self, biz: Row, acc: Row, cfg: dict[str, Any]) -> list[Variant]:
        midnight = self.now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.store.items(biz["id"], created_since=_iso(midnight), limit=1000)
        made_today = len(today)
        usable_today = sum(1 for i in today
                           if i["status"] not in ("blocked", "failed", "cancelled", "rejected"))
        backlog = len(self.store.items(
            biz["id"], statuses=("pending_review", "awaiting_manual", "needs_revision"), limit=1000))
        room = min(int(cfg["per_run"]),
                   int(acc.get("daily_limit", 1)) - usable_today,
                   int(cfg["daily_generation_cap"]) - made_today,
                   int(cfg["max_backlog"]) - backlog)
        if room <= 0:
            return []

        recent = self.store.items(biz["id"], limit=60)
        combos: Counter[str] = Counter()
        themes: Counter[str] = Counter()
        angles: Counter[str] = Counter()
        for i in recent:
            m = i.get("meta") or {}
            combos[f"{m.get('theme')}|{m.get('angle')}|{m.get('tone')}"] += 1
            themes[m.get("theme", "")] += 1
            angles[m.get("angle", "")] += 1
        return choose_variants(room, used_combo=combos, used_theme=themes, used_angle=angles,
                               themes=list(cfg["themes"]), angles=list(cfg["angles"]) or None,
                               tones=list(cfg["tones"]) or None, rng=self.rng)

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

    def _screen(self, title: str, body: str, meta: dict[str, Any],
                cfg: dict[str, Any]) -> policy_guard.PolicyReport:
        probe = ContentItem(channel="stock_asset", platform="adobe_stock",
                            title=title, body=body, meta=meta)
        return policy_guard.check(probe, PolicyConfig(banned_terms=list(cfg["banned_terms"])))

    # -- new items ---------------------------------------------------------
    def _create(self, biz: Row, acc: Row, cfg: dict[str, Any], variant: Variant,
                report: RunReport) -> None:
        try:
            title, body, meta = self._brief(variant)
        except (TransientError, ValueError) as exc:
            report.errors.append(f"文章生成: {exc}")
            return
        pol = self._screen(title, body, meta, cfg)
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
        asked = self.store.items(biz["id"], statuses=("needs_revision",),
                                 limit=int(cfg["revisions_per_run"]))
        for item in reversed(asked):
            if item.get("kind") != KIND:
                continue
            old = item.get("meta") or {}
            variant = Variant(old.get("theme", ""), old.get("angle", ""), old.get("tone", ""))
            extra = ("\n\n前回の生成プロンプト:\n" + str(old.get("prompt", "")) +
                     "\n\n依頼者からの修正指示（最優先で反映）:\n" + item.get("revision_note", ""))
            try:
                title, body, meta = self._brief(variant, extra)
            except (TransientError, ValueError) as exc:
                report.errors.append(f"修正の文章生成: {exc}")
                continue
            meta["revisions"] = int(old.get("revisions", 0)) + 1
            meta["revision_note"] = item.get("revision_note", "")
            pol = self._screen(title, body, meta, cfg)
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
        wants_person = (acc.get("approval_policy") != "auto" or self.sample
                        or int(meta.get("revisions", 0)) > 0)
        if not wants_person:
            try:
                self.store.approve(item["id"], checked["version"], "自動チェック通過")
                report.auto_approved += 1
                return
            except StoreError as exc:
                report.errors.append(f"自動承認できず確認待ちへ: {exc}")
        self.store.update_item(item["id"], {"status": "pending_review"}, expect_status="checking")
        report.to_review += 1

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
