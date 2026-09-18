"""Scheduling and execution of publish jobs.

Two phases, deliberately separate:

* :func:`plan` turns approved items into jobs with randomised future slots.
* :func:`run_due` executes jobs whose slot has arrived.

The separation matters because the checks that were true at planning time may
be false at execution time -- a kill switch may have been thrown, the day's
quota may have been spent by another process, the item may have been rejected
after being scheduled. So ``run_due`` re-verifies *everything* immediately
before handing an item to a publisher, and treats the planning-time decision as
a hint, never as a permission.
"""

from __future__ import annotations

import datetime as _dt
import random
import sqlite3
from dataclasses import dataclass, field
from typing import NamedTuple

from ..core import clock, db
from ..core.config import Settings
from ..core.logging_setup import get_logger
from ..core.models import ContentItem, JobStatus, PublishJob, Severity, Status
from ..guard import anomaly, killswitch, quota
from ..notify.notifier import Alert, Notifier
from ..publishers.base import with_retry
from ..publishers.registry import publisher_for

log = get_logger("scheduler")


@dataclass
class PlanEntry:
    item: ContentItem
    job: PublishJob | None = None
    skipped: str = ""


@dataclass
class PlanResult:
    entries: list[PlanEntry] = field(default_factory=list)

    @property
    def scheduled(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.job is not None]

    @property
    def skipped(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.job is None]

    def summary(self) -> str:
        return f"{len(self.scheduled)} scheduled, {len(self.skipped)} skipped"


@dataclass
class RunOutcome:
    job: PublishJob
    item: ContentItem | None
    ok: bool
    message: str

    def __str__(self) -> str:
        return f"[{'ok' if self.ok else 'NG'}] job {self.job.id} ({self.job.platform}): {self.message}"


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------
def plan(conn: sqlite3.Connection, settings: Settings, *, limit: int = 20,
         platform: str | None = None, rng: random.Random | None = None,
         horizon_days: int = 7) -> PlanResult:
    rng = rng or random.Random()
    result = PlanResult()
    items = db.list_items(conn, status=Status.APPROVED.value, platform=platform, limit=limit)

    for item in items:
        if db.open_job_for_content(conn, item.id):
            result.entries.append(PlanEntry(item, skipped="already has an open job"))
            continue
        halt = killswitch.check(conn, settings.state_dir, item.platform)
        if halt.halted:
            result.entries.append(PlanEntry(item, skipped=halt.describe()))
            continue
        try:
            cfg = settings.platform(item.platform)
        except Exception as exc:
            result.entries.append(PlanEntry(item, skipped=str(exc)))
            continue
        if not cfg.enabled:
            result.entries.append(PlanEntry(item, skipped=f"{item.platform} is disabled"))
            continue

        slot = quota.next_slot(conn, settings, item.platform, item.account, rng=rng,
                               horizon_days=horizon_days)
        if slot is None:
            result.entries.append(PlanEntry(
                item, skipped=f"no slot within {horizon_days}d that satisfies the cadence rules"
            ))
            continue

        with db.transaction(conn):
            job = db.insert_job(conn, PublishJob(
                content_id=item.id, platform=item.platform, account=item.account,
                scheduled_at=clock.to_iso(slot), status=JobStatus.QUEUED.value,
            ))
            db.update_item_status(conn, item.id, Status.SCHEDULED)
            db.log_event(
                conn, Severity.INFO, "schedule",
                f"scheduled {item.uid} for {clock.fmt_local(slot, settings.timezone)}",
                platform=item.platform, payload={"job_id": job.id},
            )
        result.entries.append(PlanEntry(item, job=job))
    return result


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
class Refusal(NamedTuple):
    """Why a job may not run right now, and what to do about it.

    ``kind`` separates the two cases that look alike in a log but are opposite
    operationally:

    * ``defer`` - nothing is wrong. The cadence rules say "not this minute", so
      the job moves to the next legal slot and stays queued. This is the system
      working, not failing, and it must not count towards the consecutive-
      failure detector.
    * ``block`` - a human has to look. The item is not approved, its approval
      was withdrawn, or a guardrail verdict changed after scheduling.
    """

    reason: str
    kind: str  # "defer" | "block"


OK = Refusal("", "")


