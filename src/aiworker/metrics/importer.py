"""Analytics ingestion from the CSVs the platform consoles export.

Why CSV and not an API, again: the read-only analytics APIs (X API v2,
YouTube Analytics) are the *safe* half of platform automation and are worth
adding later -- but they need credentials, a review process and an OAuth
dance, and none of that is a prerequisite for the thing this unblocks. The
export button is one click and works today.

Why this matters more than it looks: the reach-drop detector in
`guard/anomaly.py` is the only guardrail that can notice a shadowban, and
until now its data path was "a human types numbers in by hand". That is the
kind of task nobody does twice, so in practice the detector was reading an
empty table and reporting nothing wrong.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..core import db
from ..core.logging_setup import get_logger
from ..core.models import Severity

log = get_logger("metrics")

#: Header aliases seen in real exports, normalised to our fields.
#: X/Twitter Analytics, YouTube Studio, Meta/Threads Insights, note.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": (
        "date", "日付", "day", "時間", "期間", "投稿日", "公開日",
    ),
    "impressions": (
        "impressions", "インプレッション", "インプレッション数", "表示回数",
        "views", "視聴回数", "ビュー", "全体ビュー", "再生回数", "表示",
    ),
    "reach": (
        "reach", "リーチ", "リーチ数", "ユニークユーザー", "unique viewers",
        "accounts reached", "リーチしたアカウント数",
    ),
    "clicks": (
        "clicks", "クリック", "クリック数", "url clicks", "リンクのクリック数",
        "engagements", "エンゲージメント", "エンゲージメント数",
    ),
    "external_id": (
        "external_id", "tweet id", "post id", "id", "動画", "content",
        "tweet permalink", "投稿id", "パーマリンク", "url",
    ),
    "note": ("note", "備考", "タイトル", "title", "動画のタイトル", "投稿"),
}

#: Which measure to store as `reach`.
REACH_SOURCES = ("auto", "reach", "impressions")


@dataclass
class MetricsImport:
    platform: str
    rows: int = 0
    imported: int = 0
    dates: set[str] = field(default_factory=set)
    used_impressions_as_reach: bool = False
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        span = ""
        if self.dates:
            span = f"（{min(self.dates)} 〜 {max(self.dates)}）"
        note = ""
        if self.used_impressions_as_reach:
            note = "  ※リーチ列がないため表示回数を使用"
        return (f"{self.platform}: {self.imported}/{self.rows}行を取り込みました{span}"
                + (f"、{len(self.skipped)}行スキップ" if self.skipped else "") + note)


def _pick(header: list[str], field_name: str) -> str | None:
    lowered = {h.strip().lower(): h for h in header if h}
    for alias in COLUMN_ALIASES[field_name]:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    # Some consoles prefix or suffix the measure ("Impressions (total)").
    for alias in COLUMN_ALIASES[field_name]:
        for key, original in lowered.items():
            if alias.lower() in key:
                return original
    return None


def _to_int(raw: str) -> int:
    cleaned = (raw or "").replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"-", "—", "N/A"}:
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        raise ValueError(f"数値として読めません: {raw!r}") from None


def _to_date(raw: str) -> str:
    import datetime as _dt

    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%d/%m/%Y", "%m/%d/%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _dt.datetime.strptime(raw[:len(fmt) + 4], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return _dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        raise ValueError(f"日付として読めません: {raw!r}") from None


def import_csv(conn: sqlite3.Connection, path: Path, *, platform: str,
               account: str = "main", reach_from: str = "auto",
               encoding: str = "utf-8-sig") -> MetricsImport:
    """Import an analytics export. Re-importing the same file is safe."""
    if reach_from not in REACH_SOURCES:
        raise ValueError(f"reach_from は {REACH_SOURCES} のいずれか: {reach_from!r}")

    result = MetricsImport(platform=platform)
    path = Path(path)
    if not path.exists():
        result.skipped.append(f"ファイルが見つかりません: {path}")
        return result

    try:
        text = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp932")  # Japanese consoles emit Shift-JIS

    reader = csv.DictReader(text.splitlines())
    header = reader.fieldnames or []
    date_col = _pick(header, "date")
    impressions_col = _pick(header, "impressions")
    reach_col = _pick(header, "reach")

    if not date_col:
        result.skipped.append(
            f"日付の列が見つかりません。見つかった列: {header}"
        )
        return result
    if not impressions_col and not reach_col:
        result.skipped.append(
            "リーチまたは表示回数の列が見つかりません。"
            f"見つかった列: {header}"
        )
        return result

    clicks_col = _pick(header, "clicks")
    id_col = _pick(header, "external_id")
    note_col = _pick(header, "note")

    with db.transaction(conn):
        for lineno, row in enumerate(reader, start=2):
            result.rows += 1
            try:
                date = _to_date(row.get(date_col, ""))
                impressions = _to_int(row.get(impressions_col, "")) if impressions_col else 0
                reach = _to_int(row.get(reach_col, "")) if reach_col else 0
            except ValueError as exc:
                result.skipped.append(f"{lineno}行目: {exc}")
                continue

            if reach_from == "impressions":
                reach = impressions
            elif reach_from == "reach":
                pass
            elif not reach and impressions:
                # `daily_reach` already falls back, but recording it here makes
                # the substitution visible in the import summary rather than
                # something the operator has to infer from a zero column.
                result.used_impressions_as_reach = True

            db.upsert_metric(
                conn, date=date, platform=platform, account=account,
                external_id=(row.get(id_col, "") or "").strip()[:120] if id_col else "",
                impressions=impressions, reach=reach,
                clicks=_to_int(row.get(clicks_col, "")) if clicks_col else 0,
                note=(row.get(note_col, "") or "").strip()[:120] if note_col else "",
            )
            result.imported += 1
            result.dates.add(date)

        db.log_event(conn, Severity.INFO, "metrics", result.summary(),
                     platform=platform, payload={"path": str(path)})
    log.info("%s", result.summary())
    return result
