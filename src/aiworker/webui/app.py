"""Optional approval / dashboard web UI.

Server-rendered HTML, no JavaScript framework, no build step. The whole point
of this UI is to let a person clear the approval queue from a phone in a few
minutes; a single-page app would add a toolchain and a deploy story for no gain
at that size.

**No authentication.** It binds to 127.0.0.1 and refuses to listen on a public
interface without ``AIWORKER_ALLOW_PUBLIC_BIND=1``, because a queue that anyone
on the network can approve from is not an approval gate. Put it behind an SSH
tunnel or a reverse proxy with auth if you need remote access.
"""

from __future__ import annotations

import html
import os
from urllib.parse import quote

# Imported at module scope rather than inside create_app(): this module uses
# `from __future__ import annotations`, so FastAPI resolves route annotations
# against module globals. A function-local import leaves `Request` unresolvable
# and every form POST fails with a 422.
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..approval import service as approval
from ..core import clock, db
from ..core.config import Settings, load_settings
from ..core.models import Status
from ..guard import killswitch, quota
from ..revenue import report as reporting

STYLE = """
:root{--bg:#fbfbfa;--fg:#1c1b1a;--muted:#6b6862;--line:#e3e1dc;--accent:#2f6f4f;
--warn:#8a5a00;--bad:#a3342a;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#171716;--fg:#ececea;--muted:#9a968e;
--line:#32312e;--accent:#6fbf8f;--warn:#d9a441;--bad:#e4796c;--card:#1f1f1d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,
"Hiragino Sans","Noto Sans JP",sans-serif}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);
padding:12px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header a{color:var(--fg);text-decoration:none;font-weight:600}
header .halt{color:var(--bad);font-weight:700}
main{max-width:860px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px;margin-bottom:12px}
.meta{color:var(--muted);font-size:13px}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;
border:1px solid var(--line);margin-right:6px}
.badge.bad{color:var(--bad);border-color:var(--bad)}
.badge.warn{color:var(--warn);border-color:var(--warn)}
.badge.ok{color:var(--accent);border-color:var(--accent)}
pre{white-space:pre-wrap;word-break:break-word;font:13px/1.6 ui-monospace,monospace;
background:transparent;margin:8px 0}
button{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{border-color:var(--bad);color:var(--bad)}
form.inline{display:inline}
input[type=text]{font:inherit;padding:7px;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--fg);width:100%;max-width:420px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
a.item{color:inherit;text-decoration:none;display:block}
a.item:hover{opacity:.75}
"""


def _page(title: str, body: str, settings: Settings, halts: list) -> str:
    halt_html = ""
    if halts:
        halt_html = '<span class="halt">🛑 ' + html.escape(
            "; ".join(h.describe() for h in halts)
        ) + "</span>"
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>
<header><a href="/">承認キュー</a><a href="/dashboard">ダッシュボード</a>
<span class="meta">{html.escape(settings.timezone)}{' · dry-run' if settings.dry_run else ''}</span>
{halt_html}</header><main>{body}</main></body></html>"""


def _badges(item) -> str:
    out = [f'<span class="badge">{html.escape(item.status)}</span>']
    if item.policy_report.get("blocking"):
        out.append('<span class="badge bad">POLICY</span>')
    if not item.quality_report.get("ok", True):
        out.append('<span class="badge warn">QUALITY</span>')
    if item.similarity >= 0.6:
        out.append(f'<span class="badge warn">類似 {item.similarity:.0%}</span>')
    return "".join(out)


def create_app(config: str | None = None):
    settings = load_settings(config)
    app = FastAPI(title="AIworker approval", docs_url=None, redoc_url=None)

    def conn():
        return db.init_db(settings.db_path)

    def actor(request: Request) -> str:
        return os.environ.get("AIWORKER_ACTOR") or request.client.host or "webui"

    @app.get("/", response_class=HTMLResponse)
    def index(status: str = "", platform: str = ""):
        c = conn()
        try:
            items = approval.queue(
                c, status=[status] if status else None, platform=platform or None, limit=100
            )
            halts = killswitch.active_halts(c, settings.state_dir)
            counts = approval.stats(c)
            head = (
                '<div class="card"><b>キュー</b><div class="meta">'
                f"承認待ち {counts['pending_review']} ／ ブロック {counts['blocked']} ／ "
                f"修正依頼 {counts['needs_revision']} ／ 承認済 {counts['approved']} ／ "
                f"予約 {counts['scheduled']}</div>"
                '<div class="row">'
                '<a href="/"><button>すべて</button></a>'
                f'<a href="/?status={Status.PENDING_REVIEW.value}"><button>承認待ち</button></a>'
                f'<a href="/?status={Status.BLOCKED.value}"><button>ブロック</button></a>'
                f'<a href="/?status={Status.NEEDS_REVISION.value}"><button>修正依頼</button></a>'
                "</div></div>"
            )
            cards = []
            for it in items:
                created = clock.fmt_local(clock.parse_iso(it.created_at), settings.timezone)
                cards.append(
                    f'<a class="item card" href="/item/{it.id}">{_badges(it)}'
                    f'<div class="meta">#{it.id} {html.escape(it.uid)} · '
                    f"{html.escape(it.platform)} · {html.escape(it.channel)} · {created}</div>"
                    f"<div>{html.escape(it.preview(90))}</div></a>"
                )
            body = head + ("".join(cards) or '<div class="card">対象はありません</div>')
            return HTMLResponse(_page("承認キュー", body, settings, halts))
        finally:
            c.close()

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def detail(item_id: int, msg: str = ""):
        c = conn()
        try:
            item = db.get_item(c, item_id)
            if not item:
                return HTMLResponse(_page("404", '<div class="card">見つかりません</div>',
                                          settings, []), status_code=404)
            findings = "".join(
                f'<div class="meta">policy [{html.escape(f["severity"])}] '
                f'{html.escape(f["detail"])}</div>'
                for f in item.policy_report.get("findings", [])
            ) + "".join(
                f'<div class="meta">quality [{"fatal" if i["fatal"] else "warn"}] '
                f'{html.escape(i["detail"])}</div>'
                for i in item.quality_report.get("issues", [])
            ) or '<div class="meta">指摘なし</div>'
            meta_rows = "".join(
                f'<div class="meta">{html.escape(str(k))}: {html.escape(str(v))[:300]}</div>'
                for k, v in sorted(item.meta.items()) if k not in {"schema_keys"}
            )
            blocked = item.policy_report.get("blocking") or not item.quality_report.get("ok", True)
            approve_label = "承認する" if not blocked else "ブロックを上書きして承認"
            # A silent no-op is the worst failure mode for an approval gate: the
            # reviewer thinks they approved something they did not.
            banner = (f'<div class="card"><b>{html.escape(msg)}</b></div>') if msg else ""
            body = banner + f"""