def _preflight(conn: sqlite3.Connection, settings: Settings, job: PublishJob,
               item: ContentItem | None, now: _dt.datetime) -> Refusal:
    """The last line of defence: re-verify everything immediately before
    handing an item to a publisher."""
    if item is None:
        return Refusal("content item no longer exists", "block")
    if item.status not in (Status.SCHEDULED.value, Status.APPROVED.value):
        return Refusal(f"content is '{item.status}', not approved/scheduled", "block")
    if not item.approved_at or not item.approved_by:
        return Refusal("content carries no approval record", "block")
    if item.policy_report.get("blocking"):
        override = any(
            r["action"] == "approve_override" for r in db.approval_history(conn, item.id)
        )
        if not override:
            return Refusal("policy report is blocking and no override was recorded", "block")

    halt = killswitch.check(conn, settings.state_dir, job.platform)
    if halt.halted:
        return Refusal(halt.describe(), "defer")
    cfg = settings.platform(job.platform)
    if not cfg.enabled:
        return Refusal(f"platform '{job.platform}' is disabled", "defer")
    ok, why = quota.check_slot(conn, settings, job.platform, job.account, now,
                               exclude_job_id=job.id)
    if not ok:
        return Refusal(why, "defer")
    return OK


def run_due(conn: sqlite3.Connection, settings: Settings, notifier: Notifier, *,
            now: _dt.datetime | None = None, limit: int = 20,
            platform: str | None = None, sleep=None) -> list[RunOutcome]:
    now = now or clock.now_utc()
    outcomes: list[RunOutcome] = []

    halt = killswitch.check(conn, settings.state_dir)
    if halt.halted:
        db.log_event(conn, Severity.WARNING, "publish",
                     f"run skipped entirely: {halt.describe()}")
        return outcomes

    for job in db.due_jobs(conn, now_iso=clock.to_iso(now), platform=platform, limit=limit):
        item = db.get_item(conn, job.content_id)
        refusal = _preflight(conn, settings, job, item, now)
        if refusal.kind == "block":
            db.update_job(conn, job.id, status=JobStatus.BLOCKED.value,
                          last_error=refusal.reason)
            db.log_event(conn, Severity.WARNING, "publish",
                         f"job {job.id} blocked: {refusal.reason}", platform=job.platform,
                         payload={"job_id": job.id, "content_id": job.content_id})
            notifier.send(Alert(Severity.WARNING, f"job {job.id} needs a human",
                                refusal.reason, job.platform))
            outcomes.append(RunOutcome(job, item, False, f"blocked: {refusal.reason}"))
            continue
        if refusal.kind == "defer":
            nxt = quota.next_slot(conn, settings, job.platform, job.account,
                                  after=now, exclude_job_id=job.id)
            if nxt is None:
                db.update_job(conn, job.id, status=JobStatus.BLOCKED.value,
                              last_error=f"{refusal.reason}; no slot within the horizon")
                outcomes.append(RunOutcome(job, item, False,
                                           f"blocked: {refusal.reason} (no slot available)"))
                continue
            db.update_job(conn, job.id, status=JobStatus.QUEUED.value,
                          scheduled_at=clock.to_iso(nxt), last_error=refusal.reason)
            db.log_event(conn, Severity.INFO, "publish",
                         f"job {job.id} deferred to "
                         f"{clock.fmt_local(nxt, settings.timezone)}: {refusal.reason}",
                         platform=job.platform, payload={"job_id": job.id})
            outcomes.append(RunOutcome(
                job, item, True,
                f"deferred to {clock.fmt_local(nxt, settings.timezone)} ({refusal.reason})",
            ))
            continue

        assert item is not None
        cfg = settings.platform(job.platform)
        publisher = publisher_for(settings, job.platform)
        db.update_job(conn, job.id, status=JobStatus.RUNNING.value,
                      attempts=job.attempts + 1)

        kwargs = {"attempts": cfg.max_retries, "backoff_seconds": cfg.retry_backoff_seconds}
        if sleep is not None:
            kwargs["sleep"] = sleep
        result = with_retry(lambda: publisher.publish(item), **kwargs)

        if result.ok:
            # A publisher that only prepares content has not published anything.
            # The slot is spent (so the quota still counts it), but the item
            # waits in STAGED until a person confirms they posted it.
            staged = getattr(publisher, "stages_for_human", False)
            job_status = JobStatus.STAGED.value if staged else JobStatus.DONE.value
            item_status = Status.STAGED if staged else Status.PUBLISHED
            with db.transaction(conn):
                db.update_job(conn, job.id, status=job_status,
                              external_id=result.external_id,
                              external_url=result.external_url,
                              published_at=clock.to_iso(now), last_error="")
                db.update_item_status(conn, item.id, item_status)
                db.log_event(conn, Severity.INFO, "publish",
                             f"{'staged' if staged else 'published'} {item.uid} "
                             f"via {publisher.name}: {result.message}",
                             platform=job.platform,
                             payload={"job_id": job.id, "external_id": result.external_id})
            outcomes.append(RunOutcome(job, item, True, result.message))
        else:
            retryable = result.detail.get("retryable", True)
            exhausted = (job.attempts + 1) >= cfg.max_retries or not retryable
            status = JobStatus.FAILED.value if exhausted else JobStatus.QUEUED.value
            next_slot_iso = job.scheduled_at
            if not exhausted:
                nxt = quota.next_slot(conn, settings, job.platform, job.account,
                                      after=now + _dt.timedelta(minutes=cfg.retry_backoff_seconds))
                next_slot_iso = clock.to_iso(nxt) if nxt else job.scheduled_at
            with db.transaction(conn):
                db.update_job(conn, job.id, status=status, last_error=result.message,
                              scheduled_at=next_slot_iso)
                if exhausted:
                    db.update_item_status(conn, item.id, Status.FAILED)
                db.log_event(conn, Severity.ERROR, "publish",
                             f"publish failed for {item.uid}: {result.message}",
                             platform=job.platform,
                             payload={"job_id": job.id, "exhausted": exhausted})
            notifier.send(Alert(
                Severity.ERROR, f"publish failed: {item.uid}",
                f"{result.message}\nplatform: {job.platform}\n"
                f"{'no further retries' if exhausted else 'will retry'}",
                job.platform,
            ))
            outcomes.append(RunOutcome(job, item, False, result.message))

    found = anomaly.scan(conn, settings)
    if found:
        anomaly.react(conn, settings, found, notifier, state_dir=settings.state_dir)
    anomaly.mark_scanned(conn)
    return outcomes


