"""Command line interface.

Design rule: every command that can cause something to leave the machine
prints what it is about to do and, where it is not trivially reversible, needs
an explicit flag. The destructive-feeling commands (``stop``) are the easy
ones; the dangerous command is ``publish``, and it is the one wrapped in the
most checks.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__
from .approval import service as approval
from .core import clock, db
from .core.config import REPO_ROOT, Settings, load_settings
from .core.errors import AIWorkerError, ConfigError
from .core.logging_setup import get_logger, purge_old_logs, setup_logging
from .core.models import Severity, Status
from .generators.prompts import SPECS
from .generators.service import GenerationService
from .guard import anomaly, killswitch, quota
from .notify.notifier import Alert, Notifier
from .publishers.registry import publisher_for
from .revenue import importer, report as reporting
from .scheduler import planner

log = get_logger("cli")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _actor(args) -> str:
    if getattr(args, "actor", None):
        return args.actor
    return os.environ.get("AIWORKER_ACTOR") or _login_name()


def _login_name() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


class Context:
    """Loaded settings + an open DB connection + a notifier."""

    def __init__(self, args):
        self.settings: Settings = load_settings(getattr(args, "config", None))
        setup_logging(self.settings.log_dir, retention_days=self.settings.log_retention_days,
                      quiet=getattr(args, "quiet", False))
        self.conn = db.init_db(self.settings.db_path)
        self.notifier = Notifier(self.settings.notify)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def _print_items(items, settings: Settings, *, verbose: bool = False) -> None:
    if not items:
        print("(該当なし)")
        return
    for it in items:
        flags = []
        if it.policy_report.get("blocking"):
            flags.append("POLICY")
        if not it.quality_report.get("ok", True):
            flags.append("QUALITY")
        if it.similarity >= 0.6:
            flags.append(f"sim{it.similarity:.0%}")
        flag_s = (" [" + ",".join(flags) + "]") if flags else ""
        created = clock.fmt_local(clock.parse_iso(it.created_at), settings.timezone, "%m/%d %H:%M")
        print(f"#{it.id:<4} {it.uid}  {it.status:<14} {it.platform:<12} {created}{flag_s}")
        print(f"      {it.preview(72)}")
        if verbose:
            for f in it.policy_report.get("findings", []):
                print(f"      - policy  [{f['severity']}] {f['detail']}")
            for i in it.quality_report.get("issues", []):
                print(f"      - quality [{'fatal' if i['fatal'] else 'warn'}] {i['detail']}")
            if it.revision_note:
                print(f"      - 修正依頼: {it.revision_note}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_init(args) -> int:
    cfg_dst = REPO_ROOT / "config" / "config.yaml"
    cfg_src = REPO_ROOT / "config" / "config.example.yaml"
    created = []
    if not cfg_dst.exists() and cfg_src.exists():
        cfg_dst.write_text(cfg_src.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(str(cfg_dst))
    env_dst, env_src = REPO_ROOT / ".env", REPO_ROOT / ".env.example"
    if not env_dst.exists() and env_src.exists():
        env_dst.write_text(env_src.read_text(encoding="utf-8"), encoding="utf-8")
        env_dst.chmod(0o600)
        created.append(str(env_dst))

    ctx = Context(args)
    try:
        for sub in ("logs", "outbox", "imports"):
            (ctx.settings.state_dir / sub).mkdir(parents=True, exist_ok=True)
        print(f"database ready : {ctx.settings.db_path}")
        print(f"state directory: {ctx.settings.state_dir}")
        for c in created:
            print(f"created        : {c}")
        print()
        print("次の手順:")
        print("  1. config/config.yaml のテーマ・上限値を自分の運用に合わせる")
        print("  2. .env に通知先(DISCORD_WEBHOOK_URL など)を設定する")
        print("  3. aiworker doctor で設定を検証する")
        print("  4. aiworker generate --channel social_post --count 3")
        print("  5. aiworker review list")
    finally:
        ctx.close()
    return 0


def cmd_doctor(args) -> int:
    """Pre-flight check. Exits non-zero if anything would stop a real run."""
    problems, warnings, notes = [], [], []
    try:
        settings = load_settings(getattr(args, "config", None))
    except ConfigError as exc:
        print(f"✗ config: {exc}")
        return 2
    notes.append(f"config file      : {settings.source_path}")
    notes.append(f"timezone         : {settings.timezone}")
    notes.append(f"dry_run          : {settings.dry_run}")
    notes.append(f"approval gate    : {'ON' if settings.require_human_approval else 'OFF'}")
    notes.append(f"generation       : {settings.generation.provider} / {settings.generation.model}")

    if settings.generation.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("generation.provider=anthropic but ANTHROPIC_API_KEY is unset")
    if settings.generation.provider == "mock":
        warnings.append("generation.provider=mock: output is placeholder text, not publishable copy")
    if not settings.generation.themes:
        warnings.append("generation.themes is empty; generation will fall back to a single theme")

    live = [n for n, c in settings.platforms.items() if c.enabled]
    if not live:
        warnings.append("no platform is enabled; nothing can be scheduled")
    notes.append(f"enabled platforms: {', '.join(live) or '(none)'}")
    for name, cfg in settings.platforms.items():
        if cfg.enabled and cfg.daily_limit > 10:
            warnings.append(f"{name}: daily_limit={cfg.daily_limit} is high for one account")
        if cfg.enabled and cfg.min_interval_minutes < 15:
            warnings.append(f"{name}: min_interval_minutes={cfg.min_interval_minutes} is very short")

    channels = set(settings.notify.channels)
    if channels == {"console"}:
        warnings.append("notify.channels is console-only: nobody sees an alert unless they are "
                        "watching this terminal. Set DISCORD_WEBHOOK_URL or SLACK_WEBHOOK_URL.")
    if "discord" in channels and not settings.notify.discord_webhook_url:
        problems.append("notify lists 'discord' but DISCORD_WEBHOOK_URL is empty")
    if "slack" in channels and not settings.notify.slack_webhook_url:
        problems.append("notify lists 'slack' but SLACK_WEBHOOK_URL is empty")

    if len(settings.policy.banned_terms) + len(settings.policy.trademark_terms) == 0:
        warnings.append("policy deny-lists are empty; only the built-in rules apply")

    try:
        conn = db.init_db(settings.db_path)
        counts = db.count_by_status(conn)
        notes.append(f"database         : {settings.db_path} ({sum(counts.values())} items)")
        halts = killswitch.active_halts(conn, settings.state_dir)
        for h in halts:
            warnings.append(f"kill switch engaged: {h.describe()}")
        conn.close()
    except Exception as exc:
        problems.append(f"database unusable: {exc}")

    if (REPO_ROOT / ".env").exists():
        mode = (REPO_ROOT / ".env").stat().st_mode & 0o077
        if mode:
            warnings.append(".env is readable by other users; run: chmod 600 .env")

    print("── aiworker doctor ─────────────────────────────────────────")
    for n in notes:
        print(f"  {n}")
    print()
    for w in warnings:
        print(f"  ⚠ {w}")
    for p in problems:
        print(f"  ✗ {p}")
    if not problems and not warnings:
        print("  ✓ 問題なし")
    print()
    return 1 if problems else 0


def cmd_generate(args) -> int:
    ctx = Context(args)
    try:
        svc = GenerationService(ctx.conn, ctx.settings)
        result = svc.generate(
            args.channel, args.count, platform=args.platform,
            themes=args.theme or None, dry_run=args.dry_run,
        )
        print(result.summary())
        for s in result.skipped:
            print(f"  skipped: {s}")
        _print_items(result.items, ctx.settings, verbose=args.verbose)
        if result.blocked:
            print(f"\n⚠ {len(result.blocked)} 件がガードレールでブロックされました。")
            print("  確認: aiworker review list --status blocked --verbose")
        if result.pending:
            print(f"\n次: aiworker review list  ({len(result.pending)} 件が承認待ち)")
        return 0
    finally:
        ctx.close()


def cmd_review_list(args) -> int:
    ctx = Context(args)
    try:
        items = approval.queue(
            ctx.conn, status=args.status or None, platform=args.platform,
            channel=args.channel, limit=args.limit,
        )
        _print_items(items, ctx.settings, verbose=args.verbose)
        print()
        counts = approval.stats(ctx.conn)
        print(f"承認待ち {counts['pending_review']} / ブロック {counts['blocked']} / "
              f"修正依頼 {counts['needs_revision']} / 承認済 {counts['approved']}")
        return 0
    finally:
        ctx.close()


def cmd_review_show(args) -> int:
    ctx = Context(args)
    try:
        item = db.get_item(ctx.conn, int(args.ref)) if str(args.ref).isdigit() \
            else db.get_item_by_uid(ctx.conn, args.ref)
        if not item:
            print(f"no such item: {args.ref}", file=sys.stderr)
            return 1
        print("=" * 68)
        print(f"#{item.id} {item.uid}  [{item.status}]")
        print(f"channel : {item.channel}   platform: {item.platform}/{item.account}")
        print(f"axes    : {item.theme} / {item.angle} / {item.tone}")
        print(f"created : {clock.fmt_local(clock.parse_iso(item.created_at), ctx.settings.timezone)}")
        if item.approved_by:
            print(f"approved: {item.approved_by} at {item.approved_at}")
        print("=" * 68)
        if item.title:
            print(f"\n# {item.title}\n")
        print(item.body)
        if item.meta:
            print("\n--- meta ---")
            print(json.dumps(item.meta, ensure_ascii=False, indent=2))
        print("\n--- guardrails ---")
        print(f"similarity: {item.similarity:.0%} (nearest: "
              f"{item.quality_report.get('nearest_uid') or '-'})")
        for f in item.policy_report.get("findings", []):
            print(f"  policy  [{f['severity']}] {f['rule']}: {f['detail']}")
        for i in item.quality_report.get("issues", []):
            print(f"  quality [{'fatal' if i['fatal'] else 'warn'}] {i['rule']}: {i['detail']}")
        if not item.policy_report.get("findings") and not item.quality_report.get("issues"):
            print("  指摘なし")
        history = db.approval_history(ctx.conn, item.id)
        if history:
            print("\n--- 履歴 ---")
            for h in history:
                ts = clock.fmt_local(clock.parse_iso(h["created_at"]), ctx.settings.timezone)
                print(f"  {ts} {h['action']} by {h['actor']} {h['note']}")
        return 0
    finally:
        ctx.close()


def cmd_review_approve(args) -> int:
    ctx = Context(args)
    try:
        rc = 0
        for ref in args.refs:
            d = approval.approve(ctx.conn, ctx.settings, ref, actor=_actor(args),
                                 note=args.note, force=args.force)
            print(d)
            rc = rc or (0 if d.ok else 1)
        return rc
    finally:
        ctx.close()


def cmd_review_reject(args) -> int:
    ctx = Context(args)
    try:
        for ref in args.refs:
            print(approval.reject(ctx.conn, ref, actor=_actor(args), note=args.note))
        return 0
    finally:
        ctx.close()


def cmd_review_revise(args) -> int:
    ctx = Context(args)
    try:
        print(approval.request_revision(ctx.conn, args.ref, actor=_actor(args), note=args.note))
        return 0
    finally:
        ctx.close()


def cmd_review_edit(args) -> int:
    ctx = Context(args)
    try:
        body = None
        if args.body_file:
            body = Path(args.body_file).read_text(encoding="utf-8")
        elif args.body:
            body = args.body
        d = approval.edit(ctx.conn, ctx.settings, args.ref, actor=_actor(args),
                          title=args.title, body=body)
        print(d)
        return 0 if d.ok else 1
    finally:
        ctx.close()


def cmd_plan(args) -> int:
    ctx = Context(args)
    try:
        result = planner.plan(ctx.conn, ctx.settings, limit=args.limit, platform=args.platform,
                              horizon_days=args.horizon)
        print(result.summary())
        for e in result.entries:
            if e.job:
                when = clock.fmt_local(clock.parse_iso(e.job.scheduled_at), ctx.settings.timezone)
                print(f"  {e.item.uid}  →  {when}  ({e.job.platform})")
            else:
                print(f"  {e.item.uid}  skipped: {e.skipped}")
        return 0
    finally:
        ctx.close()


def cmd_publish(args) -> int:
    ctx = Context(args)
    try:
        now = clock.parse_iso(args.at) if args.at else clock.now_utc()
        if ctx.settings.dry_run:
            print("※ dry_run=true: 外部への送信は行われません")
        outcomes = planner.run_due(ctx.conn, ctx.settings, ctx.notifier, now=now,
                                   limit=args.limit, platform=args.platform)
        if not outcomes:
            print("実行対象のジョブはありません")
        for o in outcomes:
            print(f"  {o}")
        return 0
    finally:
        ctx.close()


def cmd_mark_published(args) -> int:
    ctx = Context(args)
    try:
        print(planner.mark_published(ctx.conn, args.ref, external_url=args.url,
                                     external_id=args.external_id))
        return 0
    finally:
        ctx.close()


def cmd_outbox(args) -> int:
    """List what has been staged for a person to post, and not yet confirmed.

    With `publisher: manual` this is the operator's actual worklist. Without
    it, the only record of what still needs posting is the outbox directory
    and the operator's memory.
    """
    ctx = Context(args)
    try:
        jobs = db.staged_jobs(ctx.conn, limit=args.limit)
        if not jobs:
            print("手動投稿待ちはありません")
            return 0
        print(f"手動投稿待ち {len(jobs)}件\n")
        for job in jobs:
            item = db.get_item(ctx.conn, job.content_id)
            when = clock.fmt_local(clock.parse_iso(job.published_at or job.scheduled_at),
                                   ctx.settings.timezone, "%m/%d %H:%M")
            print(f"  {item.uid if item else job.content_id}  {job.platform:<12} {when}")
            if item:
                print(f"    {item.preview(64)}")
            if job.external_url:
                print(f"    下書き: {job.external_url}")
        print("\n投稿したら記録してください:")
        print("  aiworker mark-published <uid> --url <投稿URL>")
        return 0
    finally:
        ctx.close()


def cmd_status(args) -> int:
    ctx = Context(args)
    try:
        halts = killswitch.active_halts(ctx.conn, ctx.settings.state_dir)
        if halts:
            for h in halts:
                print(f"🛑 {h.describe()}")
        else:
            print("✅ 停止中の系統はありません")
        counts = approval.stats(ctx.conn)
        print()
        print(f"承認待ち {counts['pending_review']}  ブロック {counts['blocked']}  "
              f"修正依頼 {counts['needs_revision']}  承認済 {counts['approved']}  "
              f"予約 {counts['scheduled']}  手動投稿待ち {counts['staged']}  "
              f"公開 {counts['published']}  失敗 {counts['failed']}")
        if counts["staged"]:
            print(f"\n📮 手動投稿待ちが {counts['staged']}件 あります: aiworker outbox")
        print()
        for st in quota.all_states(ctx.conn, ctx.settings):
            cfg = ctx.settings.platform(st.platform)
            mark = "  " if cfg.enabled else " ·"
            print(f"{mark} {st.summary()}  publisher={cfg.publisher}"
                  f"{'  ⚠上限間近' if st.near_limit else ''}")
        upcoming = db.list_jobs(ctx.conn, status="queued", limit=5)
        if upcoming:
            print("\n次の予定:")
            for j in upcoming:
                when = clock.fmt_local(clock.parse_iso(j.scheduled_at), ctx.settings.timezone)
                item = db.get_item(ctx.conn, j.content_id)
                print(f"  {when}  {j.platform:<12} {item.uid if item else j.content_id}")
        return 0
    finally:
        ctx.close()


def cmd_report(args) -> int:
    ctx = Context(args)
    try:
        text = reporting.ops_report(ctx.conn, ctx.settings, days=args.days)
        print(text)
        if args.notify:
            ctx.notifier.send(Alert(Severity.WARNING, "運用レポート", text[:1500]))
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"(saved to {args.output})")
        return 0
    finally:
        ctx.close()


def cmd_revenue_import(args) -> int:
    ctx = Context(args)
    try:
        result = importer.import_csv(ctx.conn, Path(args.path), source=args.source,
                                     account=args.account, currency=args.currency)
        print(result.summary())
        for s in result.skipped[:10]:
            print(f"  - {s}")
        return 0 if result.imported or not result.rows else 1
    finally:
        ctx.close()


def cmd_revenue_add(args) -> int:
    ctx = Context(args)
    try:
        print(importer.record_manual(ctx.conn, date=args.date, source=args.source,
                                     amount=args.amount, units=args.units, note=args.note,
                                     currency=args.currency))
        return 0
    finally:
        ctx.close()


def cmd_metrics_add(args) -> int:
    ctx = Context(args)
    try:
        with db.transaction(ctx.conn):
            db.insert_metric(ctx.conn, date=args.date, platform=args.platform,
                             account=args.account, external_id=args.external_id,
                             impressions=args.impressions, reach=args.reach,
                             clicks=args.clicks, note=args.note)
        print(f"recorded metrics for {args.platform} on {args.date}")
        found = anomaly.scan(ctx.conn, ctx.settings)
        if found:
            anomaly.react(ctx.conn, ctx.settings, found, ctx.notifier)
            for a in found:
                print(f"  ⚠ {a.code}: {a.message}")
        return 0
    finally:
        ctx.close()


def cmd_stop(args) -> int:
    ctx = Context(args)
    try:
        state = killswitch.engage(ctx.conn, ctx.settings.state_dir, reason=args.reason,
                                  actor=_actor(args), platform=args.platform)
        print(f"🛑 {state.describe()}")
        ctx.notifier.send(Alert(Severity.CRITICAL, f"停止: {args.platform or 'ALL'}",
                                args.reason, args.platform or ""))
        return 0
    finally:
        ctx.close()


def cmd_resume(args) -> int:
    ctx = Context(args)
    try:
        halts = killswitch.active_halts(ctx.conn, ctx.settings.state_dir)
        if not halts:
            print("停止中の系統はありません")
            return 0
        if not args.yes:
            print("以下を解除します:")
            for h in halts:
                if not args.platform or h.scope in (args.platform, "global"):
                    print(f"  - {h.describe()}")
            print("\n原因が解消したことを確認しましたか? 実行するには --yes を付けてください。")
            return 1
        killswitch.release(ctx.conn, ctx.settings.state_dir, actor=_actor(args),
                           platform=args.platform)
        print(f"✅ 解除しました ({args.platform or 'global'})")
        return 0
    finally:
        ctx.close()


def cmd_events(args) -> int:
    ctx = Context(args)
    try:
        for e in reversed(db.recent_events(ctx.conn, limit=args.limit, category=args.category,
                                           min_level=args.level)):
            ts = clock.fmt_local(clock.parse_iso(e["ts"]), ctx.settings.timezone, "%m/%d %H:%M:%S")
            plat = f" {e['platform']}" if e["platform"] else ""
            print(f"{ts} [{e['level']:<8}] {e['category']}{plat}: {e['message']}")
        return 0
    finally:
        ctx.close()


def cmd_prune(args) -> int:
    ctx = Context(args)
    try:
        days = args.days or ctx.settings.log_retention_days
        with db.transaction(ctx.conn):
            rows = db.purge_old_events(ctx.conn, days)
        files = purge_old_logs(ctx.settings.log_dir, days)
        print(f"deleted {rows} event rows and {len(files)} log files older than {days} days")
        return 0
    finally:
        ctx.close()


def cmd_checklist(args) -> int:
    path = REPO_ROOT / "docs" / "05-risk-checklist.md"
    if path.exists():
        print(path.read_text(encoding="utf-8"))
        return 0
    print("docs/05-risk-checklist.md が見つかりません", file=sys.stderr)
    return 1


def cmd_serve(args) -> int:
    try:
        from .webui.app import run
    except ImportError as exc:
        print(f"web UI needs FastAPI: pip install -r requirements-web.txt ({exc})",
              file=sys.stderr)
        return 1
    return run(host=args.host, port=args.port, config=getattr(args, "config", None))


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aiworker",
        description="リスクヘッジ重視・人間承認ゲート付きのコンテンツ運用システム",
    )
    p.add_argument("--version", action="version", version=f"aiworker {__version__}")
    p.add_argument("-c", "--config", help="設定ファイルのパス (既定: config/config.yaml)")
    p.add_argument("-q", "--quiet", action="store_true", help="標準エラーへの出力を抑制")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="DBと設定ファイルを初期化する")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("doctor", help="設定と前提条件を検証する")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("generate", help="コンテンツを生成して承認キューに入れる")
    sp.add_argument("--channel", required=True, choices=sorted(SPECS))
    sp.add_argument("--count", type=int, default=3)
    sp.add_argument("--platform", help="投稿先 (省略時はチャンネルの既定値)")
    sp.add_argument("--theme", action="append", help="テーマを指定 (複数可)")
    sp.add_argument("--dry-run", action="store_true", help="生成するがDBに保存しない")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_generate)

    rev = sub.add_parser("review", help="承認ゲート")
    rsub = rev.add_subparsers(dest="review_command", required=True)

    sp = rsub.add_parser("list", help="承認待ち一覧")
    sp.add_argument("--status", action="append",
                    choices=[s.value for s in Status])
    sp.add_argument("--platform")
    sp.add_argument("--channel", choices=sorted(SPECS))
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_review_list)

    sp = rsub.add_parser("show", help="1件の全文と判定理由を表示")
    sp.add_argument("ref")
    sp.set_defaults(func=cmd_review_show)

    sp = rsub.add_parser("approve", help="承認する")
    sp.add_argument("refs", nargs="+")
    sp.add_argument("--note", default="")
    sp.add_argument("--actor")
    sp.add_argument("--force", action="store_true",
                    help="ガードレールのブロックを人間の判断で上書きする (--note 必須)")
    sp.set_defaults(func=cmd_review_approve)

    sp = rsub.add_parser("reject", help="却下する")
    sp.add_argument("refs", nargs="+")
    sp.add_argument("--note", default="")
    sp.add_argument("--actor")
    sp.set_defaults(func=cmd_review_reject)

    sp = rsub.add_parser("revise", help="修正を依頼する")
    sp.add_argument("ref")
    sp.add_argument("--note", required=True, help="どこを直すか")
    sp.add_argument("--actor")
    sp.set_defaults(func=cmd_review_revise)

    sp = rsub.add_parser("edit", help="本文を修正して再チェックする")
    sp.add_argument("ref")
    sp.add_argument("--title")
    sp.add_argument("--body")
    sp.add_argument("--body-file")
    sp.add_argument("--actor")
    sp.set_defaults(func=cmd_review_edit)

    sp = sub.add_parser("plan", help="承認済みコンテンツに投稿枠を割り当てる")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--platform")
    sp.add_argument("--horizon", type=int, default=7, help="何日先まで探すか")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("publish", help="実行時刻が来たジョブを処理する")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--platform")
    sp.add_argument("--at", help="この時刻として実行する (ISO8601, テスト用)")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("mark-published", help="手動投稿した結果を記録する")
    sp.add_argument("ref")
    sp.add_argument("--url", default="")
    sp.add_argument("--external-id", default="")
    sp.set_defaults(func=cmd_mark_published)

    sp = sub.add_parser("outbox", help="手動投稿待ちの一覧（publisher: manual 運用の作業リスト）")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_outbox)

    sp = sub.add_parser("status", help="現在の状態を1画面で表示")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("report", help="運用・収益レポート")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--notify", action="store_true", help="通知チャンネルにも送る")
    sp.add_argument("--output", help="ファイルに保存する")
    sp.set_defaults(func=cmd_report)

    revn = sub.add_parser("revenue", help="収益データ")
    rnsub = revn.add_subparsers(dest="revenue_command", required=True)
    sp = rnsub.add_parser("import", help="CSVを取り込む")
    sp.add_argument("path")
    sp.add_argument("--source", required=True, help="a8 / note / youtube / adobe_stock など")
    sp.add_argument("--account", default="main")
    sp.add_argument("--currency", default="JPY")
    sp.set_defaults(func=cmd_revenue_import)
    sp = rnsub.add_parser("add", help="手入力で1件追加する")
    sp.add_argument("--date", required=True)
    sp.add_argument("--source", required=True)
    sp.add_argument("--amount", type=float, required=True)
    sp.add_argument("--units", type=int, default=0)
    sp.add_argument("--currency", default="JPY")
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_revenue_add)

    met = sub.add_parser("metrics", help="リーチ等の実績値")
    msub = met.add_subparsers(dest="metrics_command", required=True)
    sp = msub.add_parser("add", help="1件記録する (リーチ急減検知に使われる)")
    sp.add_argument("--date", required=True)
    sp.add_argument("--platform", required=True)
    sp.add_argument("--account", default="main")
    sp.add_argument("--external-id", default="")
    sp.add_argument("--impressions", type=int, default=0)
    sp.add_argument("--reach", type=int, default=0)
    sp.add_argument("--clicks", type=int, default=0)
    sp.add_argument("--note", default="")
    sp.set_defaults(func=cmd_metrics_add)

    sp = sub.add_parser("stop", help="緊急停止 (全体またはプラットフォーム単位)")
    sp.add_argument("--platform")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--actor")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("resume", help="停止を解除する")
    sp.add_argument("--platform")
    sp.add_argument("--yes", action="store_true", help="確認済みとして実行する")
    sp.add_argument("--actor")
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("events", help="監査ログを表示")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--category")
    sp.add_argument("--level", choices=[s.value for s in Severity])
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("prune", help="保持期間を過ぎたログとイベントを削除")
    sp.add_argument("--days", type=int)
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("checklist", help="運用開始前チェックリストを表示")
    sp.set_defaults(func=cmd_checklist)

    sp = sub.add_parser("serve", help="承認用の簡易Web UIを起動")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AIWorkerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `aiworker status | head` closes the pipe early; that is not an error.
        # Point stdout at /dev/null so the interpreter's own flush at exit does
        # not raise again (the recipe from the Python docs).
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
