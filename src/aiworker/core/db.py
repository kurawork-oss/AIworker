"""SQLite storage layer.

Why SQLite: it is a single file, it ships with Python, it survives a crash mid
write, and it gives us real transactions -- which matters because "approved"
and "published" must never disagree. Swapping in Postgres later only means
rewriting this module.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import clock
from .models import ContentItem, JobStatus, PublishJob, Severity, Status

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    uid            TEXT NOT NULL UNIQUE,
    channel        TEXT NOT NULL,
    platform       TEXT NOT NULL,
    account        TEXT NOT NULL DEFAULT 'main',
    theme          TEXT NOT NULL DEFAULT '',
    angle          TEXT NOT NULL DEFAULT '',
    tone           TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    meta           TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,
    dedupe_hash    TEXT NOT NULL DEFAULT '',
    similarity     REAL NOT NULL DEFAULT 0,
    quality_report TEXT NOT NULL DEFAULT '{}',
    policy_report  TEXT NOT NULL DEFAULT '{}',
    revision_note  TEXT NOT NULL DEFAULT '',
    run_id         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    approved_at    TEXT,
    approved_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_status   ON content_items(status);
CREATE INDEX IF NOT EXISTS idx_content_platform ON content_items(platform, status);
CREATE INDEX IF NOT EXISTS idx_content_channel  ON content_items(channel, created_at);

CREATE TABLE IF NOT EXISTS publish_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id   INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
    platform     TEXT NOT NULL,
    account      TEXT NOT NULL DEFAULT 'main',
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT '',
    external_id  TEXT NOT NULL DEFAULT '',
    external_url TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due    ON publish_jobs(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_jobs_plat   ON publish_jobs(platform, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_content_open
    ON publish_jobs(content_id) WHERE status IN ('queued', 'running', 'blocked');

CREATE TABLE IF NOT EXISTS approvals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_content ON approvals(content_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,
    category   TEXT NOT NULL,
    platform   TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts  ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_cat ON events(category, ts);

CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    platform    TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT 'main',
    external_id TEXT NOT NULL DEFAULT '',
    impressions INTEGER NOT NULL DEFAULT 0,
    reach       INTEGER NOT NULL DEFAULT 0,
    clicks      INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics(platform, date);

CREATE TABLE IF NOT EXISTS revenue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    source      TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT 'main',
    amount      REAL NOT NULL DEFAULT 0,
    currency    TEXT NOT NULL DEFAULT 'JPY',
    units       INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(date, source, account, note)
);
CREATE INDEX IF NOT EXISTS idx_revenue_date ON revenue(date);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# key/value  (kill switch, cursors, counters)
# --------------------------------------------------------------------------
def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, clock.to_iso(clock.now_utc())),
    )


def kv_delete(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM kv WHERE key=?", (key,))


def kv_prefix(conn: sqlite3.Connection, prefix: str) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)).fetchall()
    return {r["key"]: r["value"] for r in rows}


# --------------------------------------------------------------------------
# events  (the audit trail; also what the alerting layer reads)
# --------------------------------------------------------------------------
def log_event(
    conn: sqlite3.Connection,
    level: Severity | str,
    category: str,
    message: str,
    *,
    platform: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    level_s = level.value if isinstance(level, Severity) else str(level)
    cur = conn.execute(
        "INSERT INTO events(ts, level, category, platform, message, payload) VALUES(?,?,?,?,?,?)",
        (
            clock.to_iso(clock.now_utc()),
            level_s,
            category,
            platform,
            message,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid)


def recent_events(
    conn: sqlite3.Connection, *, limit: int = 50, category: str | None = None,
    min_level: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: list[Any] = []
    if category:
        sql += " AND category=?"
        args.append(category)
    if min_level:
        allowed = [s.value for s in Severity if s.rank >= Severity(min_level).rank]
        sql += f" AND level IN ({','.join('?' * len(allowed))})"
        args.extend(allowed)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def purge_old_events(conn: sqlite3.Connection, retention_days: int) -> int:
    import datetime as _dt

    cutoff = clock.to_iso(clock.now_utc() - _dt.timedelta(days=retention_days))
    cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    return cur.rowcount or 0


# --------------------------------------------------------------------------
# content items
# --------------------------------------------------------------------------
def _row_to_item(row: sqlite3.Row) -> ContentItem:
    return ContentItem(
        id=row["id"], uid=row["uid"], channel=row["channel"], platform=row["platform"],
        account=row["account"], theme=row["theme"], angle=row["angle"], tone=row["tone"],
        title=row["title"], body=row["body"], meta=json.loads(row["meta"] or "{}"),
        status=row["status"], dedupe_hash=row["dedupe_hash"], similarity=row["similarity"],
        quality_report=json.loads(row["quality_report"] or "{}"),
        policy_report=json.loads(row["policy_report"] or "{}"),
        revision_note=row["revision_note"], run_id=row["run_id"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        approved_at=row["approved_at"], approved_by=row["approved_by"],
    )


def insert_item(conn: sqlite3.Connection, item: ContentItem) -> ContentItem:
    now = clock.to_iso(clock.now_utc())
    item.created_at = item.created_at or now
    item.updated_at = now
    cur = conn.execute(
        """INSERT INTO content_items
           (uid, channel, platform, account, theme, angle, tone, title, body, meta,
            status, dedupe_hash, similarity, quality_report, policy_report,
            revision_note, run_id, created_at, updated_at, approved_at, approved_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item.uid, item.channel, item.platform, item.account, item.theme, item.angle,
            item.tone, item.title, item.body, json.dumps(item.meta, ensure_ascii=False),
            item.status, item.dedupe_hash, item.similarity,
            json.dumps(item.quality_report, ensure_ascii=False),
            json.dumps(item.policy_report, ensure_ascii=False),
            item.revision_note, item.run_id, item.created_at, item.updated_at,
            item.approved_at, item.approved_by,
        ),
    )
    item.id = int(cur.lastrowid)
    return item


