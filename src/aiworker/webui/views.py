"""The five approval views plus the report and settings screens.

Each view is a different way into the *same* queue and the *same* approval
service -- they are presentation, not policy. Whichever screen an operator
uses, the guardrail checks, the audit record and the override rules are
identical, because all of them go through `approval.service`.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections import defaultdict

from ..approval import service as approval
from ..core import clock, db
from ..core.config import Settings
from ..core.models import JobStatus, Status
from ..guard import quota
from ..revenue import report as reporting
from .components import (
    area_chart, badges, bar_rows, channel_label, e, icon, is_blocked, item_card,
    status_label,
)

REVIEWABLE = [Status.PENDING_REVIEW.value, Status.BLOCKED.value, Status.NEEDS_REVISION.value]

FILTERS = [
    ("", "すべて"),
    (Status.PENDING_REVIEW.value, "承認待ち"),
    (Status.BLOCKED.value, "ブロック"),
    (Status.NEEDS_REVISION.value, "修正依頼"),
]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return approval.stats(conn)


def _filter_chips(active: str, c: dict[str, int]) -> str:
    chips = []
    for value, label in FILTERS:
        n = sum(c.get(s, 0) for s in REVIEWABLE) if not value else c.get(value, 0)
        current = ' aria-current="page"' if value == active else ""
        href = f"/?status={value}" if value else "/"
        chips.append(f'<a class="chip" href="{href}"{current}>{label}'
                     f'<span class="n">{n}</span></a>')
    return f'<div class="chips">{"".join(chips)}</div>'


# --------------------------------------------------------------------------
# UI 1 -- list
# --------------------------------------------------------------------------
def list_view(conn: sqlite3.Connection, settings: Settings, *, status: str = "",
              channel: str = "") -> str:
    c = counts(conn)
    items = approval.queue(conn, status=[status] if status else REVIEWABLE,
                           channel=channel or None, limit=100)
    body = [_filter_chips(status, c)]
    if channel:
        body.append(
            f'<div class="card row"><span class="grow meta">'
            f'カテゴリー: <b>{e(channel_label(channel))}</b> で絞り込み中</span>'
            f'<a class="btn ghost" style="width:auto" href="/">解除</a></div>'
        )
    if not items:
        body.append('<div class="card empty">対象はありません。<br>'
                    '<span class="muted">生成すると、ここに承認待ちが並びます。</span></div>')
        return "".join(body)

    groups: dict[str, list] = defaultdict(list)
    for it in items:
        groups[it.status].append(it)
    order = [Status.BLOCKED.value, Status.PENDING_REVIEW.value, Status.NEEDS_REVISION.value]
    headings = {
        Status.BLOCKED.value: "要判断（ガードレールがブロック）",
        Status.PENDING_REVIEW.value: "承認待ち",
        Status.NEEDS_REVISION.value: "修正依頼中",
    }
    for key in order:
        if not groups.get(key):
            continue
        body.append(f'<div class="section">{headings[key]} · {len(groups[key])}件</div>')
        body += [item_card(it, settings) for it in groups[key]]
    return "".join(body)


# --------------------------------------------------------------------------
# UI 2 -- process flow
# --------------------------------------------------------------------------
def flow_view(conn: sqlite3.Connection, settings: Settings) -> str:
    """Where everything currently is, laid out as the pipeline itself.

    Not a review screen -- a "what is the system doing" screen. The human
    column is visually separated because it is the only stage that cannot
    advance on its own.
    """
    c = counts(conn)
    pending = c.get(Status.PENDING_REVIEW.value, 0)
    blocked = c.get(Status.BLOCKED.value, 0)
    revision = c.get(Status.NEEDS_REVISION.value, 0)
    approved = c.get(Status.APPROVED.value, 0)
    scheduled = c.get(Status.SCHEDULED.value, 0)
    published = c.get(Status.PUBLISHED.value, 0)
    failed = c.get(Status.FAILED.value, 0)
    rejected = c.get(Status.REJECTED.value, 0)
    generated = sum(c.values())

    return f"""
