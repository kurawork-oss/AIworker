"""The two publishers that ship enabled.

``dryrun`` proves the pipeline without touching a network. ``manual`` is the
honest default for real operation: it renders an approved item into
``var/outbox/`` as a ready-to-paste file and marks the job done, leaving the
actual posting to a person.

Manual is not a placeholder for "we didn't build the API adapter yet". Several
of these platforms have no sanctioned automation path for this use case, and
posting through an unofficial one is precisely the risk this system exists to
avoid. Automate the *pipeline*; keep a person on the *publish* button until a
platform's own API says otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core import clock
from ..core.logging_setup import get_logger
from ..core.models import ContentItem
from .base import PublishResult

log = get_logger("publish")


class DryRunPublisher:
    name = "dryrun"
    performs_network_io = False

    def publish(self, item: ContentItem) -> PublishResult:
        log.info("[dry-run] would publish %s to %s", item.uid, item.platform,
                 extra={"platform": item.platform, "content_id": item.id})
        return PublishResult(
            True, external_id=f"dryrun-{item.uid}",
            message=f"dry run: nothing was sent to {item.platform}",
        )


class ManualPublisher:
    """Stage content for a human to post."""

    name = "manual"
    performs_network_io = False

    def __init__(self, outbox: Path, timezone: str = "UTC"):
        self.outbox = Path(outbox)
        self.timezone = timezone

    def publish(self, item: ContentItem) -> PublishResult:
        day = clock.local_date(clock.now_utc(), self.timezone)
        folder = self.outbox / day / item.platform
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{item.uid}"
        text_path = folder / f"{stem}.md"
        meta_path = folder / f"{stem}.json"

        text_path.write_text(self._render(item), encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "uid": item.uid, "platform": item.platform, "account": item.account,
                    "channel": item.channel, "approved_by": item.approved_by,
                    "approved_at": item.approved_at, "meta": item.meta,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        log.info("staged %s for manual posting at %s", item.uid, text_path)
        return PublishResult(
            True, external_id=f"manual-{item.uid}", external_url=str(text_path),
            message=f"staged for manual posting: {text_path}",
        )

    @staticmethod
    def _render(item: ContentItem) -> str:
        lines = [
            f"# {item.title or item.uid}",
            "",
            f"- platform : {item.platform} / {item.account}",
            f"- channel  : {item.channel}",
            f"- theme    : {item.theme} / {item.angle} / {item.tone}",
            f"- approved : {item.approved_by} at {item.approved_at}",
            "",
            "## 投稿本文（そのまま貼り付け）",
            "",
            item.body,
            "",
        ]
        if item.meta:
            lines += ["## メタデータ", ""]
            for key, value in sorted(item.meta.items()):
                if key in {"schema_keys", "generator"}:
                    continue
                rendered = ", ".join(map(str, value)) if isinstance(value, list) else value
                lines.append(f"- **{key}**: {rendered}")
            lines.append("")
        lines += [
            "## 投稿前チェック",
            "",
            "- [ ] 本文の事実関係を確認した",
            "- [ ] リンクが正しく開くことを確認した",
            "- [ ] アフィリエイトリンクがある場合、PR表記が入っている",
            "- [ ] AI生成物の開示が必要な場合、開示文が入っている",
            "- [ ] 投稿後、外部URLを `aiworker mark-published` で記録する",
            "",
        ]
        return "\n".join(lines)