def get_item(conn: sqlite3.Connection, item_id: int) -> ContentItem | None:
    row = conn.execute("SELECT * FROM content_items WHERE id=?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def get_item_by_uid(conn: sqlite3.Connection, uid: str) -> ContentItem | None:
    row = conn.execute("SELECT * FROM content_items WHERE uid=?", (uid,)).fetchone()
    return _row_to_item(row) if row else None


def list_items(
    conn: sqlite3.Connection,
    *,
    status: str | list[str] | None = None,
    platform: str | None = None,
    channel: str | None = None,
    limit: int = 100,
    order: str = "ASC",
) -> list[ContentItem]:
    sql = "SELECT * FROM content_items WHERE 1=1"
    args: list[Any] = []
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if channel:
        sql += " AND channel=?"
        args.append(channel)
    sql += f" ORDER BY id {'DESC' if order.upper() == 'DESC' else 'ASC'} LIMIT ?"
    args.append(limit)
    return [_row_to_item(r) for r in conn.execute(sql, args).fetchall()]


def update_item_status(
    conn: sqlite3.Connection,
    item_id: int,
    status: Status | str,
    *,
    approved_by: str | None = None,
    revision_note: str | None = None,
) -> None:
    status_s = status.value if isinstance(status, Status) else str(status)
    now = clock.to_iso(clock.now_utc())
    sets = ["status=?", "updated_at=?"]
    args: list[Any] = [status_s, now]
    if status_s == Status.APPROVED.value:
        sets += ["approved_at=?", "approved_by=?"]
        args += [now, approved_by or "unknown"]
    if revision_note is not None:
        sets.append("revision_note=?")
        args.append(revision_note)
    args.append(item_id)
    conn.execute(f"UPDATE content_items SET {', '.join(sets)} WHERE id=?", args)


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) n FROM content_items GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def record_approval(
    conn: sqlite3.Connection, content_id: int, action: str, actor: str, note: str = ""
) -> None:
    conn.execute(
        "INSERT INTO approvals(content_id, action, actor, note, created_at) VALUES(?,?,?,?,?)",
        (content_id, action, actor, note, clock.to_iso(clock.now_utc())),
    )


