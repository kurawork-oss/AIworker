"""End-to-end: generate -> approve -> plan -> publish, and the ways it must refuse."""

from __future__ import annotations

import datetime as _dt

from aiworker.approval import service as approval
from aiworker.core import clock, db
from aiworker.core.models import JobStatus, PublishJob, Status
from aiworker.generators.service import GenerationService
from aiworker.guard import killswitch
from aiworker.publishers.base import PublishResult
from aiworker.scheduler import planner

JST = _dt.timezone(_dt.timedelta(hours=9))


class RecordingPublisher:
    """Stands in for a real adapter so tests can assert on what was sent."""

    name = "recording"
    performs_network_io = True

    def __init__(self, fail_with: Exception | None = None):
        self.sent: list[str] = []
        self.fail_with = fail_with

    def publish(self, item):
        if self.fail_with:
            raise self.fail_with
        self.sent.append(item.uid)
        return PublishResult(True, external_id=f"ext-{item.uid}")


def install(monkeypatch, publisher):
    monkeypatch.setattr(planner, "publisher_for", lambda settings, platform: publisher)
    return publisher


def test_happy_path(conn, settings, notifier, monkeypatch):
    pub = install(monkeypatch, RecordingPublisher())
    items = GenerationService(conn, settings).generate("social_post", 2, platform="x").items
    for it in items:
        approval.approve(conn, settings, it.id, actor="tester")

    plan = planner.plan(conn, settings)
    assert len(plan.scheduled) == 2
    for entry in plan.scheduled:
        assert db.get_item(conn, entry.item.id).status == Status.SCHEDULED.value

    last = max(clock.parse_iso(e.job.scheduled_at) for e in plan.scheduled)
    for entry in plan.scheduled:
        planner.run_due(conn, settings, notifier,
                        now=clock.parse_iso(entry.job.scheduled_at), limit=1)
    assert len(pub.sent) >= 1
    published = db.list_items(conn, status=Status.PUBLISHED.value)
    assert published
    assert last is not None


def test_unapproved_content_is_never_published(conn, settings, notifier, monkeypatch):
    """The central invariant. A job pointing at unapproved content must be
    blocked even if something managed to create the job."""
    pub = install(monkeypatch, RecordingPublisher())
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    assert item.status == Status.PENDING_REVIEW.value
    job = db.insert_job(conn, PublishJob(
        content_id=item.id, platform="x",
        scheduled_at=clock.to_iso(clock.now_utc() - _dt.timedelta(minutes=1)),
        status=JobStatus.QUEUED.value,
    ))
    outcomes = planner.run_due(conn, settings, notifier)
    assert pub.sent == []
    assert db.get_job(conn, job.id).status == JobStatus.BLOCKED.value
    assert outcomes and "not approved" in outcomes[0].message


def test_approval_withdrawn_after_scheduling_stops_the_publish(conn, settings, notifier,
                                                               monkeypatch):
    pub = install(monkeypatch, RecordingPublisher())
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    approval.approve(conn, settings, item.id, actor="tester")
    planner.plan(conn, settings)
    approval.reject(conn, item.id, actor="tester", note="撤回")
    planner.run_due(conn, settings, notifier, now=clock.now_utc() + _dt.timedelta(days=2))
    assert pub.sent == []


def test_kill_switch_stops_everything(conn, settings, notifier, monkeypatch):
    pub = install(monkeypatch, RecordingPublisher())
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    approval.approve(conn, settings, item.id, actor="tester")
    plan = planner.plan(conn, settings)
    killswitch.engage(conn, settings.state_dir, reason="テスト")
    planner.run_due(conn, settings, notifier,
                    now=clock.parse_iso(plan.scheduled[0].job.scheduled_at))
    assert pub.sent == []


