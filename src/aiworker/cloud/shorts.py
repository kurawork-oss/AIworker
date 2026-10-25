"""YouTubeショート: a theme in, a vertical 30-50 second video out.

    plan a variant -> Gemini writes the script: 4-6 scenes of
                      (narration, caption, English image prompt)
      -> policy screen ........... blocked (nothing rendered)
      -> per scene: Cloudflare image + VOICEVOX narration
      -> ffmpeg: 1080x1920, captions, slow zoom   (video.py)
      -> checks: length 10-60 s, audio present, under the 50 MB file ceiling
      -> 確認待ち: the owner watches it on the phone
      -> approved -> 投稿待ち: download, post from the YouTube app, 投稿した + URL

Why a person posts it, for now: uploads through the YouTube API from a
project that has not passed Google's audit are forced private, and the audit
takes weeks. Once it passes, the account switches to publish_method 'api'.

Why every video needs a person's approval: since July 2026 YouTube judges
"inauthentic" (template, mass-produced) content per channel, so one bad
batch can cost the whole channel its monetisation. Topics that read as
health, money or legal advice are excluded from the themes for the same
reason, and the script prompt forbids them.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable
from typing import Any

from ..core.errors import TransientError
from ..generators.diversity import Variant
from ..generators.llm import LLMProvider, LLMRequest
from ..generators.prompts import COMMON_RULES
from ..imagegen.postprocess import make_preview
from ..imagegen.providers import ImageProvider, ImageRequest
from .base import FINALS, BusinessPipeline, RunReport
from .store import Row, Store
from .tts import TTS
from .video import Scene, Video, compose

PREVIEWS = "previews"
MAX_BYTES = 50 * 1024 * 1024
MIN_SECONDS, MAX_SECONDS = 10.0, 60.0

SHORTS_THEMES = [
    "知ると得する生活の小ワザ",
    "日本の季節の行事と由来",
    "身近なモノの意外な雑学",
    "AIツールの便利な使い方",
    "仕事が早くなる小さな習慣",
    "旅行・お出かけの準備のコツ",
]
SHORTS_ANGLES = ["意外な事実から入る", "よくある勘違いを正す", "3つのコツに絞る",
                 "ビフォーアフターで見せる", "クイズ形式", "やってはいけない例から"]
SHORTS_TONES = ["テンポよく", "やさしく", "ちょっと驚き"]

SYSTEM = (
    "あなたはYouTubeショート（縦型・30〜45秒）の構成作家です。\n"
    "- 最初の一言で、問いか意外な事実を出して視聴を止める。\n"
    "- 健康・医療・お金（投資・税金・保険）・法律についての助言はしない。\n"
    "- 一般に知られた事実の範囲で書き、数字や統計を作らない。\n"
    "- image_prompt は英語。人物の顔、文字、ロゴ、実在の店や建物を特定できる要素を入れない。\n"
    + COMMON_RULES)
SCHEMA = {
    "title": "string (動画タイトル。全角30字以内。#は付けない)",
    "description": "string (概要欄の本文。2〜3行)",
    "tags": "string[] (5個以内)",
    "scenes": "object[] (4〜6個。各 {narration: 読み上げる文 40字以内, caption: 画面の字幕 26字以内, "
              "image_prompt: 英語の画像生成プロンプト})",
}


class ShortsPipeline(BusinessPipeline):
    CODE = "youtube_shorts"
    NAME = "YouTubeショート"
    KIND = "short_video"
    PLATFORM = "youtube"
    ACCOUNT_DEFAULTS = {"approval_policy": "required", "publish_method": "manual",
                        "terms_status": "conditional",
                        "terms_source_url": "https://support.google.com/youtube/answer/1311392"}
    DEFAULTS = {"themes": SHORTS_THEMES, "angles": SHORTS_ANGLES, "tones": SHORTS_TONES,
                "per_run": 1, "daily_generation_cap": 2, "max_backlog": 5,
                "max_render_attempts": 3, "retries_per_run": 2, "description_link": ""}

    def __init__(self, store: Store, llm: LLMProvider, images: ImageProvider, tts: TTS, *,
                 composer: Callable[[list[Scene], str], Video] = compose,
                 rng: random.Random | None = None, now: dt.datetime | None = None,
                 worker_id: str = "github-actions"):
        super().__init__(store, llm, rng=rng, now=now, worker_id=worker_id)
        self.images = images
        self.tts = tts
        self.composer = composer

    @property
    def sample(self) -> bool:
        return (super().sample or bool(getattr(self.images, "is_sample", False))
                or bool(getattr(self.tts, "is_sample", False)))

    def work(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        self.cleanup_finals(biz, report)
        queued = self.store.items(biz["id"], statuses=("image_queued",),
                                  limit=int(cfg["retries_per_run"]))
        for item in reversed(queued):
            if item.get("kind") == self.KIND:
                report.retried += 1
                self._render(item, acc, cfg, report)
        self._revise(biz, acc, cfg, report)
        for variant in self.plan(biz, cfg, self.room(biz, acc, cfg)):
            self._create(biz, acc, cfg, variant, report)

    # -- script ------------------------------------------------------------
    def _brief(self, variant: Variant, cfg: dict[str, Any],
               extra: str = "") -> tuple[str, str, dict[str, Any]]:
        prompt = (f"テーマ「{variant.theme}」、切り口「{variant.angle}」、語り口「{variant.tone}」で"
                  "ショート動画の台本を作ってください。" + extra)
        raw = self.llm.complete(LLMRequest(
            prompt=prompt, system=SYSTEM, schema=SCHEMA,
            context={"theme": variant.theme, "angle": variant.angle, "tone": variant.tone,
                     "channel": "short_video"},
            max_tokens=3000, temperature=1.0, seed=self.rng.randint(0, 2**31 - 1)))
        scenes = []
        for s in raw.get("scenes") or []:
            if not isinstance(s, dict):
                continue
            narration = str(s.get("narration") or "").strip()[:80]
            prompt_en = str(s.get("image_prompt") or "").strip()
            if narration and prompt_en:
                scenes.append({"narration": narration,
                               "caption": (str(s.get("caption") or "").strip() or narration)[:40],
                               "image_prompt": prompt_en})
        if not 3 <= len(scenes) <= 7:
            raise ValueError(f"script has {len(scenes)} usable scenes (needs 3-7)")
        title = str(raw.get("title") or "").strip()[:60]
        if not title:
            raise ValueError("script has no title")
        link = str(cfg.get("description_link") or "").strip()
        description = "\n\n".join(p for p in [
            str(raw.get("description") or "").strip(),
            f"▼詳しくはこちら\n{link}" if link else "",
            f"※この動画はAIで生成した画像と合成音声（{self.tts.credit}）を使用しています。",
            "#shorts"] if p)
        meta = {"theme": variant.theme, "angle": variant.angle, "tone": variant.tone,
                "scenes": scenes, "description": description,
                "tags": [str(t) for t in raw.get("tags") or []][:5],
                "voice_credit": self.tts.credit, "generator": self.llm.name,
                "image_backend": self.images.name, "render_attempts": 0}
        body = "\n".join(s["narration"] for s in scenes)
        return title, body, meta

    def _screen_all(self, title: str, body: str, meta: dict[str, Any], cfg: dict[str, Any]):
        probe = {**meta, "prompt": " ".join(s["image_prompt"] for s in meta["scenes"])}
        return self.screen(title, body, probe, cfg, "shorts_script")

    def _create(self, biz: Row, acc: Row, cfg: dict[str, Any], variant: Variant,
                report: RunReport) -> None:
        try:
            title, body, meta = self._brief(variant, cfg)
        except (TransientError, ValueError) as exc:
            report.errors.append(f"台本生成: {exc}")
            return
        pol = self._screen_all(title, body, meta, cfg)
        row = {"business_id": biz["id"], "account_id": acc["id"], "kind": self.KIND,
               "title": title, "body": body, "meta": meta, "is_sample": self.sample,
               "policy_report": pol.as_dict()}
        report.created += 1
        if pol.blocking:
            self.store.insert_item({**row, "status": "blocked", "blocking": True})
            report.blocked += 1
            return
        self._render(self.store.insert_item({**row, "status": "image_queued"}), acc, cfg, report)

    def _revise(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        for item in self.revision_requests(biz, cfg):
            old = item.get("meta") or {}
            variant = Variant(old.get("theme", ""), old.get("angle", ""), old.get("tone", ""))
            try:
                title, body, meta = self._brief(variant, cfg,
                                                self.revision_extra(item, item.get("body", "")))
            except (TransientError, ValueError) as exc:
                report.errors.append(f"修正の台本生成: {exc}")
                continue
            meta["revisions"] = int(old.get("revisions", 0)) + 1
            meta["revision_note"] = item.get("revision_note", "")
            pol = self._screen_all(title, body, meta, cfg)
            draft = self.store.update_item(
                item["id"], {"title": title, "body": body, "meta": meta, "status": "draft",
                             "policy_report": pol.as_dict(), "blocking": pol.blocking,
                             "is_sample": self.sample},
                expect_status="needs_revision")
            if draft is None:
                continue
            report.revised += 1
            if pol.blocking:
                self.store.update_item(item["id"], {"status": "blocked"}, expect_status="draft")
                report.blocked += 1
                continue
            queued = self.store.update_item(item["id"], {"status": "image_queued"},
                                            expect_status="draft")
            if queued is not None:
                self._render(queued, acc, cfg, report)

    # -- picture, voice, video ---------------------------------------------
    def _render(self, item: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        meta = dict(item.get("meta") or {})
        scenes: list[Scene] = []
        try:
            for s in meta.get("scenes", []):
                result = self.images.generate(ImageRequest(
                    prompt=s["image_prompt"],
                    negative_prompt="text, watermark, logo, brand, real person",
                    seed=self.rng.randint(0, 2**31 - 1)))
                if not result.ok:
                    raise TransientError(result.message or "画像生成に失敗しました")
                scenes.append(Scene(result.data, self.tts.synth(s["narration"]), s["caption"]))
            video = self.composer(scenes, item["title"])
        except (TransientError, ValueError, RuntimeError) as exc:
            self._failed(item, meta, cfg, str(exc), report)
            return

        issues = []
        if not MIN_SECONDS <= video.seconds <= MAX_SECONDS:
            issues.append(f"長さが {video.seconds:.0f} 秒です（{MIN_SECONDS:.0f}〜{MAX_SECONDS:.0f}秒）")
        if not video.has_audio:
            issues.append("音声がありません")
        if len(video.data) > MAX_BYTES:
            issues.append(f"ファイルが大きすぎます（{len(video.data) / 1e6:.0f}MB）")
        quality = {"issues": issues, "ok": not issues, "seconds": round(video.seconds, 1),
                   "megabytes": round(len(video.data) / 1e6, 1),
                   "size": f"{video.width}x{video.height}",
                   "needs_human": ["字幕と読み上げの内容が正しいか",
                                   "実在と誤解される映像がないか（YouTubeの合成コンテンツ申告）"]}

        base = f"{self.store.owner_id}/{item['id']}"
        if len(video.data) <= MAX_BYTES:
            self.store.upload(FINALS, f"{base}/short.mp4", video.data, "video/mp4")
        self.store.upload(PREVIEWS, f"{base}/preview.jpg", make_preview(scenes[0].image),
                          "image/jpeg")
        checked = self.store.update_item(
            item["id"], {"preview_path": f"{base}/preview.jpg",
                         "final_path": f"{base}/short.mp4" if len(video.data) <= MAX_BYTES else "",
                         "quality_report": quality, "is_sample": self.sample,
                         "status": "checking"},
            expect_status="image_queued")
        if checked is None:  # cancelled from the phone while we were rendering
            self.store.remove(FINALS, [f"{base}/short.mp4"])
            self.store.remove(PREVIEWS, [f"{base}/preview.jpg"])
            return
        if issues:
            self.store.update_item(item["id"], {"status": "blocked", "blocking": True},
                                   expect_status="checking")
            report.blocked += 1
            return
        self.verdict(checked, acc, report)

    def _failed(self, item: Row, meta: dict[str, Any], cfg: dict[str, Any], why: str,
                report: RunReport) -> None:
        meta["render_attempts"] = int(meta.get("render_attempts", 0)) + 1
        meta["last_render_error"] = why[:300]
        changes: dict[str, Any] = {"meta": meta}
        if meta["render_attempts"] >= int(cfg["max_render_attempts"]):
            changes["status"] = "failed"
            report.failed += 1
        else:
            report.errors.append(f"動画生成（次回再試行）: {why[:120]}")
        self.store.update_item(item["id"], changes, expect_status="image_queued")
