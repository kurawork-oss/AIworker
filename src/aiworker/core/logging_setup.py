"""Structured file logging with a 30-day retention sweep.

Logs go to ``var/logs/aiworker-YYYY-MM-DD.log`` as one JSON object per line, so
`jq` works on them and a day's file can be dropped wholesale once it ages out.
The database ``events`` table holds the same audit trail in queryable form; the
files exist so that a crash before the DB write still leaves a trace.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from pathlib import Path

_CONFIGURED = False


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("platform", "content_id", "job_id", "category", "detail"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def purge_old_logs(log_dir: Path, retention_days: int) -> list[Path]:
    """Delete log files older than the retention window. Returns what was removed."""
    if not log_dir.is_dir():
        return []
    cutoff = _dt.date.today() - _dt.timedelta(days=retention_days)
    removed: list[Path] = []
    for f in log_dir.glob("aiworker-*.log"):
        stamp = f.stem.removeprefix("aiworker-")
        try:
            day = _dt.date.fromisoformat(stamp)
        except ValueError:
            continue
        if day < cutoff:
            f.unlink(missing_ok=True)
            removed.append(f)
    return removed


def setup_logging(log_dir: Path, *, retention_days: int = 30, level: str = "INFO",
                  quiet: bool = False) -> logging.Logger:
    global _CONFIGURED
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("aiworker")
    if _CONFIGURED:
        return root
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    today = _dt.date.today().isoformat()
    fh = logging.FileHandler(log_dir / f"aiworker-{today}.log", encoding="utf-8")
    fh.setFormatter(JsonLineFormatter())
    root.addHandler(fh)

    if not quiet:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)

    purge_old_logs(log_dir, retention_days)
    _CONFIGURED = True
    return root


def get_logger(name: str = "aiworker") -> logging.Logger:
    return logging.getLogger(name if name.startswith("aiworker") else f"aiworker.{name}")