def test_platform_halt_only_stops_that_platform(conn, settings, notifier, monkeypatch):
    pub = install(monkeypatch, RecordingPublisher())
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    approval.approve(conn, settings, item.id, actor="tester")
    plan = planner.plan(conn, settings)
    killswitch.engage(conn, settings.state_dir, reason="x のみ", platform="x")
    outcomes = planner.run_due(conn, settings, notifier,
                               now=clock.parse_iso(plan.scheduled[0].job.scheduled_at))
    assert pub.sent == []
    assert outcomes and "deferred" in outcomes[0].message


def test_cadence_defers_rather_than_fails(conn, settings, notifier, monkeypatch):
    """Two jobs due at the same instant: one goes, the other is rescheduled --
    and the deferral must not look like a failure to the anomaly detector."""
    pub = install(monkeypatch, RecordingPublisher())
    items = GenerationService(conn, settings).generate("social_post", 2, platform="x").items
    for it in items:
        approval.approve(conn, settings, it.id, actor="tester")
    plan = planner.plan(conn, settings)
    when = max(clock.parse_iso(e.job.scheduled_at) for e in plan.scheduled)
    outcomes = planner.run_due(conn, settings, notifier, now=when)
    assert len(pub.sent) == 1
    deferred = [o for o in outcomes if "deferred" in o.message]
    assert len(deferred) == 1 and deferred[0].ok
    assert db.consecutive_failures(conn, "x") == 0


def test_transient_failures_are_retried_then_recorded(conn, settings, notifier, monkeypatch):
    from aiworker.core.errors import TransientError

    pub = install(monkeypatch, RecordingPublisher(fail_with=TransientError("503")))
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    approval.approve(conn, settings, item.id, actor="tester")
    plan = planner.plan(conn, settings)
    planner.run_due(conn, settings, notifier,
                    now=clock.parse_iso(plan.scheduled[0].job.scheduled_at), sleep=lambda _: None)
    job = db.get_job(conn, plan.scheduled[0].job.id)
    assert job.status in (JobStatus.FAILED.value, JobStatus.QUEUED.value)
    assert "503" in job.last_error


def test_hard_rejection_is_not_retried(conn, settings, notifier, monkeypatch):
    """A rejected post stays rejected; retrying it turns a warning into a strike."""
    from aiworker.core.errors import PublishError

    calls = []

    class Rejecting:
        name, performs_network_io = "rejecting", True

        def publish(self, item):
            calls.append(item.uid)
            raise PublishError("duplicate content")

    install(monkeypatch, Rejecting())
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    approval.approve(conn, settings, item.id, actor="tester")
    plan = planner.plan(conn, settings)
    planner.run_due(conn, settings, notifier,
                    now=clock.parse_iso(plan.scheduled[0].job.scheduled_at), sleep=lambda _: None)
    assert len(calls) == 1
    assert db.get_job(conn, plan.scheduled[0].job.id).status == JobStatus.FAILED.value


def test_dry_run_replaces_network_publishers(conn, settings):
    from aiworker.publishers.registry import publisher_for

    settings.dry_run = True
    settings.platforms["x"].publisher = "manual"
    assert publisher_for(settings, "x").name == "manual"  # manual sends nothing anyway

    from aiworker.publishers import registry

    registry.ADAPTERS["fake_net"] = lambda s: RecordingPublisher()
    try:
        settings.platforms["x"].publisher = "fake_net"
        assert publisher_for(settings, "x").name == "dryrun"
        settings.dry_run = False
        assert publisher_for(settings, "x").name == "recording"
    finally:
        registry.ADAPTERS.pop("fake_net")


def test_manual_publication_counts_against_the_quota(conn, settings, make_item):
    item = make_item(platform="x")
    planner.mark_published(conn, item.uid)
    from aiworker.guard import quota

    st = quota.state(conn, settings, "x")
    assert st.used_today == 1


