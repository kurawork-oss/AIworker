"""Operational and revenue reporting, rendered as text.

Text, not a web chart, because the report has to survive being pasted into
Discord, emailed, and read on a phone. An ASCII bar chart is legible in all
three; a PNG is legible in none of them without extra machinery.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from ..core import clock, db
from ..core.config import Settings
from ..core.models import JobStatus, Status
from ..guard import anomaly, killswitch, quota

BAR = "█"


def sparkbar(value: float, peak: float, width: int = 24) -> str:
    if peak <= 0:
        return ""
    return BAR * max(1 if value > 0 else 0, round(value / peak * width))


@dataclass
class Period:
    start: str
    end: str
    label: str

    @staticmethod
    def last_days(n: int, *, today: _dt.date | None = None, label: str = "") -> "Period":
        today = today or _dt.date.today()
        start = today - _dt.timedelta(days=n - 1)
        return Period(start.isoformat(), today.isoformat(), label or f"last {n} days")


@dataclass
class RevenueReport:
    period: Period
    by_source: dict[str, float] = field(default_factory=dict)
    by_date: dict[str, float] = field(default_factory=dict)
    units: dict[str, int] = field(default_factory=dict)
    currency: str = "JPY"

    @property
    def total(self) -> float:
        return sum(self.by_source.values())

    def render(self) -> str:
        lines = [f"■ 収益サマリー ({self.period.label}: {self.period.start} 〜 {self.period.end})"]
        if not self.by_source:
            lines.append("  データなし。`aiworker revenue import` でCSVを取り込んでください。")
            return "\n".join(lines)
        lines.append(f"  合計: {self.total:,.0f} {self.currency}")
        lines.append("")
        peak = max(self.by_source.values()) if self.by_source else 0
        width = max((len(s) for s in self.by_source), default=8)
        for source, amount in sorted(self.by_source.items(), key=lambda kv: -kv[1]):
            share = amount / self.total * 100 if self.total else 0
            lines.append(
                f"  {source:<{width}}  {amount:>10,.0f}  {share:>5.1f}%  "
                f"{sparkbar(amount, peak, 20)}  ({self.units.get(source, 0)}件)"
            )
        concentration = max(self.by_source.values()) / self.total * 100 if self.total else 0
        if concentration >= 70:
            top = max(self.by_source, key=self.by_source.get)
            lines += ["", f"  ⚠ 収益の{concentration:.0f}%が '{top}' に集中しています。"
                          "1つ止まると全体が止まる状態です。分散を検討してください。"]
        return "\n".join(lines)


def revenue_report(conn: sqlite3.Connection, period: Period) -> RevenueReport:
    report = RevenueReport(period=period)
    for row in db.revenue_between(conn, period.start, period.end):
        report.by_source[row["source"]] = report.by_source.get(row["source"], 0) + row["amount"]
        report.by_date[row["date"]] = report.by_date.get(row["date"], 0) + row["amount"]
        report.units[row["source"]] = report.units.get(row["source"], 0) + row["units"]
        report.currency = row["currency"] or report.currency
    return report


def ops_report(conn: sqlite3.Connection, settings: Settings, *, days: int = 7) -> str:
    """The daily/weekly operations digest: queue depth, publishing, quota
    headroom, errors, halts. Everything a human needs to decide whether to
    intervene, in one screen."""
    today = clock.to_local(clock.now_utc(), settings.timezone).date()
    period = Period.last_days(days, today=today, label=f"直近{days}日")
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(f" AIworker 運用レポート  {today.isoformat()} ({settings.timezone})")
    lines.append("=" * 68)

    halts = killswitch.active_halts(conn, settings.state_dir)
    if halts:
        lines.append("")
        lines.append("🛑 停止中:")
        for h in halts:
            lines.append(f"   - {h.describe()}")
    else:
        lines.append("")
        lines.append("✅ 停止中の系統はありません")

    counts = db.count_by_status(conn)
    lines += ["", "■ 承認キュー"]
    for status, label in [
        (Status.PENDING_REVIEW.value, "承認待ち"),
        (Status.BLOCKED.value, "自動ブロック(要判断)"),
        (Status.NEEDS_REVISION.value, "修正依頼中"),
        (Status.APPROVED.value, "承認済(未スケジュール)"),
        (Status.SCHEDULED.value, "スケジュール済"),
        (Status.PUBLISHED.value, "公開済(累計)"),
        (Status.FAILED.value, "失敗"),
        (Status.REJECTED.value, "却下(累計)"),
    ]:
        lines.append(f"   {label:<22} {counts.get(status, 0):>5}")

    lines += ["", "■ 投稿枠の消化状況"]
    for st in quota.all_states(conn, settings):
        cfg = settings.platform(st.platform)
        flag = ""
        if not cfg.enabled:
            flag = "  (無効)"
        elif st.near_limit:
            flag = "  ⚠ 上限が近い"
        lines.append(
            f"   {st.platform:<14} 本日 {st.used_today}/{st.daily_limit}  "
            f"今週 {st.used_this_week}/{st.weekly_limit}  "
            f"残り {st.capacity}{flag}"
        )

    since = clock.to_iso(clock.now_utc() - _dt.timedelta(days=days))
    published = conn.execute(
        "SELECT platform, COUNT(*) n FROM publish_jobs WHERE status=? AND published_at >= ? "
        "GROUP BY platform", (JobStatus.DONE.value, since),
    ).fetchall()
    failed = conn.execute(
        "SELECT platform, COUNT(*) n FROM publish_jobs WHERE status=? AND updated_at >= ? "
        "GROUP BY platform", (JobStatus.FAILED.value, since),
    ).fetchall()
    lines += ["", f"■ 公開実績 ({period.label})"]
    if published:
        for r in published:
            lines.append(f"   {r['platform']:<14} {r['n']:>4} 件公開")
    else:
        lines.append("   公開なし")
    if failed:
        for r in failed:
            lines.append(f"   {r['platform']:<14} {r['n']:>4} 件失敗  ⚠")

    errors = db.recent_events(conn, limit=8, min_level="error")
    lines += ["", "■ 直近のエラー / 警告"]
    if errors:
        for e in errors:
            ts = clock.fmt_local(clock.parse_iso(e["ts"]), settings.timezone, "%m/%d %H:%M")
            lines.append(f"   {ts} [{e['level']}] {e['message'][:80]}")
    else:
        lines.append("   なし")

    found = anomaly.scan(conn, settings)
    lines += ["", "■ 異常検知"]
    if found:
        for a in found:
            lines.append(f"   [{a.severity.value}] {a.platform or '-'}: {a.message}")
    else:
        lines.append("   検知なし")

    lines += ["", revenue_report(conn, period).render()]

    lines += ["", "■ 次のアクション"]
    todo = []
    if counts.get(Status.PENDING_REVIEW.value):
        todo.append(f"承認待ち {counts[Status.PENDING_REVIEW.value]} 件を確認: aiworker review list")
    if counts.get(Status.BLOCKED.value):
        todo.append(f"ブロック {counts[Status.BLOCKED.value]} 件の判断: aiworker review list --status blocked")
    if counts.get(Status.APPROVED.value):
        todo.append(f"承認済 {counts[Status.APPROVED.value]} 件のスケジュール: aiworker plan")
    if halts:
        todo.append("停止中の系統を確認し、原因解消後に aiworker resume")
    if not todo:
        todo.append("対応不要です。")
    for t in todo:
        lines.append(f"   - {t}")
    lines.append("")
    return "\n".join(lines)


def daily_revenue_chart(conn: sqlite3.Connection, period: Period, *, width: int = 30) -> str:
    rows = db.revenue_between(conn, period.start, period.end)
    by_date: dict[str, float] = defaultdict(float)
    for r in rows:
        by_date[r["date"]] += r["amount"]
    if not by_date:
        return "(データなし)"
    peak = max(by_date.values())
    out = []
    for date in sorted(by_date):
        out.append(f"{date}  {by_date[date]:>9,.0f}  {sparkbar(by_date[date], peak, width)}")
    return "\n".join(out)
