"""Shared HTML pieces: the app shell, tab bar, badges, item cards, charts.

Plain functions returning strings. No template engine and no build step -- the
UI is small enough that a dependency would cost more than it saves, and the
operator can run it with `pip install fastapi uvicorn` and nothing else.
"""

from __future__ import annotations

import html
from typing import Iterable

from ..core import clock
from ..core.config import Settings
from ..core.models import Status
from .theme import stylesheet

APP_NAME = "AIworker"

STATUS_LABELS = {
    Status.PENDING_REVIEW.value: "承認待ち",
    Status.BLOCKED.value: "ブロック",
    Status.NEEDS_REVISION.value: "修正依頼中",
    Status.APPROVED.value: "承認済",
    Status.REJECTED.value: "却下",
    Status.SCHEDULED.value: "予約済",
    Status.PUBLISHED.value: "公開済",
    Status.FAILED.value: "失敗",
}
STATUS_TONE = {
    Status.BLOCKED.value: "bad",
    Status.FAILED.value: "bad",
    Status.NEEDS_REVISION.value: "warn",
    Status.APPROVED.value: "good",
    Status.PUBLISHED.value: "good",
    Status.SCHEDULED.value: "good",
}
CHANNEL_LABELS = {
    "social_post": "SNS投稿",
    "social_thread": "スレッド",
    "note_article": "note記事",
    "shorts_script": "Shorts台本",
    "stock_asset": "ストック素材",
    "digital_product": "デジタル商品",
}

ICONS = {
    "tasks": '<path d="M9 5h10M9 12h10M9 19h10M4 5l1 1 2-2M4 12l1 1 2-2M4 19l1 1 2-2"/>',
    "reports": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "settings": '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M21.5 12h-3'
                'M5.5 12h-3M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1M18.7 18.7l-2.1-2.1'
                'M7.4 7.4L5.3 5.3"/>',
    "doc": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
}

TABS = [
    ("tasks", "/", "タスク"),
    ("reports", "/reports", "レポート"),
    ("settings", "/settings", "設定"),
]

TASK_VIEWS = [
    ("/", "リスト"),
    ("/flow", "フロー"),
    ("/swipe", "スワイプ"),
    ("/chat", "対話"),
]