def mark_published(conn: sqlite3.Connection, ref: int | str, *, external_url: str = "",
                   external_id: str = "") -> str:
    """Record that a person actually posted a staged item.

    The posting slot was already counted when the item was staged -- this
    closes the loop, so the operator's worklist empties and the dashboard
    stops showing it as outstanding.
    """
    item = db.get_item(conn, int(ref)) if str(ref).isdigit() else db.get_item_by_uid(conn, str(ref))
    if item is None:
        return f"no such content item: {ref}"
    job = db.open_job_for_content(conn, item.id) or _staged_job_for(conn, item.id)
    now = clock.to_iso(clock.now_utc())
    with db.transaction(conn):
        if job:
            db.update_job(conn, job.id, status=JobStatus.DONE.value,
                          published_at=job.published_at or now,
                          external_url=external_url or job.external_url,
                          external_id=external_id or job.external_id)
        else:
            db.insert_job(conn, PublishJob(
                content_id=item.id, platform=item.platform, account=item.account,
                scheduled_at=now, status=JobStatus.DONE.value, published_at=now,
                external_url=external_url, external_id=external_id,
            ))
        db.update_item_status(conn, item.id, Status.PUBLISHED)
        db.log_event(conn, Severity.INFO, "publish",
                     f"manually published {item.uid}", platform=item.platform,
                     payload={"external_url": external_url})
    return f"{item.uid} を公開済みとして記録しました"


def _staged_job_for(conn: sqlite3.Connection, content_id: int) -> PublishJob | None:
    row = conn.execute(
        "SELECT * FROM publish_jobs WHERE content_id=? AND status=? ORDER BY id DESC LIMIT 1",
        (content_id, JobStatus.STAGED.value),
    ).fetchone()
    if row is None:
        return None
    return PublishJob(
        id=row["id"], content_id=row["content_id"], platform=row["platform"],
        account=row["account"], scheduled_at=row["scheduled_at"], status=row["status"],
        attempts=row["attempts"], last_error=row["last_error"],
        external_id=row["external_id"], external_url=row["external_url"],
        published_at=row["published_at"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
