from __future__ import annotations

import datetime as _dt

from aiworker.core import clock, db
from aiworker.core.models import JobStatus, PublishJob
from aiworker.guard import quota

JST = _dt.timezone(_dt.timedelta(hours=9))


def _published(conn, make_item, platform="x", *, at, account="main"):
    item = make_item(platform=platform)
    db.insert_job(conn, PublishJob(
        content_id=item.id, platform=platform, account=account,
        scheduled_at=clock.to_iso(at), status=JobStatus.DONE.value,
        published_at=clock.to_iso(at),
    ))


def test_active_hours_are_enforced(conn, settings):
    early = _dt.datetime(2026, 9, 20, 7, 0, tzinfo=JST)
    ok, why = quota.check_slot(conn, settings, "x", "main", early)
    assert not ok and "active hours" in why


def test_daily_limit_blocks_the_next_post(conn, settings, make_item):
    day = _dt.datetime(2026, 9, 20, 10, 0, tzinfo=JST)
    for i in range(3):  # daily_limit is 3
        _published(conn, make_item, at=day + _dt.timedelta(hours=3 * i))
    ok, why = quota.check_slot(conn, settings, "x", "main",
                               _dt.datetime(2026, 9, 20, 20, 0, tzinfo=JST))
    assert not ok and "daily limit" in why


def test_daily_limit_resets_on_the_next_local_day(conn, settings, make_item):
    day = _dt.datetime(2026, 9, 20, 10, 0, tzinfo=JST)
    for i in range(3):
        _published(conn, make_item, at=day + _dt.timedelta(hours=3 * i))
    ok, _ = quota.check_slot(conn, settings, "x", "main",
                             _dt.datetime(2026, 9, 21, 10, 0, tzinfo=JST))
    assert ok


def test_minimum_interval_is_enforced(conn, settings, make_item):
    _published(conn, make_item, at=_dt.datetime(2026, 9, 20, 12, 0, tzinfo=JST))
    ok, why = quota.check_slot(conn, settings, "x", "main",
                               _dt.datetime(2026, 9, 20, 12, 30, tzinfo=JST))
    assert not ok and "minimum interval" in why
    ok, _ = quota.check_slot(conn, settings, "x", "main",
                             _dt.datetime(2026, 9, 20, 13, 30, tzinfo=JST))
    assert ok


def test_reserved_future_slots_count_against_the_cap(conn, settings, make_item):
    """Two `plan` runs must not be able to double-book the same day."""
    day = _dt.datetime(2026, 9, 20, 10, 0, tzinfo=JST)
    for i in range(3):
        db.insert_job(conn, PublishJob(
            content_id=make_item().id, platform="x",
            scheduled_at=clock.to_iso(day + _dt.timedelta(hours=3 * i)),
            status=JobStatus.QUEUED.value,
        ))
    ok, why = quota.check_slot(conn, settings, "x", "main",
                               _dt.datetime(2026, 9, 20, 20, 30, tzinfo=JST))
    assert not ok and "daily limit" in why


def test_a_job_does_not_crowd_itself_out(conn, settings, make_item):
    """At execution time a job's own reservation must be ignored, or every
    scheduled job would block itself on the minimum-interval rule."""
    at = _dt.datetime(2026, 9, 20, 12, 0, tzinfo=JST)
    job = db.insert_job(conn, PublishJob(
        content_id=make_item().id, platform="x", scheduled_at=clock.to_iso(at),
        status=JobStatus.QUEUED.value,
    ))
    ok, why = quota.check_slot(conn, settings, "x", "main", at)
    assert not ok, "sanity: without the exclusion it collides with itself"
    ok, why = quota.check_slot(conn, settings, "x", "main", at, exclude_job_id=job.id)
    assert ok, why


def test_disabled_platform_never_gets_a_slot(conn, settings):
    ok, why = quota.check_slot(conn, settings, "note", "main",
                               _dt.datetime(2026, 9, 20, 12, 0, tzinfo=JST))
    assert not ok and "disabled" in why


def test_next_slot_respects_every_rule(conn, settings, rng, make_item):
    start = _dt.datetime(2026, 9, 20, 12, 0, tzinfo=JST)
    _published(conn, make_item, at=start)
    slot = quota.next_slot(conn, settings, "x", "main", after=start, rng=rng)
    assert slot is not None
    local = clock.to_local(slot, settings.timezone)
    assert 9 <= local.hour < 21
    assert (slot - start).total_seconds() >= 60 * 60


def test_next_slot_spreads_a_batch_across_days(conn, settings, rng, make_item):
    """daily_limit=3 means a batch of 5 cannot all land on one day."""
    after = _dt.datetime(2026, 9, 20, 9, 0, tzinfo=JST)
    days = []
    for i in range(5):
        slot = quota.next_slot(conn, settings, "x", "main", after=after, rng=rng)
        assert slot is not None
        db.insert_job(conn, PublishJob(content_id=make_item().id, platform="x",
                                       scheduled_at=clock.to_iso(slot),
                                       status=JobStatus.QUEUED.value))
        days.append(clock.local_date(slot, settings.timezone))
    assert max(days.count(d) for d in set(days)) <= 3


def test_scheduling_is_not_a_fixed_cadence(conn, settings):
    """Randomised slots: two planning runs from the same starting point must
    not produce identical times, or the posting pattern is a fingerprint."""
    import random

    after = _dt.datetime(2026, 9, 20, 9, 0, tzinfo=JST)
    a = quota.next_slot(conn, settings, "x", "main", after=after, rng=random.Random(1))
    b = quota.next_slot(conn, settings, "x", "main", after=after, rng=random.Random(2))
    assert a != b


def test_near_limit_warning(conn, settings, make_item):
    day = _dt.datetime.now(JST).replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(3):
        _published(conn, make_item, at=day - _dt.timedelta(minutes=90 * i))
    hot = quota.warn_if_near_limit(conn, settings)
    assert any(s.platform == "x" for s in hot)