<div class="card">
  <div class="section" style="margin-top:0">パイプラインの現在地</div>
  <div class="flow">
    <div class="flowcol">
      <h4>生成</h4>
      <div class="step on"><span class="n">{generated}</span>累計</div>
      <div class="arrow">↓</div>
      <div class="step">自動<br>多様性制御</div>
    </div>
    <div class="flowcol">
      <h4>AI処理・自動判定</h4>
      <div class="step">規約チェック</div>
      <div class="arrow">↓</div>
      <div class="step">重複・品質チェック</div>
      <div class="arrow">↓</div>
      <div class="step on"><span class="n">{blocked}</span>ブロック</div>
    </div>
    <div class="flowcol human">
      <h4>人間の承認</h4>
      <div class="step act"><span class="n">{pending}</span>承認待ち</div>
      <div class="step"><span class="n">{revision}</span>修正依頼</div>
      <div class="arrow">↓</div>
      <div class="step"><span class="n">{rejected}</span>却下</div>
    </div>
  </div>
  <p class="muted" style="margin:10px 2px 0">
    人間の承認を通らない限り、右側の工程には進みません。
  </p>
</div>

<div class="section">承認のあと</div>
<div class="card">
  <div class="flow">
    <div class="flowcol">
      <h4>枠の割当</h4>
      <div class="step on"><span class="n">{approved}</span>承認済</div>
      <div class="arrow">↓</div>
      <div class="step">時刻をランダム分散</div>
    </div>
    <div class="flowcol">
      <h4>公開直前の再チェック</h4>
      <div class="step on"><span class="n">{scheduled}</span>予約済</div>
      <div class="arrow">↓</div>
      <div class="step">停止 / 枠 / 承認状態</div>
    </div>
    <div class="flowcol">
      <h4>公開</h4>
      <div class="step act"><span class="n">{published}</span>公開済</div>
      <div class="step"><span class="n">{failed}</span>失敗</div>
    </div>
  </div>
</div>

<div class="card">
  <div class="meta">いま人間がやることは <b>{pending + blocked}件</b> です。</div>
  <div class="actions" style="border:0;padding-top:10px">
    <a class="btn brand" href="/">リストで見る</a>
    <a class="btn" href="/swipe">続けて処理する</a>
  </div>
</div>"""


# --------------------------------------------------------------------------
# UI 3 -- swipe / one at a time
# --------------------------------------------------------------------------
def swipe_view(conn: sqlite3.Connection, settings: Settings, *, index: int = 0) -> str:
    """One card at a time, for clearing a backlog quickly.

    Blocked items are deliberately **not** in this deck. Fast one-tap approval
    is exactly the wrong affordance for content a guardrail refused; those are
    listed separately with a link into the detail view, where the reasons are
    on screen. Speed is for the routine items.
    """
    queue = [i for i in approval.queue(conn, status=[Status.PENDING_REVIEW.value], limit=100)]
    blocked = approval.queue(conn, status=[Status.BLOCKED.value], limit=100)

    blocked_note = ""
    if blocked:
        blocked_note = (
            f'<div class="card"><div class="notice meta">'
            f'ブロック {len(blocked)}件はスワイプ対象から外しています。'
            f'理由を読んだうえで個別に判断してください。</div>'
            f'<a class="btn brand" href="/?status={Status.BLOCKED.value}">'
            f'ブロックを確認する</a></div>'
        )

    if not queue:
        return ('<div class="card empty">承認待ちはありません 🎉</div>' + blocked_note)

    index = max(0, min(index, len(queue) - 1))
    item = queue[index]
    dots = "".join(
        f'<span class="dot{" on" if i == index else ""}"></span>'
        for i in range(min(len(queue), 8))
    )
    meta_rows = "".join(
        f'<div class="barrow" style="grid-template-columns:96px 1fr">'
        f'<span class="muted">{e(k)}</span><span class="meta">{e(str(v))[:120]}</span></div>'
        for k, v in sorted(item.meta.items())
        if k not in {"schema_keys", "generator"}
    )
    return f"""{blocked_note}
