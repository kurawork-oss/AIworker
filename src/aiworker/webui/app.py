"""Optional approval / dashboard web UI.

A phone-shaped operations app, server-rendered. No JavaScript framework and no
build step: it is small enough that a toolchain would cost more than it saves,
and it must run with `pip install fastapi uvicorn` and nothing else.

Five ways to work the same queue -- list, pipeline, one-at-a-time, dashboard,
conversation -- all of them going through `approval.service`, so the guardrail
checks, the audit record and the override rules do not vary by screen.

**No authentication.** It binds to 127.0.0.1 and refuses a public interface
without ``AIWORKER_ALLOW_PUBLIC_BIND=1``, because a queue anyone on the network
can approve from is not an approval gate. Use an SSH tunnel, or a reverse proxy
that authenticates, for remote access.
"""

from __future__ import annotations

import os
from urllib.parse import quote

# Imported at module scope rather than inside create_app(): this module uses
# `from __future__ import annotations`, so FastAPI resolves route annotations
# against module globals. A function-local import leaves `Request`
# unresolvable and every form POST fails with a 422.
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..approval import service as approval
from ..core import clock, db
from ..core.config import load_settings
from ..core.models import Status
from ..guard import killswitch
from . import views
from .components import (
    badges, channel_label, e, is_blocked, page, status_label,
)


def _decision_card(item, blocked: bool, next_url: str = "") -> str:
    """The approve / revise / reject controls.

    On a blocked item both the ordering and the styling change. The guardrails
    already refused this content, so the prominent actions are the ones that
    respect that refusal, and the override sits in a separate fenced-off block.
    Making "ignore the guardrail" the big green button would be an approval
    gate that argues for its own bypass.
    """
    nxt = f'<input type="hidden" name="next" value="{e(next_url)}">' if next_url else ""
    revise = f"""
<form class="action-block" method="post" action="/item/{item.id}/revise">{nxt}
  <label class="field"><span>修正内容（必須）</span>
    <input type="text" name="note" placeholder="例: 冒頭を具体的な数字に変える"></label>
  <button class="{'brand' if blocked else ''}" type="submit">修正を依頼</button>
</form>"""
    reject = f"""
<form class="action-block" method="post" action="/item/{item.id}/reject">{nxt}
  <label class="field"><span>却下理由（任意）</span>
    <input type="text" name="note" placeholder="例: テーマが直近と重複"></label>
  <button class="reject" type="submit">却下する</button>
</form>"""

    if not blocked:
        approve = f"""
<form class="action-block" method="post" action="/item/{item.id}/approve">{nxt}
  <label class="field"><span>メモ（任意）</span>
    <input type="text" name="note" placeholder="承認時のメモ"></label>
  <button class="approve" type="submit">承認する</button>
</form>"""
        return f'<div class="card"><div class="section" style="margin-top:0">判断</div>' \
               f'{approve}{revise}{reject}</div>'

    override = f"""
<div class="danger-zone">
  <div class="meta">⚠ この項目はガードレールがブロックしています。
  上書き承認は <b>approve_override</b> として、理由つきで監査ログに残ります。
  上の判定を読んだうえで、それでも問題ないと判断できる場合だけ使ってください。</div>
  <form method="post" action="/item/{item.id}/approve" style="margin-top:10px">{nxt}
    <label class="field"><span>上書きする理由（必須）</span>
      <input type="text" name="note" placeholder="なぜこの指摘が当てはまらないのか"></label>
    <button class="override" type="submit">理由を記録して上書き承認</button>
  </form>
</div>"""
    return ('<div class="card"><div class="section" style="margin-top:0">判断</div>'
            '<div class="notice meta">ガードレールがブロックした項目です。'
            'まず修正依頼か却下を検討してください。</div>'
            f'{revise}{reject}{override}</div>')


