"""What every business's scheduled run has in common.

A business (画像販売, note, A8, ショート動画 ...) differs in what it makes and
where it goes. It does not differ in the rules around that:

* nothing runs while the global stop, the business or the account is off,
  or while the account's terms are unchecked (受入条件6・7);
* how many to make is bounded by the account's daily limit, a daily
  generation cap and a backlog cap;
* variety is planned, not hoped for (theme x angle x tone, least-recent first);
* approval only ever happens through ``approve_item()`` -- and only when the
  owner put the account on auto, the item is clean, it is not a sample and no
  person asked for a revision;
* the run's log line carries counts, never content (Actions logs are public).

Subclasses implement :meth:`work` and whatever it calls.
"""

from __future__ import annotations

import datetime as dt
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import PolicyConfig
from ..core.errors import ConfigError
from ..core.models import ContentItem
from ..generators.diversity import Variant, choose_variants
from ..generators.llm import LLMProvider
from ..guard import policy as policy_guard
from .store import Row, Store, StoreError

TZ = ZoneInfo("Asia/Tokyo")
UNUSABLE = ("blocked", "failed", "cancelled", "rejected")
FINALS = "finals"
CLEANUP_STATUSES = ("published", "rejected", "cancelled", "failed")


@dataclass
class RunReport:
    business: str = ""
    stopped: str = ""
    created: int = 0
    blocked: int = 0
    to_review: int = 0
    auto_approved: int = 0
    revised: int = 0
    retried: int = 0
    published: int = 0
    failed: int = 0
    cleaned: int = 0
    notes: list[str] = field(default_factory=list)   # not errors: "未接続" and the like
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"business": self.business, "stopped": self.stopped, "created": self.created,
                "blocked": self.blocked, "to_review": self.to_review,
                "auto_approved": self.auto_approved, "revised": self.revised,
                "retried": self.retried, "published": self.published, "failed": self.failed,
                "cleaned": self.cleaned, "notes": self.notes, "errors": len(self.errors)}

    def summary(self) -> str:
        head = f"{self.business}: " if self.business else ""
        if self.stopped:
            return f"{head}停止: {self.stopped}"
        parts = [f"新規 {self.created}", f"確認待ち {self.to_review}",
                 f"自動承認 {self.auto_approved}", f"ブロック {self.blocked}",
                 f"修正 {self.revised}", f"再試行 {self.retried}", f"公開 {self.published}",
                 f"失敗 {self.failed}", f"整理 {self.cleaned}", f"エラー {len(self.errors)}"]
        tail = f"（{'・'.join(self.notes)}）" if self.notes else ""
        return head + " / ".join(parts) + tail

    @property
    def config_error(self) -> bool:
        return any(e.startswith("設定エラー") for e in self.errors)


def iso(d: dt.datetime) -> str:
    return d.astimezone(dt.UTC).isoformat()


