"""Turn approved stock prompts into image files.

Runs after approval, before the human uploads: it reads staged/approved
stock-asset items, generates the image, and writes it next to the metadata in
`var/outbox/` so the remaining human step is "upload these files".

Uploading stays manual. Adobe Stock's contributor terms state that automated
submission is not permitted, and this system does not breach a platform's terms
to save a click.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..core import clock, db
from ..core.config import Settings
from ..core.errors import TransientError
from ..core.logging_setup import get_logger
from ..core.models import Channel, ContentItem, Severity, Status
from .providers import ImageProvider, ImageRequest, build_provider

log = get_logger("imagegen")


@dataclass
class ImageRun:
    generated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    provider: str = ""
    cost_usd: float = 0.0

    def summary(self) -> str:
        cost = f"、推定コスト ${self.cost_usd:.2f}" if self.cost_usd else "（無料）"
        return (f"{self.provider}: {len(self.generated)}枚生成"
                + (f"、{len(self.failed)}件失敗" if self.failed else "")
                + (f"、{len(self.skipped)}件スキップ" if self.skipped else "")
                + cost)


def image_path(settings: Settings, item: ContentItem) -> Path:
    day = clock.local_date(clock.now_utc(), settings.timezone)
    folder = Path(settings.state_dir) / "outbox" / day / item.platform
    return folder / f"{item.uid}.png"


def pending_items(conn: sqlite3.Connection, settings: Settings, *,
                  limit: int = 50) -> list[ContentItem]:
    """Stock assets that are cleared to exist but have no image file yet."""
    items = db.list_items(
        conn,
        status=[Status.APPROVED.value, Status.SCHEDULED.value, Status.STAGED.value],
        channel=Channel.STOCK_ASSET.value, limit=limit,
    )
    return [i for i in items if not image_path(settings, i).exists()]


def generate(conn: sqlite3.Connection, settings: Settings, *, limit: int = 20,
             provider: ImageProvider | None = None, dry_run: bool = False) -> ImageRun:
    cfg = settings.image
    provider = provider or build_provider(cfg.provider, cfg.model)
    run = ImageRun(provider=provider.name)

    for item in pending_items(conn, settings, limit=limit):
        prompt = str(item.meta.get("prompt", "")).strip()
        if not prompt:
            run.skipped.append(f"{item.uid}: prompt がありません")
            continue
        if dry_run:
            run.generated.append(item.uid)
            continue

        request = ImageRequest(
            prompt=prompt,
            negative_prompt=str(item.meta.get("negative_prompt", "")),
            width=cfg.width, height=cfg.height,
            seed=abs(hash(item.uid)) % (2**31),
        )
        try:
            result = provider.generate(request)
        except TransientError as exc:
            run.failed.append(f"{item.uid}: {exc}")
            continue

        if not result.ok:
            run.failed.append(f"{item.uid}: {result.message}")
            continue

        path = image_path(settings, item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.data)
        # Keywords and the AI disclosure travel with the file, because that is
        # what actually gets typed into the upload form.
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "title": item.title,
                    "description": item.meta.get("description", ""),
                    "keywords": item.meta.get("keywords", []),
                    "ai_generated": True,
                    "prompt": prompt,
                    "generator": provider.name,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        run.generated.append(item.uid)
        run.cost_usd += provider.cost_per_image_usd

    if not dry_run and (run.generated or run.failed):
        with db.transaction(conn):
            db.log_event(
                conn,
                Severity.ERROR if run.failed and not run.generated else Severity.INFO,
                "imagegen", run.summary(),
                payload={"generated": len(run.generated), "failed": len(run.failed)},
            )
    log.info("%s", run.summary())
    return run