def detail_body(conn, settings, item, msg: str = "") -> str:
    findings = "".join(
        f'<div class="meta">{"🛑" if f["severity"] == "blocking" else "⚠"} '
        f'{e(f["detail"])}</div>'
        for f in item.policy_report.get("findings", [])
    ) + "".join(
        f'<div class="meta">{"🛑" if i["fatal"] else "⚠"} {e(i["detail"])}</div>'
        for i in item.quality_report.get("issues", [])
    ) or '<div class="meta">指摘なし</div>'

    meta_rows = "".join(
        f'<div class="barrow" style="grid-template-columns:104px 1fr">'
        f'<span class="muted">{e(k)}</span><span class="meta">{e(str(v))[:300]}</span></div>'
        for k, v in sorted(item.meta.items()) if k != "schema_keys"
    ) or '<div class="meta">なし</div>'

    history = "".join(
        f'<div class="barrow" style="grid-template-columns:104px 1fr">'
        f'<span class="muted">'
        f'{e(clock.fmt_local(clock.parse_iso(h["created_at"]), settings.timezone, "%m/%d %H:%M"))}'
        f'</span><span class="meta">{e(h["action"])} — {e(h["actor"])} '
        f'{e(h["note"])}</span></div>'
        for h in db.approval_history(conn, item.id)
    )

    blocked = is_blocked(item)
    return f"""
<div class="card">
  <div class="row wrap">{badges(item)}</div>
  <h3 style="margin:10px 0 4px">{e(item.title or item.uid)}</h3>
  <div class="muted">#{item.id} {e(item.uid)} · {e(channel_label(item.channel))} ·
    {e(item.platform)}／{e(item.account)}</div>
  <div class="muted">{e(item.theme)} / {e(item.angle)} / {e(item.tone)}</div>
  <pre style="margin-top:12px">{e(item.body)}</pre>
</div>

<div class="card">
  <div class="section" style="margin-top:0">ガードレール判定</div>
  <div class="muted" style="margin-bottom:6px">類似度 {item.similarity:.0%}
    （最も近い項目: {e(item.quality_report.get("nearest_uid") or "なし")}）</div>
  {findings}
</div>

<div class="card"><div class="section" style="margin-top:0">メタデータ</div>{meta_rows}</div>
{_decision_card(item, blocked)}
{f'<div class="card"><div class="section" style="margin-top:0">履歴</div>{history}</div>'
 if history else ""}
<div class="card"><a class="btn ghost" href="/">← キューに戻る</a></div>"""


