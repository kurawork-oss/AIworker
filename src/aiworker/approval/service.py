"""The approval gate.

This is the only place in the codebase that can move an item into
``APPROVED``. Every transition is recorded in the ``approvals`` table with an
actor, so "who published this" is always answerable after the fact.

Two rules give the gate its teeth:

1. **Re-screening on approve.** Guardrails run again at approval time, over the
   current text. An edit between generation and approval cannot slip past
   checks that only ran before the edit.
2. **Overrides are explicit and loud.** Approving something the guards blocked
   needs ``force=True`` *and* a written reason, and is logged at WARNING with
   the reason attached. Nothing silently downgrades a block.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..core import db
from ..core.config import Settings
from ..core.errors import GuardError
from ..core.logging_setup import get_logger
from ..core.models import ContentItem, Severity, Status
from ..generators.service import rescreen

log = get_logger("approval")

REVIEWABLE = (Status.PENDING_REVIEW.value, Status.BLOCKED.value, Status.NEEDS_REVISION.value)


@dataclass
class Decision:
    item: ContentItem
    action: str
    ok: bool
    message: str = ""

    def __str__(self) -> str:
        mark = "OK" if self.ok else "NG"
        return f"[{mark}] {self.item.uid} {self.action}: {self.message}".rstrip(": ")


def queue(conn: sqlite3.Connection, *, status: str | list[str] | None = None,
          platform: str | None = None, channel: str | None = None,
          limit: int = 50) -> list[ContentItem]:
    return db.list_items(
        conn, status=status or list(REVIEWABLE), platform=platform, channel=channel, limit=limit
    )


def _load(conn: sqlite3.Connection, ref: int | str) -> ContentItem:
    item = db.get_item(conn, int(ref)) if str(ref).isdigit() else db.get_item_by_uid(conn, str(ref))
    if item is None:
        raise GuardError(f"no such content item: {ref}", code="not_found")
    return item


def approve(conn: sqlite3.Connection, settings: Settings, ref: int | str, *,
            actor: str, note: str = "", force: bool = False) -> Decision:
    item = _load(conn, ref)
    if item.status == Status.APPROVED.value:
        return Decision(item, "approve", True, "すでに承認済みです")
    if item.status not in REVIEWABLE:
        return Decision(item, "approve", False,
                        f"状態が「{item.status}」のため承認できません")

    item = rescreen(conn, settings, item)
    blocking = item.policy_report.get("blocking") or not item.quality_report.get("ok", True)
    if blocking and not force:
        reasons = [f["detail"] for f in item.policy_report.get("findings", [])
                   if f["severity"] == "blocking"]
        reasons += [i["detail"] for i in item.quality_report.get("issues", []) if i["fatal"]]
        _persist_reports(conn, item)
        db.update_item_status(conn, item.id, Status.BLOCKED)
        return Decision(
            item, "approve", False,
            "ガードレールがブロックしています: " + " / ".join(reasons) +
            " ｜ 上書きする場合は --force と --note（理由）の両方を付けてください",
        )
    if blocking and force and not note.strip():
        return Decision(item, "approve", False,
                        "上書き承認には理由（--note）が必要です。監査ログに記録されます")

    with db.transaction(conn):
        _persist_reports(conn, item)
        db.update_item_status(conn, item.id, Status.APPROVED, approved_by=actor)
        db.record_approval(conn, item.id, "approve_override" if blocking else "approve",
                           actor, note)
        db.log_event(
            conn, Severity.WARNING if blocking else Severity.INFO, "approval",
            f"{'OVERRIDE ' if blocking else ''}approved {item.uid} by {actor}",
            platform=item.platform,
            payload={"note": note, "forced": bool(blocking and force)},
        )
    item.status = Status.APPROVED.value
    item.approved_by = actor
    return Decision(item, "approve", True,
                    "上書き承認を記録しました" if blocking else "承認しました")


def reject(conn: sqlite3.Connection, ref: int | str, *, actor: str, note: str = "") -> Decision:
    item = _load(conn, ref)
    if item.status == Status.PUBLISHED.value:
        return Decision(item, "reject", False, "すでに公開済みのため却下できません")
    with db.transaction(conn):
        db.update_item_status(conn, item.id, Status.REJECTED)
        db.record_approval(conn, item.id, "reject", actor, note)
        db.log_event(conn, Severity.INFO, "approval", f"rejected {item.uid} by {actor}",
                     platform=item.platform, payload={"note": note})
        job = db.open_job_for_content(conn, item.id)
        if job:
            db.update_job(conn, job.id, status="cancelled",
                          last_error="content rejected after scheduling")
    item.status = Status.REJECTED.value
    return Decision(item, "reject", True, "却下しました")


def request_revision(conn: sqlite3.Connection, ref: int | str, *, actor: str,
                     note: str) -> Decision:
    if not note.strip():
        return Decision(_load(conn, ref), "revise", False,
                        "修正依頼には、どこを直すかの記述（--note）が必要です")
    item = _load(conn, ref)
    with db.transaction(conn):
        db.update_item_status(conn, item.id, Status.NEEDS_REVISION, revision_note=note)
        db.record_approval(conn, item.id, "request_revision", actor, note)
        db.log_event(conn, Severity.INFO, "approval",
                     f"revision requested on {item.uid} by {actor}",
                     platform=item.platform, payload={"note": note})
    item.status = Status.NEEDS_REVISION.value
    item.revision_note = note
    return Decision(item, "revise", True, "修正依頼として差し戻しました")


def edit(conn: sqlite3.Connection, settings: Settings, ref: int | str, *, actor: str,
         title: str | None = None, body: str | None = None,
         meta: dict | None = None) -> Decision:
    """Apply a human edit and re-screen. The item returns to the queue -- an
    edit never carries an earlier approval forward."""
    import json

    item = _load(conn, ref)
    if item.status == Status.PUBLISHED.value:
        return Decision(item, "edit", False, "すでに公開済みのため編集できません")
    if title is not None:
        item.title = title
    if body is not None:
        item.body = body
    if meta:
        item.meta.update(meta)
    item = rescreen(conn, settings, item)
    blocking = item.policy_report.get("blocking") or not item.quality_report.get("ok", True)
    new_status = Status.BLOCKED if blocking else Status.PENDING_REVIEW

    with db.transaction(conn):
        conn.execute(
            "UPDATE content_items SET title=?, body=?, meta=?, dedupe_hash=? WHERE id=?",
            (item.title, item.body, json.dumps(item.meta, ensure_ascii=False),
             item.dedupe_hash, item.id),
        )
        _persist_reports(conn, item)
        db.update_item_status(conn, item.id, new_status)
        db.record_approval(conn, item.id, "edit", actor, "")
        db.log_event(conn, Severity.INFO, "approval", f"edited {item.uid} by {actor}",
                     platform=item.platform, payload={"new_status": new_status.value})
    item.status = new_status.value
    return Decision(item, "edit", True, f"保存しました（現在: {new_status.value}）")


def bulk_approve(conn: sqlite3.Connection, settings: Settings, refs: list[int | str], *,
                 actor: str, note: str = "") -> list[Decision]:
    """Approve several items. Never forces: anything the guards flagged stays
    flagged and must be handled one at a time, deliberately."""
    return [approve(conn, settings, r, actor=actor, note=note, force=False) for r in refs]


def _persist_reports(conn: sqlite3.Connection, item: ContentItem) -> None:
    import json

    conn.execute(
        "UPDATE content_items SET quality_report=?, policy_report=?, similarity=? WHERE id=?",
        (json.dumps(item.quality_report, ensure_ascii=False),
         json.dumps(item.policy_report, ensure_ascii=False), item.similarity, item.id),
    )


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    counts = db.count_by_status(conn)
    return {s.value: counts.get(s.value, 0) for s in Status}