def e(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def icon(name: str, size: int = 22) -> str:
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{ICONS.get(name, "")}</svg>')


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def channel_label(channel: str) -> str:
    return CHANNEL_LABELS.get(channel, channel)


def badges(item) -> str:
    tone = STATUS_TONE.get(item.status, "")
    out = [f'<span class="badge {tone}">{e(status_label(item.status))}</span>']
    if item.policy_report.get("blocking"):
        out.append('<span class="badge bad flat">規約</span>')
    if not item.quality_report.get("ok", True):
        out.append('<span class="badge warn flat">品質</span>')
    if item.similarity >= 0.6:
        out.append(f'<span class="badge warn flat">類似 {item.similarity:.0%}</span>')
    return "".join(out)


def is_blocked(item) -> bool:
    return bool(item.policy_report.get("blocking")) or not item.quality_report.get("ok", True)


CURRENT = ' aria-current="page"'


def tabbar(active: str) -> str:
    links = "".join(
        f'<a href="{href}"{CURRENT if key == active else ""}>'
        f'{icon(key)}<span>{label}</span></a>'
        for key, href, label in TABS
    )
    return f'<nav class="tabbar">{links}</nav>'


def view_switch(path: str) -> str:
    links = "".join(
        f'<a href="{href}"{CURRENT if href == path else ""}>{label}</a>'
        for href, label in TASK_VIEWS
    )
    return f'<div class="switch">{links}</div>'


def page(title: str, body: str, settings: Settings, *, tab: str = "tasks",
         switch_path: str | None = None, halts: Iterable = (), toast: str = "") -> str:
    halts = list(halts)
    halt_bar = ""
    if halts:
        halt_bar = ('<div class="halt">🛑 停止中 — '
                    + e("; ".join(h.describe() for h in halts)) + "</div>")
    toast_html = f'<div class="toast">{e(toast)}</div>' if toast else ""
    mode = "dry-run" if settings.dry_run else "本番"
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{e(title)} · {APP_NAME}</title><style>{stylesheet()}</style></head><body>
<div class="phone">
  <div class="topbars">
    <header class="appbar">
      <span class="slot"></span>
      <span class="title">{APP_NAME}</span>
      <span class="slot"><span class="avatar" title="{e(mode)}">運用</span></span>
    </header>
    {view_switch(switch_path) if switch_path else ""}
  </div>
  {halt_bar}
  <main>{toast_html}{body}</main>
</div>
{tabbar(tab)}
</body></html>"""


# --------------------------------------------------------------------------
# item cards
# --------------------------------------------------------------------------
def item_card(item, settings: Settings, *, actions: bool = True) -> str:
    """One row in the approval queue.

    A blocked item does not get a one-tap approve here. Approving something a
    guardrail refused is a decision that needs the reasons on screen, so the
    card routes to the detail view instead.
    """
    when = clock.fmt_local(clock.parse_iso(item.created_at), settings.timezone, "%m/%d %H:%M")
    blocked = is_blocked(item)
    buttons = ""
    if actions:
        if blocked:
            buttons = (
                f'<div class="actions">'
                f'<a class="btn brand" href="/item/{item.id}">理由を確認</a>'
                f'<form method="post" action="/item/{item.id}/reject" style="flex:1">'
                f'<button class="reject" type="submit">却下</button></form>'
                f"</div>"
            )
        else:
            buttons = (
                f'<div class="actions">'
                f'<form method="post" action="/item/{item.id}/approve" style="flex:1">'
                f'<button class="approve" type="submit">承認</button></form>'
                f'<form method="post" action="/item/{item.id}/reject" style="flex:1">'
                f'<button class="reject" type="submit">却下</button></form>'
                f'<a class="btn ghost" href="/item/{item.id}">詳細</a>'
                f"</div>"
            )
    return f"""<article class="card">
  <div class="item">
    <span class="thumb">{icon("doc", 18)}</span>
    <div class="grow">
      <h3>{e(item.title or item.preview(40))}</h3>
      <div class="muted">{e(channel_label(item.channel))} · {e(item.platform)} · {when}</div>
      <div class="row wrap" style="margin-top:6px">{badges(item)}</div>
    </div>
  </div>
  <p class="body-preview">{e(item.body_preview(110))}</p>
  {buttons}
</article>"""


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
def area_chart(points: list[tuple[str, float]], *, unit: str = "円",
               width: int = 340, height: int = 110) -> str:
    """Daily trend as a single-series area chart.

    One series, so no legend -- the caption names it. Sequential blue rather
    than a categorical hue, because the job is magnitude over time, not
    identity. Values are labelled at the endpoints only; a number on every
    point is noise at this size.
    """
    if len(points) < 2:
        return '<p class="muted">推移を描くにはデータが足りません（2日分以上必要）</p>'
    values = [v for _, v in points]
    peak = max(values) or 1.0
    pad_l, pad_r, pad_t, pad_b = 6, 6, 10, 18
    iw = width - pad_l - pad_r
    ih = height - pad_t - pad_b
    step = iw / (len(points) - 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        return pad_l + i * step, pad_t + ih - (v / peak) * ih

    coords = [xy(i, v) for i, v in enumerate(values)]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
                    for i, (x, y) in enumerate(coords))
    area = (line + f" L{coords[-1][0]:.1f},{pad_t + ih:.1f} "
                   f"L{coords[0][0]:.1f},{pad_t + ih:.1f} Z")
    grid = "".join(
        f'<line x1="{pad_l}" y1="{pad_t + ih * f:.1f}" x2="{width - pad_r}" '
        f'y2="{pad_t + ih * f:.1f}" stroke="var(--grid)" stroke-width="1"/>'
        for f in (0, 0.5, 1)
    )
    last_x, last_y = coords[-1]
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img"
  aria-label="日次収益の推移。最新 {values[-1]:,.0f}{unit}、期間の最大 {peak:,.0f}{unit}。">
  {grid}
  <path d="{area}" fill="var(--series-fill)"/>
  <path d="{line}" fill="none" stroke="var(--series-1)" stroke-width="2"
        stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="var(--series-1)"
          stroke="var(--card)" stroke-width="2"/>
  <text x="{pad_l}" y="{height - 4}" fill="var(--muted)" font-size="10">{e(points[0][0][5:])}</text>
  <text x="{width - pad_r}" y="{height - 4}" fill="var(--muted)" font-size="10"
        text-anchor="end">{e(points[-1][0][5:])}</text>
</svg>"""


def bar_rows(rows: list[tuple[str, float]], *, unit: str = "円") -> str:
    """Magnitude comparison across a handful of named things.

    Sequential one-hue bars with the value written beside each, so the reading
    never depends on colour.
    """
    if not rows:
        return '<p class="muted">データなし</p>'
    peak = max(v for _, v in rows) or 1.0
    out = []
    for name, value in rows:
        pct = max(2.0, value / peak * 100)
        out.append(
            f'<div class="barrow"><span class="meta">{e(name)}</span>'
            f'<span class="track"><span class="fill" style="width:{pct:.1f}%"></span></span>'
            f'<span class="val">{value:,.0f}{unit}</span></div>'
        )
    return "".join(out)