def test_only_one_open_job_per_item(conn, settings, make_item):
    """The unique index is what stops a double-post if `plan` runs twice."""
    import sqlite3

    import pytest

    item = make_item(platform="x")
    db.insert_job(conn, PublishJob(content_id=item.id, platform="x",
                                   scheduled_at=clock.to_iso(clock.now_utc()),
                                   status=JobStatus.QUEUED.value))
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_job(conn, PublishJob(content_id=item.id, platform="x",
                                       scheduled_at=clock.to_iso(clock.now_utc()),
                                       status=JobStatus.QUEUED.value))


def test_plan_is_idempotent(conn, settings):
    items = GenerationService(conn, settings).generate("social_post", 2, platform="x").items
    for it in items:
        approval.approve(conn, settings, it.id, actor="tester")
    first = planner.plan(conn, settings)
    second = planner.plan(conn, settings)
    assert len(first.scheduled) == 2
    assert len(second.scheduled) == 0


# --------------------------------------------------------------------------
# manual publishing: staged is not published
# --------------------------------------------------------------------------
def _stage_one(conn, settings, notifier, tmp_path):
    """Approve, schedule and run one item through the manual publisher."""
    from aiworker.publishers.dryrun import ManualPublisher

    settings.platforms["x"].publisher = "manual"
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    approval.approve(conn, settings, item.id, actor="tester")
    plan = planner.plan(conn, settings)
    when = clock.parse_iso(plan.scheduled[0].job.scheduled_at)
    planner.run_due(conn, settings, notifier, now=when)
    return db.get_item(conn, item.id), plan.scheduled[0].job, when


def test_manual_publishing_stages_rather_than_publishes(conn, settings, notifier, tmp_path):
    """The manual publisher writes a file; it does not post anything. Marking
    the item published would tell the operator something went out that has
    not, and would leave them without a worklist."""
    item, job, _ = _stage_one(conn, settings, notifier, tmp_path)
    assert db.get_item(conn, item.id).status == Status.STAGED.value
    assert db.get_job(conn, job.id).status == JobStatus.STAGED.value
    assert db.staged_jobs(conn), "the staged item must appear on the worklist"


def test_a_staged_item_still_consumes_its_posting_slot(conn, settings, notifier, tmp_path):
    """Over-counting a slot costs one post; under-counting it costs an account.
    A slot handed to a human must not be released because they have not
    confirmed yet."""
    from aiworker.guard import quota

    _stage_one(conn, settings, notifier, tmp_path)
    assert quota.state(conn, settings, "x").used_this_week == 1


def test_confirming_a_staged_item_does_not_double_count_the_slot(conn, settings, notifier,
                                                                 tmp_path):
    from aiworker.guard import quota

    item, _, _ = _stage_one(conn, settings, notifier, tmp_path)
    before = quota.state(conn, settings, "x").used_this_week
    planner.mark_published(conn, item.uid, external_url="https://example.invalid/1")
    assert quota.state(conn, settings, "x").used_this_week == before
    stored = db.get_item(conn, item.id)
    assert stored.status == Status.PUBLISHED.value


def test_confirming_records_the_external_url(conn, settings, notifier, tmp_path):
    item, job, _ = _stage_one(conn, settings, notifier, tmp_path)
    planner.mark_published(conn, item.uid, external_url="https://example.invalid/42")
    stored = db.get_job(conn, job.id)
    assert stored.status == JobStatus.DONE.value
    assert stored.external_url == "https://example.invalid/42"


def test_a_real_publisher_still_publishes_directly(conn, settings, notifier, monkeypatch):
    """Only publishers that say a human must post are parked in STAGED."""
    pub = install(monkeypatch, RecordingPublisher())
    assert getattr(pub, "stages_for_human", False) is False
    item = GenerationService(conn, settings).generate("social_post", 1, platform="x").items[0]
    approval.approve(conn, settings, item.id, actor="tester")
    plan = planner.plan(conn, settings)
    planner.run_due(conn, settings, notifier,
                    now=clock.parse_iso(plan.scheduled[0].job.scheduled_at))
    assert db.get_item(conn, item.id).status == Status.PUBLISHED.value