<div class="deck">
  <div class="counter">残り {len(queue)}件 · {index + 1} / {len(queue)}</div>
  <article class="card">
    <div class="row wrap">{badges(item)}</div>
    <h3 style="margin:10px 0 4px">{e(item.title or item.uid)}</h3>
    <div class="muted">{e(channel_label(item.channel))} · {e(item.platform)} ·
      {e(item.theme)} / {e(item.angle)}</div>
    <pre style="margin-top:12px">{e(item.body)}</pre>
    {f'<div style="margin-top:12px">{meta_rows}</div>' if meta_rows else ""}
  </article>

  <div class="swipe-actions">
    <form method="post" action="/item/{item.id}/approve">
      <input type="hidden" name="next" value="/swipe?index={index}">
      <button class="approve lg" type="submit">承認</button>
    </form>
    <form method="post" action="/item/{item.id}/reject">
      <input type="hidden" name="next" value="/swipe?index={index}">
      <button class="reject lg" type="submit">却下</button>
    </form>
    <a class="btn lg" href="/item/{item.id}">詳細</a>
  </div>

  <div class="actions" style="border:0;padding-top:10px">
    <a class="btn ghost" href="/swipe?index={max(0, index - 1)}">← 前へ</a>
    <a class="btn ghost" href="/swipe?index={index + 1}">次へ →</a>
  </div>
  <div class="dots">{dots}</div>
</div>"""


# --------------------------------------------------------------------------
# UI 5 -- dashboard / by category
# --------------------------------------------------------------------------
def reports_view(conn: sqlite3.Connection, settings: Settings, *, days: int = 7) -> str:
    c = counts(conn)
    pending = c.get(Status.PENDING_REVIEW.value, 0)
    blocked = c.get(Status.BLOCKED.value, 0)
    today = clock.to_local(clock.now_utc(), settings.timezone).date()
    period = reporting.Period.last_days(days, today=today, label=f"直近{days}日")
    rev = reporting.revenue_report(conn, period)

    series = []
    cursor = _dt.date.fromisoformat(period.start)
    end = _dt.date.fromisoformat(period.end)
    while cursor <= end:
        key = cursor.isoformat()
        series.append((key, rev.by_date.get(key, 0.0)))
        cursor += _dt.timedelta(days=1)

    live = sum(1 for cfg in settings.platforms.values() if cfg.enabled)
    published_7d = conn.execute(
        "SELECT COUNT(*) n FROM publish_jobs WHERE status=? AND published_at >= ?",
        (JobStatus.DONE.value, clock.to_iso(clock.now_utc() - _dt.timedelta(days=days))),
    ).fetchone()["n"]

    by_channel = conn.execute(
        "SELECT channel, COUNT(*) n FROM content_items WHERE status IN (?,?,?) "
        "GROUP BY channel ORDER BY n DESC",
        tuple(REVIEWABLE),
    ).fetchall()
    cats = "".join(
        f'<a class="cat" href="/?status=">'
        f'<h4>{e(channel_label(r["channel"]))}</h4>'
        f'<div class="n">{r["n"]}</div><div class="muted">件 待機中</div></a>'
        for r in by_channel
    ) or '<div class="cat"><div class="muted">待機中の項目はありません</div></div>'

    concentration = ""
    if rev.total and max(rev.by_source.values()) / rev.total >= 0.7:
        top = max(rev.by_source, key=rev.by_source.get)
        share = max(rev.by_source.values()) / rev.total * 100
        concentration = (f'<div class="notice meta" style="margin-top:10px">'
                         f'⚠ 収益の{share:.0f}%が「{e(top)}」に集中しています。'
                         f'1つ止まると全体が止まります。</div>')

    ops = reporting.ops_report(conn, settings, days=days)

    return f"""
