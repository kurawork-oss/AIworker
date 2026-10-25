"""Text businesses: note articles and A8 affiliate articles.

    note記事        Gemini drafts -> checks -> 確認待ち -> approved
                    -> 投稿待ち: the owner pastes it into note (no posting API
                       exists, and an unofficial one is the account risk this
                       system exists to avoid) -> 投稿した + URL
    A8アフィリエイト  a registered program -> Gemini drafts a PR-labelled article
                    from the owner's facts only -> checks -> 確認待ち -> approved
                    -> Hatena Blog via its official API, within the daily limit

Both default to a person approving every article: a bad article goes out
under the owner's name, and an affiliate one carries legal duties
(景品表示法・ステマ規制).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from html import escape
from typing import Any
from urllib.parse import urlsplit

from ..core.config import PlatformConfig
from ..core.errors import TransientError
from ..core.models import ContentItem
from ..generators.diversity import Variant
from ..generators.llm import LLMRequest
from ..generators.prompts import COMMON_RULES, spec_for
from ..guard.policy import has_ai_disclosure
from .base import TZ, BusinessPipeline, RunReport, iso
from .hatena import HatenaPublisher, PublishError, marker
from .store import Row

_URL = re.compile(r"https?://[^\s)\]>\"']+")
AI_NOTE = "※この記事はAIの支援を受けて作成し、公開前に運営者が内容を確認しています。"
PR_NOTE = "※本記事はプロモーション（広告）を含みます。"
LINK_SLOT = "{{LINK}}"


def shingles(text: str, n: int = 3) -> set[str]:
    t = re.sub(r"\s+", "", text)
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))}


def similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class ArticlePipeline(BusinessPipeline):
    CHANNEL = ""
    MIN_CHARS = 1200
    MAX_SIMILARITY = 0.55

    def work(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        self._revise(biz, acc, cfg, report)
        self.publish(biz, acc, cfg, report)
        for variant in self.variants(biz, acc, cfg, report):
            self._create(biz, acc, cfg, variant, report)

    # -- per business ------------------------------------------------------
    def variants(self, biz: Row, acc: Row, cfg: dict[str, Any],
                 report: RunReport) -> list[Variant]:
        return self.plan(biz, cfg, self.room(biz, acc, cfg))

    def brief(self, variant: Variant, cfg: dict[str, Any],
              extra: str = "") -> tuple[str, str, dict[str, Any]]:
        raise NotImplementedError

    def publish(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        """Manual businesses publish nothing: the owner posts from the app."""

    # -- shared ------------------------------------------------------------
    def quality(self, biz: Row, title: str, body: str, item_id: str = "") -> dict[str, Any]:
        issues: list[str] = []
        if len(body) < self.MIN_CHARS:
            issues.append(f"本文が短すぎます（{len(body)}字。{self.MIN_CHARS}字以上）")
        worst = 0.0
        for other in self.store.items(biz["id"], limit=40):
            if other["id"] == item_id or other.get("kind") != self.KIND:
                continue
            if title and other.get("title") == title:
                issues.append("過去の記事と同じタイトルです")
            worst = max(worst, similarity(body, other.get("body") or ""))
        if worst > self.MAX_SIMILARITY:
            issues.append(f"過去の記事と内容が似すぎています（類似度 {worst:.2f}）")
        return {"issues": issues, "ok": not issues, "similarity": round(worst, 3),
                "chars": len(body),
                "needs_human": ["事実関係・数字が正しいか", "自分の名前で出してよい内容か"]}

    def _create(self, biz: Row, acc: Row, cfg: dict[str, Any], variant: Variant,
                report: RunReport) -> None:
        try:
            title, body, meta = self.brief(variant, cfg)
        except (TransientError, ValueError) as exc:
            report.errors.append(f"文章生成: {exc}")
            return
        pol = self.screen(title, body, meta, cfg, self.CHANNEL)
        qual = self.quality(biz, title, body)
        blocked = pol.blocking or not qual["ok"]
        row = {"business_id": biz["id"], "account_id": acc["id"], "kind": self.KIND,
               "title": title, "body": body, "meta": meta, "is_sample": self.sample,
               "policy_report": pol.as_dict(), "quality_report": qual}
        report.created += 1
        if blocked:
            self.store.insert_item({**row, "status": "blocked", "blocking": True})
            report.blocked += 1
            return
        self.verdict(self.store.insert_item({**row, "status": "checking"}), acc, report)

    def _revise(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        for item in self.revision_requests(biz, cfg):
            old = item.get("meta") or {}
            variant = Variant(old.get("theme", ""), old.get("angle", ""), old.get("tone", ""))
            try:
                title, body, meta = self.brief(variant, cfg,
                                               self.revision_extra(item, item.get("body", "")))
            except (TransientError, ValueError) as exc:
                report.errors.append(f"修正の文章生成: {exc}")
                continue
            meta["revisions"] = int(old.get("revisions", 0)) + 1
            meta["revision_note"] = item.get("revision_note", "")
            pol = self.screen(title, body, meta, cfg, self.CHANNEL)
            qual = self.quality(biz, title, body, item["id"])
            blocked = pol.blocking or not qual["ok"]
            redone = self.store.update_item(
                item["id"], {"title": title, "body": body, "meta": meta,
                             "status": "blocked" if blocked else "checking",
                             "policy_report": pol.as_dict(), "quality_report": qual,
                             "blocking": blocked, "is_sample": self.sample},
                expect_status="needs_revision")
            if redone is None:
                continue
            report.revised += 1
            if blocked:
                report.blocked += 1
            else:
                self.verdict(redone, acc, report)  # revisions > 0: always a person

    def complete(self, prompt: str, system: str, schema: dict[str, str], variant: Variant,
                 max_tokens: int, channel: str) -> dict[str, Any]:
        return self.llm.complete(LLMRequest(
            prompt=prompt, system=system, schema=schema,
            context={"theme": variant.theme, "angle": variant.angle, "tone": variant.tone,
                     "channel": channel},
            max_tokens=max_tokens, temperature=1.0, seed=self.rng.randint(0, 2**31 - 1)))


# --------------------------------------------------------------------------
# note
# --------------------------------------------------------------------------
NOTE_THEMES = [
    "生成AIで日々の作業を時短する具体的な手順",
    "AI画像をストックフォトで販売する始め方と注意点",
    "スマホ中心で副業を仕組み化するコツ",
    "AIツールを仕事に取り入れるときの失敗例と対策",
    "AIへの頼み方（プロンプト）の基本と型",
    "副業を続けるための時間の作り方",
]


class NotePipeline(ArticlePipeline):
    CODE = "note_articles"
    NAME = "note記事"
    KIND = "note_article"
    PLATFORM = "note"
    CHANNEL = "note_article"
    MIN_CHARS = 1500
    ACCOUNT_DEFAULTS = {"approval_policy": "required", "publish_method": "manual",
                        "terms_status": "conditional",
                        "terms_source_url": "https://note.com/terms"}
    DEFAULTS = {"themes": NOTE_THEMES, "per_run": 1, "daily_generation_cap": 3,
                "max_backlog": 10}

    def brief(self, variant: Variant, cfg: dict[str, Any],
              extra: str = "") -> tuple[str, str, dict[str, Any]]:
        spec = spec_for("note_article")
        raw = self.complete(
            spec.prompt_template.format(theme=variant.theme, angle=variant.angle,
                                        tone=variant.tone) + extra,
            spec.system, spec.schema, variant, spec.max_tokens, "note_article")
        title = str(raw.get("title") or "").strip()[:80]
        # The model has no business inventing links in the owner's article.
        body = _URL.sub("", str(raw.get("body") or "")).strip()
        if not title or not body:
            raise ValueError("model output has no title or body")
        probe = ContentItem(channel="note_article", title=title, body=body)
        if not has_ai_disclosure(probe, PlatformConfig(name="note")):
            body = f"{body}\n\n{AI_NOTE}"
        try:
            price = max(0, int(raw.get("price_hint_jpy") or 0))
        except (TypeError, ValueError):
            price = 0
        meta = {"theme": variant.theme, "angle": variant.angle, "tone": variant.tone,
                "headline": str(raw.get("headline") or "").strip(),
                "outline": [str(o) for o in raw.get("outline") or []][:8],
                "price_hint_jpy": price, "generator": self.llm.name}
        return title, body, meta


# --------------------------------------------------------------------------
# A8 affiliate -> Hatena Blog
# --------------------------------------------------------------------------
AFFILIATE_ANGLES = ["初めての人向けの選び方", "申し込み前に確認したいポイント",
                    "向いている人・向いていない人", "よくある疑問に答える",
                    "使い始めるまでの流れ", "比べるときの観点"]
AFFILIATE_TONES = ["丁寧", "要点先出し", "対話的"]

AFFILIATE_SYSTEM = (
    "あなたは日本語の紹介記事を書く編集者です。読者が自分に合うかを判断できることを最優先にします。\n"
    "絶対に守ること:\n"
    "- 商品・サービスについて、【提供情報】に書かれていない事実（価格、機能、実績、ランキング、"
    "口コミ、受賞歴、期間限定の特典）を書かない。不明な点は「公式サイトで確認」と書く。\n"
    "- 自分が使ったかのような体験談・感想を作らない。\n"
    "- 効果や収益を断定しない。他社や他の商品をおとしめない。\n"
    "- URLを書かない。申し込みリンクを置く位置に {{LINK}} とだけ書く（1〜2回）。\n"
    "- 出力はJSONオブジェクト1つのみ。\n" + COMMON_RULES
)
AFFILIATE_SCHEMA = {
    "title": "string (32〜40字程度。誇張しない)",
    "body": "string (本文markdown。見出し##を4〜6個、{{LINK}}を1〜2回)",
    "categories": "string[] (ブログのカテゴリ、3つまで)",
    "description": "string (120字以内の要約)",
}


def _tracking_ok(program: Row) -> bool:
    host = (urlsplit(program.get("link_url", "")).hostname or "").lower()
    if program.get("asp", "a8") == "a8":
        return host == "a8.net" or host.endswith(".a8.net")
    return bool(host)


def link_html(program: Row) -> str:
    """The only link in an affiliate article -- built here, never by the model."""
    a = (f'<a href="{escape(program["link_url"])}" rel="nofollow sponsored noopener" '
         f'target="_blank">{escape(program["name"])}の公式サイトを見る</a>')
    if program.get("pixel_url"):
        a += f'<img border="0" width="1" height="1" src="{escape(program["pixel_url"])}" alt="">'
    return a


class AffiliatePipeline(ArticlePipeline):
    CODE = "affiliate"
    NAME = "A8アフィリエイト"
    KIND = "affiliate_article"
    PLATFORM = "hatena_blog"
    CHANNEL = "affiliate_article"
    MIN_CHARS = 1200
    ACCOUNT_DEFAULTS = {"approval_policy": "required", "publish_method": "api",
                        "terms_status": "conditional",
                        "terms_source_url": "https://help.hatenablog.com/entry/atompub"}
    DEFAULTS = {"angles": AFFILIATE_ANGLES, "tones": AFFILIATE_TONES, "per_run": 1,
                "daily_generation_cap": 3, "max_backlog": 10}

    def __init__(self, *args: Any,
                 publisher: Callable[[], HatenaPublisher | None] = HatenaPublisher.from_env,
                 **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._publisher = publisher
        self._programs: dict[str, Row] = {}

    def variants(self, biz: Row, acc: Row, cfg: dict[str, Any],
                 report: RunReport) -> list[Variant]:
        usable = [p for p in self.store.programs()
                  if "blog" in (p.get("media") or ["blog"]) and _tracking_ok(p)]
        self._programs = {p["id"]: p for p in usable}
        if not usable:
            report.notes.append("提携案件が未登録")
            return []
        return self.plan(biz, cfg, self.room(biz, acc, cfg), themes=list(self._programs))

    def brief(self, variant: Variant, cfg: dict[str, Any],
              extra: str = "") -> tuple[str, str, dict[str, Any]]:
        program = self._programs.get(variant.theme)
        if program is None:  # revising an article whose program was removed
            program = next((p for p in self.store.programs() if p["id"] == variant.theme), None)
        if program is None:
            raise ValueError("提携案件が見つかりません（削除または停止）")
        prompt = (
            f"案件「{program['name']}」の紹介記事を、切り口「{variant.angle}」・語り口「{variant.tone}」で"
            "書いてください。\n"
            "- 1800〜3000文字。冒頭で「どんな人向けか」を示す。\n"
            "- 選ぶときに確認すべき点、注意点・向かない人も必ず書く。\n"
            "- 最後に短いまとめ。\n\n"
            f"【提供情報】（これ以外の事実は書かない）\n{program.get('facts') or '（なし）'}"
            + extra)
        raw = self.complete(prompt, AFFILIATE_SYSTEM, AFFILIATE_SCHEMA, variant, 6000,
                            "affiliate_article")
        title = str(raw.get("title") or "").strip()[:80]
        text = _URL.sub("", str(raw.get("body") or "")).strip()
        if not title or not text:
            raise ValueError("model output has no title or body")
        anchor = link_html(program)
        head, *rest = text.split(LINK_SLOT)
        if rest:
            # At most two links however many slots the model left.
            text = head + "".join((anchor if n < 2 else "") + part for n, part in enumerate(rest))
        else:
            text += f"\n\n## 公式サイト\n\n{anchor}"
        uid = uuid.uuid4().hex[:12]
        body = (f"{PR_NOTE}\n\n{text}\n\n---\n\n"
                "※サービス内容・料金は変更されることがあります。最新情報は公式サイトでご確認ください。\n"
                f"{AI_NOTE}\n\n{marker(uid)}")
        meta = {"theme": program["id"], "angle": variant.angle, "tone": variant.tone,
                "program_id": program["id"], "program_name": program["name"],
                "affiliate_url": program["link_url"], "uid": uid,
                "categories": [str(c) for c in raw.get("categories") or []][:3],
                "description": str(raw.get("description") or "").strip()[:200],
                "generator": self.llm.name}
        return title, body, meta

    def publish(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        if acc.get("publish_method") != "api":
            return
        waiting = [i for i in self.store.items(biz["id"], statuses=("approved", "scheduled"),
                                               limit=50) if i.get("kind") == self.KIND]
        if not waiting:
            return
        publisher = self._publisher()
        if publisher is None:
            report.notes.append("はてなブログ未接続")
            return
        midnight = self.now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        done_today = len(self.store.items(biz["id"], statuses=("published",),
                                          published_since=iso(midnight), limit=100))
        budget = int(acc.get("daily_limit", 1)) - done_today
        for item in reversed(waiting):  # oldest first
            if budget <= 0:
                report.notes.append("本日の投稿上限")
                break
            uid = (item.get("meta") or {}).get("uid", "")
            if item["status"] == "approved":
                if self.store.update_item(item["id"], {"status": "scheduled"},
                                          expect_status="approved") is None:
                    continue
                found = None
            else:
                # A previous run claimed it and may have posted it: look first.
                try:
                    found = publisher.find(uid) if uid else None
                except TransientError as exc:
                    report.errors.append(f"投稿確認（次回再試行）: {exc}")
                    continue
            try:
                posted = found or publisher.post(item["title"], item["body"],
                                                 (item.get("meta") or {}).get("categories"))
            except TransientError as exc:
                report.errors.append(f"投稿（次回再試行）: {exc}")
                continue
            except PublishError as exc:
                self.store.update_item(item["id"], {"status": "failed"},
                                       expect_status="scheduled")
                report.failed += 1
                report.errors.append(str(exc))
                continue
            self.store.update_item(
                item["id"], {"status": "published", "external_url": posted.url,
                             "external_id": posted.entry_id, "published_at": iso(self.now)},
                expect_status="scheduled")
            report.published += 1
            budget -= 1
