"""Emergency stop.

Two independent mechanisms, because the one that matters must work even when
the other is broken:

1. A **stop file** (``var/STOP`` or ``var/STOP.<platform>``). Works when the DB
   is locked, corrupt, or the process cannot open it. `touch var/STOP` from any
   shell halts publishing.
2. A **kv flag** in the database, set by ``aiworker stop`` and by the anomaly
   detector when it auto-halts.

Either one being present blocks publishing. Both must be cleared to resume, and
clearing is always a deliberate human action -- nothing in the system lifts its
own halt.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..core import clock, db
from ..core.models import Severity

GLOBAL_KEY = "killswitch:global"
PLATFORM_KEY = "killswitch:platform:{}"


@dataclass
class HaltState:
    halted: bool
    scope: str = ""
    reason: str = ""
    source: str = ""
    since: str = ""

    def describe(self) -> str:
        if not self.halted:
            return "running"
        return f"HALTED [{self.scope}] via {self.source}: {self.reason or '(no reason given)'}"


def stop_file(state_dir: Path, platform: str | None = None) -> Path:
    return Path(state_dir) / (f"STOP.{platform}" if platform else "STOP")


def _file_reason(path: Path) -> str:
    """First line of a stop file is the reason; the rest is provenance."""
    text = path.read_text(encoding="utf-8").strip()
    return text.splitlines()[0].strip() if text else "stop file present"


def check(conn: sqlite3.Connection, state_dir: Path, platform: str | None = None) -> HaltState:
    """Return the halt state that applies to ``platform`` (global halts win)."""
    gf = stop_file(state_dir)
    if gf.exists():
        return HaltState(True, "global", _file_reason(gf), "stop-file",
                         clock.to_iso(clock.now_utc()))
    raw = db.kv_get(conn, GLOBAL_KEY)
    if raw:
        reason, _, since = raw.partition("|")
        return HaltState(True, "global", reason, "db", since)
    if platform:
        pf = stop_file(state_dir, platform)
        if pf.exists():
            return HaltState(True, platform, _file_reason(pf), "stop-file",
                             clock.to_iso(clock.now_utc()))
        raw = db.kv_get(conn, PLATFORM_KEY.format(platform))
        if raw:
            reason, _, since = raw.partition("|")
            return HaltState(True, platform, reason, "db", since)
    return HaltState(False)


def engage(conn: sqlite3.Connection, state_dir: Path, *, reason: str, actor: str = "cli",
           platform: str | None = None, write_file: bool = True) -> HaltState:
    now = clock.to_iso(clock.now_utc())
    key = PLATFORM_KEY.format(platform) if platform else GLOBAL_KEY
    db.kv_set(conn, key, f"{reason}|{now}")
    if write_file:
        f = stop_file(state_dir, platform)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{reason}\nengaged_at={now}\nby={actor}\n", encoding="utf-8")
    db.log_event(
        conn, Severity.CRITICAL, "killswitch",
        f"kill switch ENGAGED ({platform or 'global'}): {reason}",
        platform=platform or "", payload={"actor": actor},
    )
    return HaltState(True, platform or "global", reason, "db", now)


def release(conn: sqlite3.Connection, state_dir: Path, *, actor: str = "cli",
            platform: str | None = None) -> None:
    key = PLATFORM_KEY.format(platform) if platform else GLOBAL_KEY
    db.kv_delete(conn, key)
    stop_file(state_dir, platform).unlink(missing_ok=True)
    db.log_event(
        conn, Severity.WARNING, "killswitch",
        f"kill switch released ({platform or 'global'})",
        platform=platform or "", payload={"actor": actor},
    )


def active_halts(conn: sqlite3.Connection, state_dir: Path) -> list[HaltState]:
    out: list[HaltState] = []
    g = check(conn, state_dir)
    if g.halted:
        out.append(g)
    for key, value in db.kv_prefix(conn, "killswitch:platform:").items():
        plat = key.rsplit(":", 1)[-1]
        reason, _, since = value.partition("|")
        out.append(HaltState(True, plat, reason, "db", since))
    for f in Path(state_dir).glob("STOP.*"):
        plat = f.name.split(".", 1)[1]
        if not any(h.scope == plat for h in out):
            out.append(HaltState(True, plat, _file_reason(f), "stop-file", ""))
    return out