def create_app(config: str | None = None):
    settings = load_settings(config)
    app = FastAPI(title=f"AIworker 承認UI", docs_url=None, redoc_url=None)

    def conn():
        return db.init_db(settings.db_path)

    def actor(request: Request) -> str:
        return os.environ.get("AIWORKER_ACTOR") or (request.client.host if request.client
                                                    else "webui")

    def halts(c):
        return killswitch.active_halts(c, settings.state_dir)

    def redirect(target: str, msg: str = "") -> RedirectResponse:
        sep = "&" if "?" in target else "?"
        url = f"{target}{sep}msg={quote(msg)}" if msg else target
        return RedirectResponse(url, status_code=303)

    # ---- task views ------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(status: str = "", channel: str = "", msg: str = ""):
        c = conn()
        try:
            return HTMLResponse(page("承認キュー",
                                     views.list_view(c, settings, status=status,
                                                     channel=channel),
                                     settings, tab="tasks", switch_path="/",
                                     halts=halts(c), toast=msg))
        finally:
            c.close()

    @app.get("/flow", response_class=HTMLResponse)
    def flow(msg: str = ""):
        c = conn()
        try:
            return HTMLResponse(page("フロー", views.flow_view(c, settings), settings,
                                     tab="tasks", switch_path="/flow", halts=halts(c),
                                     toast=msg))
        finally:
            c.close()

    @app.get("/swipe", response_class=HTMLResponse)
    def swipe(index: int = 0, msg: str = ""):
        c = conn()
        try:
            return HTMLResponse(page("スワイプ", views.swipe_view(c, settings, index=index),
                                     settings, tab="tasks", switch_path="/swipe",
                                     halts=halts(c), toast=msg))
        finally:
            c.close()

    @app.get("/chat", response_class=HTMLResponse)
    def chat(msg: str = ""):
        c = conn()
        try:
            return HTMLResponse(page("対話", views.chat_view(c, settings), settings,
                                     tab="tasks", switch_path="/chat", halts=halts(c),
                                     toast=msg))
        finally:
            c.close()

    # ---- reports & settings ---------------------------------------------
    @app.get("/reports", response_class=HTMLResponse)
    def reports(days: int = 7, msg: str = ""):
        c = conn()
        try:
            return HTMLResponse(page("レポート", views.reports_view(c, settings, days=days),
                                     settings, tab="reports", halts=halts(c), toast=msg))
        finally:
            c.close()

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(msg: str = ""):
        c = conn()
        try:
            return HTMLResponse(page("設定", views.settings_view(c, settings, halts(c)),
                                     settings, tab="settings", halts=halts(c), toast=msg))
        finally:
            c.close()

    # ---- item detail -----------------------------------------------------
    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def detail(item_id: int, msg: str = ""):
        c = conn()
        try:
            item = db.get_item(c, item_id)
            if not item:
                return HTMLResponse(
                    page("見つかりません", '<div class="card empty">この項目は存在しません</div>',
                         settings, tab="tasks"), status_code=404)
            return HTMLResponse(page(item.uid, detail_body(c, settings, item), settings,
                                     tab="tasks", halts=halts(c), toast=msg))
        finally:
            c.close()

    # ---- actions ---------------------------------------------------------
    @app.post("/item/{item_id}/approve")
    def do_approve(item_id: int, request: Request, note: str = Form(""),
                   next: str = Form("")):
        c = conn()
        try:
            item = db.get_item(c, item_id)
            blocked = bool(item and is_blocked(item))
            decision = approval.approve(c, settings, item_id, actor=actor(request),
                                        note=note, force=blocked and bool(note.strip()))
            msg = "✅ 承認しました" if decision.ok else f"⚠ {decision.message}"
            return redirect(next or f"/item/{item_id}", msg)
        finally:
            c.close()

    @app.post("/item/{item_id}/reject")
    def do_reject(item_id: int, request: Request, note: str = Form(""),
                  next: str = Form("")):
        c = conn()
        try:
            decision = approval.reject(c, item_id, actor=actor(request), note=note)
            msg = "却下しました" if decision.ok else f"⚠ {decision.message}"
            return redirect(next or "/", msg)
        finally:
            c.close()

    @app.post("/item/{item_id}/revise")
    def do_revise(item_id: int, request: Request, note: str = Form(""),
                  next: str = Form("")):
        c = conn()
        try:
            decision = approval.request_revision(c, item_id, actor=actor(request), note=note)
            msg = "修正依頼を記録しました" if decision.ok else f"⚠ {decision.message}"
            return redirect(next or f"/item/{item_id}", msg)
        finally:
            c.close()

    @app.post("/stop")
    def do_stop(request: Request, reason: str = Form("")):
        c = conn()
        try:
            if not reason.strip():
                return redirect("/settings", "⚠ 停止には理由が必要です")
            killswitch.engage(c, settings.state_dir, reason=reason.strip(),
                              actor=actor(request))
            return redirect("/settings", "🛑 停止しました")
        finally:
            c.close()

    @app.post("/resume")
    def do_resume(request: Request):
        """Release every halt.

        Deliberately not per-platform here: a phone screen is the wrong place
        for a partial release, and the CLI (`aiworker resume --platform x`)
        exists for that. Whoever presses this is asserting the cause is fixed.
        """
        c = conn()
        try:
            for h in killswitch.active_halts(c, settings.state_dir):
                killswitch.release(c, settings.state_dir, actor=actor(request),
                                   platform=None if h.scope == "global" else h.scope)
            return redirect("/settings", "✅ 停止を解除しました")
        finally:
            c.close()

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "dry_run": settings.dry_run}

    return app


def run(host: str = "127.0.0.1", port: int = 8787, config: str | None = None) -> int:
    import uvicorn

    public = host not in {"127.0.0.1", "localhost", "::1"}
    if public and os.environ.get("AIWORKER_ALLOW_PUBLIC_BIND") != "1":
        print(
            f"refusing to bind to {host}: this UI has no authentication, and anything "
            "that can reach it can approve content.\n"
            "Use an SSH tunnel (ssh -L 8787:127.0.0.1:8787 <host>) or set "
            "AIWORKER_ALLOW_PUBLIC_BIND=1 if it is already behind an authenticating proxy."
        )
        return 2
    print(f"approval UI: http://{host}:{port}  (Ctrl-C to stop)")
    uvicorn.run(create_app(config), host=host, port=port, log_level="warning")
    return 0
