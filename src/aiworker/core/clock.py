"""Time helpers.

Rule of the codebase: **every timestamp is stored in UTC as an ISO-8601 string
with a `+00:00` offset**, and converted to the operator's local timezone only
at display time. Mixing naive local timestamps into the database is the classic
way to get a "daily limit" that silently resets at the wrong hour.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

UTC = _dt.timezone.utc


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(tz=UTC)


def to_iso(dt: _dt.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_iso(value: str) -> _dt.datetime:
    dt = _dt.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def to_local(dt: _dt.datetime, tz_name: str) -> _dt.datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz_name))


def local_date(dt: _dt.datetime, tz_name: str) -> str:
    """The calendar date *in the operator's timezone* -- the unit daily quotas
    are counted in."""
    return to_local(dt, tz_name).date().isoformat()


def fmt_local(dt: _dt.datetime, tz_name: str, pattern: str = "%Y-%m-%d %H:%M") -> str:
    return to_local(dt, tz_name).strftime(pattern)
