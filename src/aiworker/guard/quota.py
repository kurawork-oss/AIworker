"""Rate limiting and posting-cadence rules.

Three separate limits, all enforced together, because each one models a
different way an account gets flagged:

* **daily / weekly caps** - total volume.
* **minimum interval** - burstiness. Ten posts in ten minutes reads as a bot
  even when the daily total is modest.
* **active hours** - a human does not post at 04:00 every single night.

Slots already handed out to future jobs count against the caps as well, so
running ``plan`` twice cannot double-book a day.
"""

from __future__ import annotations

import datetime as _dt
import random
import sqlite3
from dataclasses import dataclass, field

from ..core import clock, db
from ..core.config import PlatformConfig, Settings


@dataclass
class QuotaState:
    platform: str
    account: str
    used_today: int
    used_this_week: int
    daily_limit: int
    weekly_limit: int
    last_slot: _dt.datetime | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self.used_today)

    @property
    def remaining_week(self) -> int:
        return max(0, self.weekly_limit - self.used_this_week)

    @property
    def capacity(self) -> int:
        return min(self.remaining_today, self.remaining_week)

    @property
    def near_limit(self) -> bool:
        if not self.daily_limit:
            return False
        return self.used_today / self.daily_limit >= 0.8

    def summary(self) -> str:
        return (f"{self.platform}/{self.account}: today {self.used_today}/{self.daily_limit}, "
                f"week {self.used_this_week}/{self.weekly_limit}")


def _slots_in_use(conn: sqlite3.Connection, platform: str, account: str,
                  since: _dt.datetime, exclude_job_id: int | None = None) -> list[_dt.datetime]:
    """Published + already-scheduled timestamps since ``since``."""
    since_iso = clock.to_iso(since)
    stamps = db.published_timestamps(conn, platform, account, since_iso=since_iso)
    stamps += db.scheduled_timestamps(conn, platform, account, since_iso=since_iso,
                                      exclude_job_id=exclude_job_id)
    return sorted(clock.parse_iso(s) for s in stamps)


def state(conn: sqlite3.Connection, settings: Settings, platform: str,
          account: str = "main", *, at: _dt.datetime | None = None,
          exclude_job_id: int | None = None) -> QuotaState:
    cfg = settings.platform(platform)
    now = at or clock.now_utc()
    tz_name = settings.timezone
    local_now = clock.to_local(now, tz_name)

    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - _dt.timedelta(days=local_now.weekday())

    slots = _slots_in_use(conn, platform, account, week_start.astimezone(clock.UTC),
                          exclude_job_id)
    today_key = local_now.date().isoformat()
    used_today = sum(1 for s in slots if clock.local_date(s, tz_name) == today_key)
    used_week = len(slots)
    last = max((s for s in slots), default=None)
    return QuotaState(
        platform=platform, account=account, used_today=used_today, used_this_week=used_week,
        daily_limit=cfg.daily_limit, weekly_limit=cfg.weekly_limit, last_slot=last,
    )


def all_states(conn: sqlite3.Connection, settings: Settings) -> list[QuotaState]:
    out = []
    for name, cfg in settings.platforms.items():
        for account in cfg.accounts:
            out.append(state(conn, settings, name, account))
    return out


def check_slot(conn: sqlite3.Connection, settings: Settings, platform: str, account: str,
               when: _dt.datetime, *, exclude_job_id: int | None = None) -> tuple[bool, str]:
    """Can one item go out at ``when``? Returns (ok, reason-if-not)."""
    cfg = settings.platform(platform)
    if not cfg.enabled:
        return False, f"platform '{platform}' is disabled in config"

    local = clock.to_local(when, settings.timezone)
    lo, hi = cfg.active_hours
    if not (lo <= local.hour < hi):
        return False, f"{local.strftime('%H:%M')} is outside active hours {lo:02d}-{hi:02d}"

    st = state(conn, settings, platform, account, at=when, exclude_job_id=exclude_job_id)
    if st.used_today >= cfg.daily_limit:
        return False, f"daily limit reached ({st.used_today}/{cfg.daily_limit})"
    if cfg.weekly_limit and st.used_this_week >= cfg.weekly_limit:
        return False, f"weekly limit reached ({st.used_this_week}/{cfg.weekly_limit})"

    if cfg.min_interval_minutes:
        window = _dt.timedelta(minutes=cfg.min_interval_minutes)
        for slot in _slots_in_use(conn, platform, account, when - window * 2, exclude_job_id):
            if abs((slot - when).total_seconds()) < window.total_seconds():
                mins = int(abs((slot - when).total_seconds()) // 60)
                return False, (f"only {mins}min from another post; "
                               f"minimum interval is {cfg.min_interval_minutes}min")
    return True, ""


def next_slot(conn: sqlite3.Connection, settings: Settings, platform: str, account: str,
              *, after: _dt.datetime | None = None, rng: random.Random | None = None,
              horizon_days: int = 7, exclude_job_id: int | None = None) -> _dt.datetime | None:
    """Find the next randomised slot that satisfies every cadence rule.

    Randomisation is the point: a fixed 09:00/13:00/19:00 cadence is a
    fingerprint. We walk forward in jittered steps and return the first
    candidate that passes :func:`check_slot`.
    """
    cfg = settings.platform(platform)
    rng = rng or random.Random()
    cursor = (after or clock.now_utc()) + _dt.timedelta(minutes=rng.randint(1, 15))
    deadline = cursor + _dt.timedelta(days=horizon_days)

    while cursor < deadline:
        ok, _ = check_slot(conn, settings, platform, account, cursor,
                           exclude_job_id=exclude_job_id)
        if ok:
            return cursor
        step = max(5, cfg.min_interval_minutes or 30)
        jitter = rng.randint(-cfg.jitter_minutes, cfg.jitter_minutes) if cfg.jitter_minutes else 0
        cursor += _dt.timedelta(minutes=max(5, step + jitter))
        local = clock.to_local(cursor, settings.timezone)
        lo, hi = cfg.active_hours
        if local.hour >= hi:  # jump to the next morning rather than stepping through the night
            nxt = (local + _dt.timedelta(days=1)).replace(
                hour=lo, minute=rng.randint(0, 59), second=0, microsecond=0
            )
            cursor = nxt.astimezone(clock.UTC)
        elif local.hour < lo:
            nxt = local.replace(hour=lo, minute=rng.randint(0, 59), second=0, microsecond=0)
            cursor = nxt.astimezone(clock.UTC)
    return None


def warn_if_near_limit(conn: sqlite3.Connection, settings: Settings) -> list[QuotaState]:
    ratio = settings.anomaly.quota_warn_ratio
    hot = []
    for st in all_states(conn, settings):
        if st.daily_limit and st.used_today / st.daily_limit >= ratio:
            hot.append(st)
    return hot


def platform_of(settings: Settings, name: str) -> PlatformConfig:
    return settings.platform(name)