class BusinessPipeline:
    CODE = ""
    NAME = ""
    KIND = ""
    PLATFORM = ""
    #: How the account is created on first run. Approval defaults to a
    #: person; a subclass argues for anything else in its own docstring.
    ACCOUNT_DEFAULTS: dict[str, Any] = {"approval_policy": "required", "publish_method": "manual",
                                        "terms_status": "unverified"}
    DEFAULTS: dict[str, Any] = {}
    BASE_DEFAULTS: dict[str, Any] = {
        "themes": [], "angles": [], "tones": [],
        "per_run": 1, "daily_generation_cap": 5, "max_backlog": 20,
        "revisions_per_run": 3, "banned_terms": [],
    }

    def __init__(self, store: Store, llm: LLMProvider, *, rng: random.Random | None = None,
                 now: dt.datetime | None = None, worker_id: str = "github-actions"):
        self.store = store
        self.llm = llm
        self.rng = rng or random.Random()
        self.now = now or dt.datetime.now(TZ)
        self.worker_id = worker_id

    # -- identity ----------------------------------------------------------
    @property
    def sample(self) -> bool:
        """Anything made with a stand-in generator is test output, end to end."""
        return self.llm.name == "mock"

    def ensure_setup(self) -> tuple[Row, Row]:
        biz = self.store.business(self.CODE)
        if biz is None:
            biz = self.store.create_business({"code": self.CODE, "name": self.NAME,
                                              "stage": "verifying", "settings": {}})
        accounts = [a for a in self.store.accounts(biz["id"]) if a["platform"] == self.PLATFORM]
        acc = accounts[0] if accounts else self.store.create_account(
            {"business_id": biz["id"], "platform": self.PLATFORM, **self.ACCOUNT_DEFAULTS})
        return biz, acc

    def settings(self, biz: Row) -> dict[str, Any]:
        merged = {**self.BASE_DEFAULTS, **self.DEFAULTS}
        merged.update({k: v for k, v in (biz.get("settings") or {}).items()
                       if v not in (None, "", [])})
        return merged

    # -- the run -----------------------------------------------------------
    def run(self) -> RunReport:
        report = RunReport(business=self.NAME)
        if self.store.controls().get("global_stop"):
            report.stopped = "全体停止中"
            return report
        self.store.heartbeat(self.worker_id, {"business": self.CODE, "llm": self.llm.name})
        biz, acc = self.ensure_setup()
        report.business = biz["name"]
        if biz.get("stage") == "stopped":
            report.stopped = "事業が停止中"
            return report
        if acc.get("status") != "active":
            report.stopped = "アカウントが停止中"
            return report
        # 受入条件7: terms nobody has checked (or that forbid this use) stop
        # the work, rather than producing things that cannot be used.
        if acc.get("terms_status") in ("unverified", "forbidden"):
            report.stopped = "規約が未確認または不可のアカウント"
            return report
        try:
            self.work(biz, acc, self.settings(biz), report)
        except ConfigError as exc:
            # A missing or wrong key will not fix itself on the next item.
            report.errors.append(f"設定エラー: {exc}")
        level = "error" if report.errors else "info"
        self.store.log(level, "pipeline", report.summary(),
                       {**report.as_dict(), "sample": self.sample,
                        "first_error": report.errors[0][:300] if report.errors else ""})
        return report

    def work(self, biz: Row, acc: Row, cfg: dict[str, Any], report: RunReport) -> None:
        raise NotImplementedError

    # -- how many, and which -----------------------------------------------
    def room(self, biz: Row, acc: Row, cfg: dict[str, Any]) -> int:
        midnight = self.now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.store.items(biz["id"], created_since=iso(midnight), limit=1000)
        usable_today = sum(1 for i in today if i["status"] not in UNUSABLE)
        backlog = len(self.store.items(
            biz["id"], statuses=("pending_review", "awaiting_manual", "needs_revision"), limit=1000))
        return max(0, min(int(cfg["per_run"]),
                          int(acc.get("daily_limit", 1)) - usable_today,
                          int(cfg["daily_generation_cap"]) - len(today),
                          int(cfg["max_backlog"]) - backlog))

    def plan(self, biz: Row, cfg: dict[str, Any], count: int,
             themes: list[str] | None = None) -> list[Variant]:
        if count <= 0:
            return []
        combos: Counter[str] = Counter()
        used_theme: Counter[str] = Counter()
        used_angle: Counter[str] = Counter()
        for i in self.store.items(biz["id"], limit=60):
            m = i.get("meta") or {}
            combos[f"{m.get('theme')}|{m.get('angle')}|{m.get('tone')}"] += 1
            used_theme[m.get("theme", "")] += 1
            used_angle[m.get("angle", "")] += 1
        return choose_variants(count, used_combo=combos, used_theme=used_theme,
                               used_angle=used_angle,
                               themes=themes if themes is not None else list(cfg["themes"]),
                               angles=list(cfg["angles"]) or None,
                               tones=list(cfg["tones"]) or None, rng=self.rng)

    # -- checks and verdicts -----------------------------------------------
    def screen(self, title: str, body: str, meta: dict[str, Any], cfg: dict[str, Any],
               channel: str) -> policy_guard.PolicyReport:
        probe = ContentItem(channel=channel, platform=self.PLATFORM,
                            title=title, body=body, meta=meta)
        return policy_guard.check(probe, PolicyConfig(banned_terms=list(cfg["banned_terms"])))

    def verdict(self, item: Row, acc: Row, report: RunReport) -> None:
        """Move a `checking` item on: auto-approved, or to a person."""
        meta = item.get("meta") or {}
        wants_person = (acc.get("approval_policy") != "auto" or self.sample
                        or int(meta.get("revisions", 0)) > 0)
        if not wants_person:
            try:
                self.store.approve(item["id"], item["version"], "自動チェック通過")
                report.auto_approved += 1
                return
            except StoreError as exc:
                report.errors.append(f"自動承認できず確認待ちへ: {exc}")
        self.store.update_item(item["id"], {"status": "pending_review"}, expect_status="checking")
        report.to_review += 1

    def revision_requests(self, biz: Row, cfg: dict[str, Any]) -> list[Row]:
        asked = self.store.items(biz["id"], statuses=("needs_revision",),
                                 limit=int(cfg["revisions_per_run"]))
        return [i for i in reversed(asked) if i.get("kind") == self.KIND]  # oldest first

    def revision_extra(self, item: Row, previous: str) -> str:
        return ("\n\n前回の出力:\n" + previous[:1500] +
                "\n\n依頼者からの修正指示（最優先で反映）:\n" + item.get("revision_note", ""))

    # -- storage hygiene ---------------------------------------------------
    def cleanup_finals(self, biz: Row, report: RunReport) -> None:
        """Delete full-size files nobody will publish any more.

        A stock JPEG is ~1-2 MB and a Short ~5-15 MB; the free storage tier
        is 1 GB. Previews stay as the record of what was made.
        """
        done = [i for i in self.store.items(biz["id"], statuses=CLEANUP_STATUSES, limit=200)
                if i.get("final_path")]
        if not done:
            return
        self.store.remove(FINALS, [i["final_path"] for i in done])
        for i in done:
            self.store.update_item(i["id"], {"final_path": ""})
            report.cleaned += 1