<div class="tiles">
  <div class="tile"><div class="k">稼働中の系統</div><div class="v">{live}</div></div>
  <div class="tile"><div class="k">直近{days}日の収益</div>
    <div class="v">¥{rev.total:,.0f}</div></div>
</div>

<section class="hero">
  <div class="k">承認待ち</div>
  <div class="v">{pending + blocked}</div>
  <div class="legend">承認待ち <b>{pending}</b> ／ 要判断 <b>{blocked}</b></div>
  <div class="cta">
    <a class="btn solid" href="/?status={Status.BLOCKED.value}">要判断から見る</a>
    <a class="btn" href="/">キューへ</a>
  </div>
</section>

<div class="card">
  <figure>
    <figcaption>日次の収益推移</figcaption>
    <div class="figsub">{e(period.start)} 〜 {e(period.end)} ／ 合計 ¥{rev.total:,.0f}</div>
    {area_chart(series)}
  </figure>
</div>

<div class="card">
  <figure>
    <figcaption>収益源別</figcaption>
    <div class="figsub">1つに偏っていないかを見る指標です</div>
    {bar_rows(sorted(rev.by_source.items(), key=lambda kv: -kv[1]))}
  </figure>
  {concentration}
</div>

<div class="section">カテゴリー別の待機件数</div>
<div class="catgrid">{cats}</div>

<div class="section">公開実績</div>
<div class="card"><div class="meta">直近{days}日で <b>{published_7d}件</b> 公開しました。</div></div>

<div class="card">
  <details>
    <summary class="meta" style="cursor:pointer">運用レポート全文（テキスト）</summary>
    <p class="muted" style="margin:8px 0">通知に送られるのと同じ内容です。
      横長の表なので、横向きか広い画面で読むと崩れません。</p>
    <pre style="overflow-x:auto">{e(ops)}</pre>
  </details>
</div>"""


# --------------------------------------------------------------------------
# chat view
# --------------------------------------------------------------------------
def chat_view(conn: sqlite3.Connection, settings: Settings) -> str:
    """The queue as a conversation.

    Same actions, same service, different framing: each waiting item is a
    message with its actions inline, newest last, so it reads like catching up
    on a thread. Useful when the queue is short and you want context rather
    than a grid.
    """
    items = approval.queue(conn, status=REVIEWABLE, limit=30)
    if not items:
        return ('<div class="msg"><span class="who">AI</span>'
                '<div class="bubble">承認待ちはありません。次の生成までお待ちください。</div></div>')

    out = []
    last_day = ""
    for item in reversed(items):
        day = clock.fmt_local(clock.parse_iso(item.created_at), settings.timezone, "%m月%d日")
        if day != last_day:
            out.append(f'<div class="daysep">{day}</div>')
            last_day = day
        blocked = is_blocked(item)
        if blocked:
            reasons = [f["detail"] for f in item.policy_report.get("findings", [])
                       if f["severity"] == "blocking"]
            reasons += [i["detail"] for i in item.quality_report.get("issues", []) if i["fatal"]]
            lead = ("🛑 <b>" + e(channel_label(item.channel)) + "の下書きを作りましたが、"
                    "ガードレールが止めました。</b>")
            detail = "<br>".join("・" + e(r) for r in reasons[:3])
            actions = (f'<a class="btn brand" href="/item/{item.id}">理由を読んで判断</a>'
                       f'<form method="post" action="/item/{item.id}/reject">'
                       f'<input type="hidden" name="next" value="/chat">'
                       f'<button class="reject" type="submit">却下</button></form>')
        else:
            lead = (e(channel_label(item.channel)) + "の下書きができました。確認をお願いします。"
                    + f'<div class="meta" style="margin-top:6px"><b>{e(item.title)}</b></div>')
            detail = f'<span class="muted">{e(item.body_preview(90))}</span>'
            actions = (f'<form method="post" action="/item/{item.id}/approve">'
                       f'<input type="hidden" name="next" value="/chat">'
                       f'<button class="approve" type="submit">承認</button></form>'
                       f'<a class="btn" href="/item/{item.id}">下書きを見る</a>'
                       f'<form method="post" action="/item/{item.id}/reject">'
                       f'<input type="hidden" name="next" value="/chat">'
                       f'<button class="reject" type="submit">却下</button></form>')
        out.append(f"""<div class="msg"><span class="who">AI</span>
