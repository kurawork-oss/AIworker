from __future__ import annotations

import datetime as _dt

from aiworker.core import clock, db
from aiworker.core.models import JobStatus, PublishJob, Severity
from aiworker.guard import anomaly, killswitch
from aiworker.notify.notifier import Alert, Notifier


class CapturingNotifier(Notifier):
    def __init__(self, config):
        super().__init__(config)
        self.alerts: list[Alert] = []

    def send(self, alert: Alert):
        self.alerts.append(alert)
        return {"capture": True}


def _job(conn, make_item, status, platform="x", error=""):
    item = make_item(platform=platform)
    return db.insert_job(conn, PublishJob(
        content_id=item.id, platform=platform,
        scheduled_at=clock.to_iso(clock.now_utc()), status=status, last_error=error,
    ))


def test_consecutive_failures_are_detected(conn, settings, make_item):
    for _ in range(2):  # threshold is 2 in the test settings
        _job(conn, make_item, JobStatus.FAILED.value, error="500")
    found = anomaly.detect_consecutive_failures(conn, settings)
    assert found and found[0].code == "consecutive_failures" and found[0].halt


def test_a_success_resets_the_failure_streak(conn, settings, make_item):
    _job(conn, make_item, JobStatus.FAILED.value)
    _job(conn, make_item, JobStatus.FAILED.value)
    _job(conn, make_item, JobStatus.DONE.value)
    assert anomaly.detect_consecutive_failures(conn, settings) == []


def test_failures_trigger_an_automatic_halt(conn, settings, make_item):
    notifier = CapturingNotifier(settings.notify)
    for _ in range(2):
        _job(conn, make_item, JobStatus.FAILED.value, error="500")
    anomaly.react(conn, settings, anomaly.scan(conn, settings), notifier,
                  state_dir=settings.state_dir)
    assert killswitch.check(conn, settings.state_dir, "x").halted
    assert any("自動停止" in a.title for a in notifier.alerts)


def test_auto_halt_can_be_switched_off(conn, settings, make_item):
    settings.anomaly.auto_halt_on_failures = False
    for _ in range(2):
        _job(conn, make_item, JobStatus.FAILED.value)
    anomaly.react(conn, settings, anomaly.scan(conn, settings),
                  CapturingNotifier(settings.notify), state_dir=settings.state_dir)
    assert not killswitch.check(conn, settings.state_dir, "x").halted


def test_platform_warning_language_halts_immediately(conn, settings, make_item):
    _job(conn, make_item, JobStatus.FAILED.value,
         error="Your account has been restricted for policy violation")
    found = anomaly.detect_platform_warnings(conn, settings)
    assert found and found[0].halt
    assert found[0].severity is Severity.CRITICAL


def test_japanese_warning_language_is_detected(conn, settings, make_item):
    _job(conn, make_item, JobStatus.FAILED.value, error="アカウント停止のお知らせ")
    assert anomaly.detect_platform_warnings(conn, settings)


def test_reach_collapse_is_detected(conn, settings):
    base = _dt.date(2026, 9, 10)
    for i in range(4):
        db.insert_metric(conn, date=(base + _dt.timedelta(days=i)).isoformat(),
                         platform="x", reach=1000)
    db.insert_metric(conn, date=(base + _dt.timedelta(days=4)).isoformat(),
                     platform="x", reach=50)
    found = anomaly.detect_reach_drop(conn, settings)
    assert found and found[0].code == "reach_drop"
    assert not found[0].halt, "a reach drop alerts a human; it does not halt on its own"


def test_normal_variation_is_not_an_anomaly(conn, settings):
    base = _dt.date(2026, 9, 10)
    for i, reach in enumerate([1000, 1100, 900, 1050, 850]):
        db.insert_metric(conn, date=(base + _dt.timedelta(days=i)).isoformat(),
                         platform="x", reach=reach)
    assert anomaly.detect_reach_drop(conn, settings) == []


def test_one_viral_day_does_not_make_the_next_day_look_like_a_collapse(conn, settings):
    """Median, not mean: a spike must not raise the baseline for days afterwards."""
    base = _dt.date(2026, 9, 10)
    for i, reach in enumerate([1000, 1000, 50000, 1000, 900]):
        db.insert_metric(conn, date=(base + _dt.timedelta(days=i)).isoformat(),
                         platform="x", reach=reach)
    assert anomaly.detect_reach_drop(conn, settings) == []


def test_insufficient_history_is_not_an_anomaly(conn, settings):
    db.insert_metric(conn, date="2026-09-10", platform="x", reach=1000)
    db.insert_metric(conn, date="2026-09-11", platform="x", reach=10)
    assert anomaly.detect_reach_drop(conn, settings) == []


def test_every_anomaly_is_written_to_the_audit_log(conn, settings, make_item):
    for _ in range(2):
        _job(conn, make_item, JobStatus.FAILED.value)
    anomaly.react(conn, settings, anomaly.scan(conn, settings),
                  CapturingNotifier(settings.notify), state_dir=settings.state_dir)
    assert db.recent_events(conn, category="anomaly")