<div class="card">{_badges(item)}
<div class="meta">#{item.id} {html.escape(item.uid)} · {html.escape(item.platform)} ·
{html.escape(item.theme)} / {html.escape(item.angle)} / {html.escape(item.tone)}</div>
<h3>{html.escape(item.title)}</h3><pre>{html.escape(item.body)}</pre>
</div>
<div class="card"><b>メタデータ</b>{meta_rows or '<div class="meta">なし</div>'}</div>
<div class="card"><b>ガードレール判定</b>
<div class="meta">類似度 {item.similarity:.0%}</div>{findings}</div>
<div class="card"><b>判断</b>
<form method="post" action="/item/{item.id}/approve">
  <input type="text" name="note" placeholder="{'上書き理由(必須)' if blocked else 'メモ(任意)'}">
  <div class="row"><button class="primary" type="submit">{approve_label}</button></div>
</form>
<form method="post" action="/item/{item.id}/revise">
  <input type="text" name="note" placeholder="修正内容(必須)">
  <div class="row"><button type="submit">修正を依頼</button></div>
</form>
<form method="post" action="/item/{item.id}/reject">
  <input type="text" name="note" placeholder="却下理由(任意)">
  <div class="row"><button class="danger" type="submit">却下</button></div>
</form>
</div>"""
            return HTMLResponse(_page(item.uid, body, settings,
                                      killswitch.active_halts(c, settings.state_dir)))
        finally:
            c.close()

    @app.post("/item/{item_id}/approve")
    def do_approve(item_id: int, request: Request, note: str = Form("")):
        c = conn()
        try:
            item = db.get_item(c, item_id)
            blocked = bool(item and (item.policy_report.get("blocking")
                                     or not item.quality_report.get("ok", True)))
            decision = approval.approve(c, settings, item_id, actor=actor(request), note=note,
                                        force=blocked and bool(note.strip()))
            msg = ("✅ 承認しました" if decision.ok else f"⚠ 承認できません: {decision.message}")
            return RedirectResponse(f"/item/{item_id}?msg={quote(msg)}", status_code=303)
        finally:
            c.close()

    @app.post("/item/{item_id}/reject")
    def do_reject(item_id: int, request: Request, note: str = Form("")):
        c = conn()
        try:
            approval.reject(c, item_id, actor=actor(request), note=note)
            return RedirectResponse("/", status_code=303)
        finally:
            c.close()

    @app.post("/item/{item_id}/revise")
    def do_revise(item_id: int, request: Request, note: str = Form("")):
        c = conn()
        try:
            decision = approval.request_revision(c, item_id, actor=actor(request), note=note)
            msg = "修正依頼を記録しました" if decision.ok else f"⚠ {decision.message}"
            return RedirectResponse(f"/item/{item_id}?msg={quote(msg)}", status_code=303)
        finally:
            c.close()

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(days: int = 7):
        c = conn()
        try:
            text = reporting.ops_report(c, settings, days=days)
            quotas = "".join(
                f'<div class="meta">{html.escape(s.summary())}</div>'
                for s in quota.all_states(c, settings)
            )
            body = (f'<div class="card"><b>投稿枠</b>{quotas}</div>'
                    f'<div class="card"><pre>{html.escape(text)}</pre></div>')
            return HTMLResponse(_page("ダッシュボード", body, settings,
                                      killswitch.active_halts(c, settings.state_dir)))
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