<div class="bubble">{lead}<div style="margin-top:6px">{detail}</div>
<div class="inline-actions">{actions}</div></div></div>""")

        if item.status == Status.NEEDS_REVISION.value and item.revision_note:
            out.append(f'<div class="msg me"><span class="who">私</span>'
                       f'<div class="bubble">{e(item.revision_note)}</div></div>')
    return "".join(out)


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
def settings_view(conn: sqlite3.Connection, settings: Settings, halts: list) -> str:
    rows = "".join(
        f'<div class="barrow" style="grid-template-columns:96px 1fr auto">'
        f'<span class="meta">{e(s.platform)}</span>'
        f'<span class="track"><span class="fill" style="width:'
        f'{min(100, (s.used_today / s.daily_limit * 100) if s.daily_limit else 0):.0f}%"></span></span>'
        f'<span class="val">本日 {s.used_today}/{s.daily_limit}</span></div>'
        for s in quota.all_states(conn, settings)
    )
    if halts:
        halt_card = (
            '<div class="card"><div class="section" style="margin-top:0">停止中</div>'
            + "".join(f'<div class="meta">🛑 {e(h.describe())}</div>' for h in halts)
            + '<form method="post" action="/resume" style="margin-top:12px">'
              '<button class="brand" type="submit">停止を解除する</button></form>'
              '<p class="muted">原因が解消したことを確認してから解除してください。</p></div>'
        )
    else:
        halt_card = (
            '<div class="card"><div class="section" style="margin-top:0">緊急停止</div>'
            '<p class="meta">押すと、承認済みのものも含めて公開が止まります。'
            '解除は人間の操作が必要です。</p>'
            '<form method="post" action="/stop">'
            '<label class="field"><span>停止の理由（必須）</span>'
            '<input type="text" name="reason" placeholder="例: リーチが急減している"></label>'
            '<button class="reject" type="submit">すべて停止する</button></form>'
            '<p class="muted">端末から直接止める場合: <code>touch var/STOP</code></p></div>'
        )

    return f"""
{halt_card}
<div class="section">投稿枠の消化</div>
<div class="card">{rows}</div>
<div class="section">設定</div>
<div class="card">
  <div class="barrow" style="grid-template-columns:120px 1fr">
    <span class="muted">タイムゾーン</span><span class="meta">{e(settings.timezone)}</span></div>
  <div class="barrow" style="grid-template-columns:120px 1fr">
    <span class="muted">公開モード</span>
    <span class="meta">{"dry-run（外部送信なし）" if settings.dry_run else "本番"}</span></div>
  <div class="barrow" style="grid-template-columns:120px 1fr">
    <span class="muted">承認ゲート</span><span class="meta">必須（無効化できません）</span></div>
  <div class="barrow" style="grid-template-columns:120px 1fr">
    <span class="muted">生成</span>
    <span class="meta">{e(settings.generation.provider)}</span></div>
  <div class="barrow" style="grid-template-columns:120px 1fr">
    <span class="muted">設定ファイル</span>
    <span class="meta">{e(settings.source_path)}</span></div>
  <p class="muted" style="margin-top:10px">
    値の変更は config/config.yaml を編集してください。この画面からは変更できません。</p>
</div>"""
