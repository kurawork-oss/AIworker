"""Anomaly detection and automatic halt.

The detectors answer the four questions that precede an account getting
restricted:

1. Are posts failing repeatedly? (API rejections cluster before a suspension.)
2. Did reach fall off a cliff? (The classic shadowban signature.)
3. Did a platform say something that looks like a warning?
4. Are we about to run into our own daily cap?

(1) and (3) can halt the pipeline on their own. Everything else alerts a human
and leaves the decision to them -- an auto-halt on a noisy signal is its own
kind of outage.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

from ..core import clock, db
from ..core.config import Settings
from ..core.models import Severity
from ..notify.notifier import Alert, Notifier
from . import killswitch, quota


@dataclass
class Anomaly:
    code: str
    platform: str
    severity: Severity
    message: str
    halt: bool = False

    def as_dict(self) -> dict:
        return {
            "code": self.code, "platform": self.platform,
            "severity": self.severity.value, "message": self.message, "halt": self.halt,
        }


def detect_consecutive_failures(conn: sqlite3.Connection, settings: Settings) -> list[Anomaly]:
    out = []
    threshold = settings.anomaly.consecutive_failure_threshold
    for name in settings.platforms:
        n = db.consecutive_failures(conn, name)
        if n >= threshold:
            out.append(Anomaly(
                "consecutive_failures", name, Severity.CRITICAL,
                f"{n} consecutive publish failures (threshold {threshold})",
                halt=settings.anomaly.auto_halt_on_failures,
            ))
    return out


def detect_reach_drop(conn: sqlite3.Connection, settings: Settings) -> list[Anomaly]:
    """Latest day's reach vs the median of the preceding days.

    Median rather than mean: one viral post should not raise the bar so high
    that every normal day afterwards looks like a collapse.
    """
    out = []
    cfg = settings.anomaly
    for name in settings.platforms:
        series = db.daily_reach(conn, name, limit=cfg.reach_min_samples + 1)
        if len(series) < cfg.reach_min_samples + 1:
            continue
        *history, (latest_day, latest) = series
        baseline = statistics.median(v for _, v in history)
        if baseline <= 0:
            continue
        ratio = latest / baseline
        if ratio < cfg.reach_drop_ratio:
            out.append(Anomaly(
                "reach_drop", name, Severity.ERROR,
                f"reach on {latest_day} was {latest:.0f} vs median {baseline:.0f} "
                f"({ratio:.0%} of baseline; alert below {cfg.reach_drop_ratio:.0%})",
            ))
    return out


def detect_platform_warnings(conn: sqlite3.Connection, settings: Settings,
                             *, lookback: int = 100) -> list[Anomaly]:
    """Scan recent job errors and events for language that reads like a
    platform warning (suspension, strike, restriction...)."""
    out = []
    keywords = [k.lower() for k in settings.anomaly.warning_keywords]
    rows = conn.execute(
        "SELECT platform, last_error FROM publish_jobs WHERE last_error != '' "
        "ORDER BY id DESC LIMIT ?", (lookback,)
    ).fetchall()
    rows = list(rows) + [
        {"platform": r["platform"], "last_error": r["message"]}
        for r in db.recent_events(conn, limit=lookback, min_level="warning")
    ]
    seen: set[tuple[str, str]] = set()
    for r in rows:
        text = (r["last_error"] or "").lower()
        hit = next((k for k in keywords if k in text), None)
        if hit and (r["platform"], hit) not in seen:
            seen.add((r["platform"], hit))
            out.append(Anomaly(
                "platform_warning", r["platform"] or "", Severity.CRITICAL,
                f"platform response mentions '{hit}': {(r['last_error'] or '')[:200]}",
                halt=True,
            ))
    return out


def detect_quota_pressure(conn: sqlite3.Connection, settings: Settings) -> list[Anomaly]:
    out = []
    for st in quota.warn_if_near_limit(conn, settings):
        out.append(Anomaly(
            "quota_pressure", st.platform, Severity.WARNING,
            f"{st.summary()} -- at {settings.anomaly.quota_warn_ratio:.0%} of the daily cap",
        ))
    return out


def scan(conn: sqlite3.Connection, settings: Settings) -> list[Anomaly]:
    found: list[Anomaly] = []
    found += detect_consecutive_failures(conn, settings)
    found += detect_platform_warnings(conn, settings)
    found += detect_reach_drop(conn, settings)
    found += detect_quota_pressure(conn, settings)
    return found


def react(conn: sqlite3.Connection, settings: Settings, anomalies: list[Anomaly],
          notifier: Notifier, *, state_dir: Path | None = None,
          auto_halt: bool = True) -> list[Anomaly]:
    """Log every anomaly, alert on it, and engage the kill switch where the
    detector asked for one."""
    state_dir = state_dir or settings.state_dir
    for a in anomalies:
        db.log_event(conn, a.severity, "anomaly", a.message, platform=a.platform,
                     payload=a.as_dict())
        notifier.send(Alert(a.severity, f"anomaly: {a.code}", a.message, a.platform))
        if a.halt and auto_halt:
            current = killswitch.check(conn, state_dir, a.platform)
            if not current.halted:
                killswitch.engage(
                    conn, state_dir, reason=f"auto-halt: {a.code} - {a.message}",
                    actor="anomaly-detector", platform=a.platform or None,
                )
                notifier.send(Alert(
                    Severity.CRITICAL, f"AUTO-HALT: {a.platform or 'global'}",
                    f"publishing stopped automatically.\nreason: {a.message}\n"
                    f"resume with: aiworker resume --platform {a.platform}",
                    a.platform,
                ))
    return anomalies


def last_scan_at(conn: sqlite3.Connection) -> str | None:
    return db.kv_get(conn, "anomaly:last_scan_at")


def mark_scanned(conn: sqlite3.Connection) -> None:
    db.kv_set(conn, "anomaly:last_scan_at", clock.to_iso(clock.now_utc()))
