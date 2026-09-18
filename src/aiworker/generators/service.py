"""Generation service: variant -> LLM -> guardrails -> pending queue.

Invariants this module is responsible for:

* Nothing it writes is ever ``approved``. The best outcome here is
  ``pending_review``; the worst is ``blocked``.
* Backpressure: if the approval queue for a channel is already at
  ``generation.max_pending_per_channel``, generation stops. An unbounded queue
  of unreviewed content is how a "human approves everything" system quietly
  turns into a rubber stamp.
* Every item carries the evidence for its own verdict (quality_report,
  policy_report) so the reviewer sees *why* it is flagged.
"""

from __future__ import annotations

import random
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core import db
from ..core.config import Settings
from ..core.errors import TransientError
from ..core.logging_setup import get_logger
from ..core.models import ContentItem, Severity, Status
from ..guard import policy as policy_guard
from ..guard import quality as quality_guard
from .diversity import Variant, plan_variants
from .llm import LLMProvider, LLMRequest, build_provider
from .prompts import ChannelSpec, spec_for

log = get_logger("generate")


@dataclass
class GenerationResult:
    run_id: str
    channel: str
    requested: int
    items: list[ContentItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def pending(self) -> list[ContentItem]:
        return [i for i in self.items if i.status == Status.PENDING_REVIEW.value]

    @property
    def blocked(self) -> list[ContentItem]:
        return [i for i in self.items if i.status == Status.BLOCKED.value]

    def summary(self) -> str:
        return (f"run {self.run_id}: {len(self.pending)} pending, {len(self.blocked)} blocked, "
                f"{len(self.skipped)} skipped (requested {self.requested})")


class GenerationService:
    def __init__(self, conn: sqlite3.Connection, settings: Settings,
                 provider: LLMProvider | None = None, rng: random.Random | None = None):
        self.conn = conn
        self.settings = settings
        self.rng = rng or random.Random()
        self.provider = provider or build_provider(
            settings.generation.provider,
            settings.generation.model,
            settings.generation.timeout_seconds,
        )

    # -- queue pressure ----------------------------------------------------
    def pending_count(self, channel: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM content_items WHERE channel=? AND status IN (?,?)",
            (channel, Status.PENDING_REVIEW.value, Status.NEEDS_REVISION.value),
        ).fetchone()
        return int(row["n"])

    def headroom(self, channel: str) -> int:
        return max(0, self.settings.generation.max_pending_per_channel - self.pending_count(channel))

    # -- main entry point --------------------------------------------------
    def generate(self, channel: str, count: int, *, platform: str | None = None,
                 themes: list[str] | None = None, dry_run: bool = False) -> GenerationResult:
        spec = spec_for(channel)
        run_id = uuid.uuid4().hex[:12]
        result = GenerationResult(run_id=run_id, channel=channel, requested=count)

        room = self.headroom(channel)
        if room <= 0:
            msg = (f"approval queue for '{channel}' is full "
                   f"({self.pending_count(channel)}/"
                   f"{self.settings.generation.max_pending_per_channel}); "
                   "review the backlog before generating more")
            result.skipped.append(msg)
            db.log_event(self.conn, Severity.WARNING, "generate", msg)
            return result
        if count > room:
            result.skipped.append(f"trimmed {count} -> {room} to respect the pending-queue cap")
            count = room

        target_platform = platform or (spec.default_platforms[0] if spec.default_platforms else "")
        variants = plan_variants(
            self.conn, channel, count,
            themes=themes or self.settings.generation.themes,
            angles=self.settings.generation.angles or None,
            tones=self.settings.generation.tones or None,
            rng=self.rng,
        )

        for variant in variants:
            try:
                item = self._generate_one(spec, variant, target_platform, run_id)
            except TransientError as exc:
                result.skipped.append(f"{variant.key}: {exc}")
                db.log_event(self.conn, Severity.ERROR, "generate",
                             f"generation failed for {variant.key}: {exc}")
                continue
            except ValueError as exc:  # malformed model output
                result.skipped.append(f"{variant.key}: {exc}")
                db.log_event(self.conn, Severity.WARNING, "generate",
                             f"unusable model output for {variant.key}: {exc}")
                continue

            if dry_run:
                result.items.append(item)
                continue

            with db.transaction(self.conn):
                db.insert_item(self.conn, item)
                db.log_event(
                    self.conn, Severity.INFO, "generate",
                    f"generated {item.uid} ({channel}) -> {item.status}",
                    platform=item.platform,
                    payload={"run_id": run_id, "variant": variant.key,
                             "similarity": round(item.similarity, 3)},
                )
            result.items.append(item)
        return result

    # -- one item ----------------------------------------------------------
    def _generate_one(self, spec: ChannelSpec, variant: Variant, platform: str,
                      run_id: str) -> ContentItem:
        prompt = spec.prompt_template.format(
            theme=variant.theme, angle=variant.angle, tone=variant.tone
        )
        recent = self._recent_openings(spec.channel)
        if recent:
            prompt += "\n\n直近の投稿の書き出し(重複を避けること):\n" + "\n".join(
                f"- {o}" for o in recent
            )
        request = LLMRequest(
            prompt=prompt, system=spec.system, schema=spec.schema,
            context={"theme": variant.theme, "angle": variant.angle,
                     "tone": variant.tone, "channel": spec.channel},
            max_tokens=spec.max_tokens, temperature=spec.temperature,
            seed=self.rng.randint(0, 2**31),
        )
        raw: dict[str, Any] = self.provider.complete(request)

        title = str(raw.get(spec.title_key, "") or "").strip()
        body = str(raw.get(spec.body_key, "") or "").strip()
        if not body:
            raise ValueError(f"model output has no '{spec.body_key}' field")
        meta = {k: raw[k] for k in spec.meta_keys if k in raw}
        meta["generator"] = self.provider.name
        meta["schema_keys"] = sorted(raw.keys())

        item = ContentItem(
            uid=f"{spec.channel[:4]}-{uuid.uuid4().hex[:10]}",
            channel=spec.channel, platform=platform, account="main",
            theme=variant.theme, angle=variant.angle, tone=variant.tone,
            title=title, body=body, meta=meta, run_id=run_id,
        )
        item.dedupe_hash = quality_guard.fingerprint(f"{title}\n{body}")

        plat_cfg = self.settings.platforms.get(platform)
        if policy_guard.ensure_ai_disclosure(item, plat_cfg):
            log.info("auto-added AI disclosure for %s", item.uid)
        pol = policy_guard.check(item, self.settings.policy, plat_cfg)
        qual = quality_guard.check(item, self.settings.quality, self.conn)
        item.policy_report = pol.as_dict()
        item.quality_report = qual.as_dict()
        item.similarity = qual.similarity
        item.status = (
            Status.BLOCKED.value if (pol.blocking or not qual.ok) else Status.PENDING_REVIEW.value
        )
        return item

    def _recent_openings(self, channel: str, limit: int = 5) -> list[str]:
        rows = self.conn.execute(
            "SELECT body FROM content_items WHERE channel=? ORDER BY id DESC LIMIT ?",
            (channel, limit),
        ).fetchall()
        return [(r["body"] or "").strip().splitlines()[0][:40] for r in rows if r["body"]]


def rescreen(conn: sqlite3.Connection, settings: Settings, item: ContentItem) -> ContentItem:
    """Re-run the guardrails over an item a human edited.

    Used by the approval flow: an edit must not be able to smuggle content past
    checks that only ran at generation time.
    """
    plat_cfg = settings.platforms.get(item.platform)
    policy_guard.ensure_ai_disclosure(item, plat_cfg)
    pol = policy_guard.check(item, settings.policy, plat_cfg)
    qual = quality_guard.check(item, settings.quality, conn)
    item.policy_report = pol.as_dict()
    item.quality_report = qual.as_dict()
    item.similarity = qual.similarity
    item.dedupe_hash = quality_guard.fingerprint(f"{item.title}\n{item.body}")
    return item
