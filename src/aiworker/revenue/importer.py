"""Revenue ingestion.

Every source here is CSV or manual entry, and that is a deliberate choice
rather than a gap: A8.net has no public affiliate-reporting API, note has no
sales API, and stock agencies vary. Scraping a console behind a login to get
these numbers would put the *accounts* at risk to populate a *dashboard* --
exactly the trade this system exists to refuse.

So: download the CSV, drop it in, import it. Two minutes a week, no account
risk. Sources with a real API (YouTube Analytics) can get an adapter later
without changing anything downstream.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..core import db
from ..core.logging_setup import get_logger
from ..core.models import Severity

log = get_logger("revenue")

#: Column aliases seen in the wild, normalised to our field names.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "日付", "発生日", "成果発生日", "確定日", "day"),
    "amount": ("amount", "報酬額", "報酬", "金額", "確定報酬", "売上", "earnings", "revenue"),
    "units": ("units", "件数", "成果件数", "販売数", "sales", "count", "quantity"),
    "note": ("note", "プログラム名", "商品名", "備考", "program", "title", "description"),
    "currency": ("currency", "通貨"),
}


@dataclass
class ImportResult:
    source: str
    rows: int = 0
    imported: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.source}: {self.imported}/{self.rows}行を取り込みました"
                + (f"、{len(self.skipped)}行スキップ" if self.skipped else ""))


def _pick(header: list[str], field_name: str) -> str | None:
    lowered = {h.strip().lower(): h for h in header}
    for alias in COLUMN_ALIASES[field_name]:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    return None


def _to_float(raw: str) -> float:
    cleaned = (raw or "").replace(",", "").replace("¥", "").replace("円", "").strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        raise ValueError(f"数値として読めません: {raw!r}") from None


def _to_date(raw: str) -> str:
    """Normalise the date formats these consoles actually emit."""
    import datetime as _dt

    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(raw[:len(fmt) + 4], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return _dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        raise ValueError(f"日付として読めません: {raw!r}") from None


def import_csv(conn: sqlite3.Connection, path: Path, *, source: str,
               account: str = "main", currency: str = "JPY",
               encoding: str = "utf-8-sig") -> ImportResult:
    """Import a revenue CSV. Re-importing the same file is safe -- rows are
    upserted on (date, source, account, note)."""
    result = ImportResult(source=source)
    path = Path(path)
    if not path.exists():
        result.skipped.append(f"ファイルが見つかりません: {path}")
        return result

    try:
        text = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp932")  # Japanese consoles love Shift-JIS

    reader = csv.DictReader(text.splitlines())
    header = reader.fieldnames or []
    date_col = _pick(header, "date")
    amount_col = _pick(header, "amount")
    if not date_col or not amount_col:
        result.skipped.append(
            f"日付と金額の列が見つかりません。見つかった列: {header} ／ "
            f"日付として認識する列名: {'、'.join(COLUMN_ALIASES['date'])} ／ "
            f"金額として認識する列名: {'、'.join(COLUMN_ALIASES['amount'])}"
        )
        return result
    units_col = _pick(header, "units")
    note_col = _pick(header, "note")
    currency_col = _pick(header, "currency")

    with db.transaction(conn):
        for i, row in enumerate(reader, start=2):
            result.rows += 1
            try:
                date = _to_date(row.get(date_col, ""))
                amount = _to_float(row.get(amount_col, ""))
            except ValueError as exc:
                result.skipped.append(f"{i}行目: {exc}")
                continue
            units = int(_to_float(row.get(units_col, "0"))) if units_col else 0
            note = (row.get(note_col, "") or "").strip() if note_col else ""
            cur = (row.get(currency_col, "") or currency).strip() if currency_col else currency
            db.upsert_revenue(conn, date=date, source=source, account=account, amount=amount,
                              currency=cur, units=units, note=note[:120])
            result.imported += 1
        db.log_event(conn, Severity.INFO, "revenue", result.summary(),
                     payload={"path": str(path), "source": source})
    log.info("%s", result.summary())
    return result


def record_manual(conn: sqlite3.Connection, *, date: str, source: str, amount: float,
                  account: str = "main", currency: str = "JPY", units: int = 0,
                  note: str = "") -> str:
    with db.transaction(conn):
        db.upsert_revenue(conn, date=_to_date(date), source=source, account=account,
                          amount=amount, currency=currency, units=units, note=note)
        db.log_event(conn, Severity.INFO, "revenue",
                     f"manual entry: {source} {amount}{currency} on {date}")
    return f"recorded {amount:.0f}{currency} for {source} on {date}"