def approval_history(conn: sqlite3.Connection, content_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM approvals WHERE content_id=? ORDER BY id", (content_id,)
    ).fetchall()


# --------------------------------------------------------------------------
# publish jobs
# --------------------------------------------------------------------------
def _row_to_job(row: sqlite3.Row) -> PublishJob:
    return PublishJob(
        id=row["id"], content_id=row["content_id"], platform=row["platform"],
        account=row["account"], scheduled_at=row["scheduled_at"], status=row["status"],
        attempts=row["attempts"], last_error=row["last_error"], external_id=row["external_id"],
        external_url=row["external_url"], published_at=row["published_at"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def insert_job(conn: sqlite3.Connection, job: PublishJob) -> PublishJob:
    now = clock.to_iso(clock.now_utc())
    job.created_at = job.created_at or now
    job.updated_at = now
    cur = conn.execute(
        """INSERT INTO publish_jobs
           (content_id, platform, account, scheduled_at, status, attempts, last_error,
            external_id, external_url, published_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job.content_id, job.platform, job.account, job.scheduled_at, job.status,
            job.attempts, job.last_error, job.external_id, job.external_url,
            job.published_at, job.created_at, job.updated_at,
        ),
    )
    job.id = int(cur.lastrowid)
    return job


def get_job(conn: sqlite3.Connection, job_id: int) -> PublishJob | None:
    row = conn.execute("SELECT * FROM publish_jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def open_job_for_content(conn: sqlite3.Connection, content_id: int) -> PublishJob | None:
    row = conn.execute(
        "SELECT * FROM publish_jobs WHERE content_id=? AND status IN "
        "('queued','running','blocked') ORDER BY id DESC LIMIT 1",
        (content_id,),
    ).fetchone()
    return _row_to_job(row) if row else None


def due_jobs(conn: sqlite3.Connection, *, now_iso: str, platform: str | None = None,
             limit: int = 50) -> list[PublishJob]:
    sql = ("SELECT * FROM publish_jobs WHERE status IN ('queued','blocked') "
           "AND scheduled_at <= ?")
    args: list[Any] = [now_iso]
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    sql += " ORDER BY scheduled_at ASC, id ASC LIMIT ?"
    args.append(limit)
    return [_row_to_job(r) for r in conn.execute(sql, args).fetchall()]


def list_jobs(conn: sqlite3.Connection, *, status: str | list[str] | None = None,
              platform: str | None = None, limit: int = 100) -> list[PublishJob]:
    sql = "SELECT * FROM publish_jobs WHERE 1=1"
    args: list[Any] = []
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        args.extend(statuses)
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    sql += " ORDER BY scheduled_at ASC LIMIT ?"
    args.append(limit)
    return [_row_to_job(r) for r in conn.execute(sql, args).fetchall()]


def update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "scheduled_at", "status", "attempts", "last_error", "external_id",
        "external_url", "published_at",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update publish_jobs columns: {sorted(bad)}")
    sets = ", ".join(f"{k}=?" for k in fields)
    args = list(fields.values())
    args += [clock.to_iso(clock.now_utc()), job_id]
    conn.execute(f"UPDATE publish_jobs SET {sets}, updated_at=? WHERE id=?", args)


def published_timestamps(
    conn: sqlite3.Connection, platform: str, account: str, *, since_iso: str
) -> list[str]:
    """Timestamps of slots that have been spent.

    STAGED counts as well as DONE. A staged item has had its posting slot
    allocated and a file written for a person to post; releasing the slot
    because nobody has confirmed yet would let the next run schedule over it.
    Over-counting a slot costs one post; under-counting it costs an account.
    """
    rows = conn.execute(
        "SELECT published_at FROM publish_jobs WHERE platform=? AND account=? "
        "AND status IN (?,?) AND published_at IS NOT NULL AND published_at >= ? "
        "ORDER BY published_at",
        (platform, account, JobStatus.DONE.value, JobStatus.STAGED.value, since_iso),
    ).fetchall()
    return [r["published_at"] for r in rows]


def scheduled_timestamps(
    conn: sqlite3.Connection, platform: str, account: str, *, since_iso: str,
    exclude_job_id: int | None = None,
) -> list[str]:
    """Future slots already handed out -- they count against the quota too,
    otherwise `plan` would happily schedule 50 posts for the same day.

    ``exclude_job_id`` leaves out the job currently being executed: its own
    reservation must not make it look like it is crowding itself.
    """
    rows = conn.execute(
        "SELECT scheduled_at FROM publish_jobs WHERE platform=? AND account=? "
        "AND status IN ('queued','running','blocked') AND scheduled_at >= ? "
        "AND id != COALESCE(?, -1) ORDER BY scheduled_at",
        (platform, account, since_iso, exclude_job_id),
    ).fetchall()
    return [r["scheduled_at"] for r in rows]


def consecutive_failures(conn: sqlite3.Connection, platform: str, *, window: int = 10) -> int:
    rows = conn.execute(
        "SELECT status FROM publish_jobs WHERE platform=? AND status IN ('done','failed') "
        "ORDER BY updated_at DESC LIMIT ?",
        (platform, window),
    ).fetchall()
    count = 0
    for r in rows:
        if r["status"] == JobStatus.FAILED.value:
            count += 1
        else:
            break
    return count


# --------------------------------------------------------------------------
# metrics & revenue
# --------------------------------------------------------------------------
def insert_metric(conn: sqlite3.Connection, *, date: str, platform: str, account: str = "main",
                  external_id: str = "", impressions: int = 0, reach: int = 0,
                  clicks: int = 0, note: str = "") -> None:
    conn.execute(
        "INSERT INTO metrics(date, platform, account, external_id, impressions, reach, "
        "clicks, note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (date, platform, account, external_id, impressions, reach, clicks, note,
         clock.to_iso(clock.now_utc())),
    )


def daily_reach(conn: sqlite3.Connection, platform: str, *, limit: int = 30) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT date, SUM(reach) r FROM metrics WHERE platform=? GROUP BY date "
        "ORDER BY date DESC LIMIT ?",
        (platform, limit),
    ).fetchall()
    return [(r["date"], float(r["r"] or 0)) for r in reversed(rows)]


def upsert_revenue(conn: sqlite3.Connection, *, date: str, source: str, amount: float,
                   account: str = "main", currency: str = "JPY", units: int = 0,
                   note: str = "") -> None:
    conn.execute(
        "INSERT INTO revenue(date, source, account, amount, currency, units, note, created_at) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date, source, account, note) DO UPDATE SET "
        "amount=excluded.amount, units=excluded.units, currency=excluded.currency",
        (date, source, account, amount, currency, units, note,
         clock.to_iso(clock.now_utc())),
    )


def revenue_between(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM revenue WHERE date >= ? AND date <= ? ORDER BY date", (start, end)
    ).fetchall()


def staged_jobs(conn: sqlite3.Connection, *, limit: int = 100) -> list[PublishJob]:
    """Jobs whose content is written to the outbox and waiting on a person."""
    rows = conn.execute(
        "SELECT * FROM publish_jobs WHERE status=? ORDER BY published_at LIMIT ?",
        (JobStatus.STAGED.value, limit),
    ).fetchall()
    return [_row_to_job(r) for r in rows]
