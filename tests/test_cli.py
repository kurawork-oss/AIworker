"""CLI smoke tests: each command must run and exit cleanly against a temp state dir."""

from __future__ import annotations

import yaml

from aiworker.cli import main

CONFIG = {
    "timezone": "Asia/Tokyo",
    "dry_run": True,
    "platforms": {
        "x": {"enabled": True, "publisher": "dryrun", "daily_limit": 3, "weekly_limit": 10,
              "min_interval_minutes": 60, "active_hours": [9, 21]},
    },
    "generation": {"provider": "mock", "themes": ["テストテーマ"]},
    "notify": {"channels": [], "min_level": "critical"},
}


def config_file(tmp_path):
    cfg = dict(CONFIG)
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["db_path"] = str(tmp_path / "state" / "t.db")
    cfg["log_dir"] = str(tmp_path / "state" / "logs")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return str(p)


def run(tmp_path, *args) -> int:
    return main(["-c", config_file(tmp_path), "-q", *args])


def test_doctor_runs(tmp_path, capsys):
    assert run(tmp_path, "doctor") == 0
    assert "aiworker doctor" in capsys.readouterr().out


def test_status_runs_on_an_empty_database(tmp_path, capsys):
    assert run(tmp_path, "status") == 0
    assert "停止中の系統はありません" in capsys.readouterr().out


def test_generate_then_review_then_approve_then_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AIWORKER_ACTOR", "pytest")
    assert run(tmp_path, "generate", "--channel", "social_post", "--count", "2") == 0
    assert "pending" in capsys.readouterr().out

    assert run(tmp_path, "review", "list") == 0
    assert "承認待ち 2" in capsys.readouterr().out

    assert run(tmp_path, "review", "approve", "1", "2") == 0
    assert "approve" in capsys.readouterr().out

    assert run(tmp_path, "plan") == 0
    assert "scheduled" in capsys.readouterr().out


def test_publish_reports_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AIWORKER_ACTOR", "pytest")
    run(tmp_path, "generate", "--channel", "social_post", "--count", "1")
    run(tmp_path, "review", "approve", "1")
    run(tmp_path, "plan")
    capsys.readouterr()
    assert run(tmp_path, "publish", "--at", "2026-12-01T12:00:00+09:00") == 0
    assert "dry_run" in capsys.readouterr().out


def test_stop_and_resume(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AIWORKER_ACTOR", "pytest")
    assert run(tmp_path, "stop", "--reason", "テスト") == 0
    capsys.readouterr()
    assert run(tmp_path, "resume") == 1, "resume without --yes must refuse"
    assert "--yes" in capsys.readouterr().out
    assert run(tmp_path, "resume", "--yes") == 0


def test_review_show_reports_missing_items(tmp_path):
    assert run(tmp_path, "review", "show", "9999") == 1


def test_revenue_add_and_report(tmp_path, capsys):
    assert run(tmp_path, "revenue", "add", "--date", "2026-09-15", "--source", "a8",
               "--amount", "1500") == 0
    capsys.readouterr()
    assert run(tmp_path, "report", "--days", "30") == 0
    assert "1,500" in capsys.readouterr().out


def test_metrics_add(tmp_path):
    assert run(tmp_path, "metrics", "add", "--date", "2026-09-15", "--platform", "x",
               "--reach", "500") == 0


def test_events_and_prune(tmp_path):
    run(tmp_path, "generate", "--channel", "social_post", "--count", "1")
    assert run(tmp_path, "events", "--limit", "5") == 0
    assert run(tmp_path, "prune", "--days", "30") == 0


def test_checklist_prints_the_document(tmp_path, capsys):
    assert run(tmp_path, "checklist") == 0
    assert "チェックリスト" in capsys.readouterr().out


# --------------------------------------------------------------------------
# web UI (skipped when FastAPI is not installed)
# --------------------------------------------------------------------------
import pytest  # noqa: E402

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

CFG_PATH: list[str] = [""]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIWORKER_ACTOR", "pytest")
    cfg = config_file(tmp_path)
    CFG_PATH[0] = cfg
    main(["-c", cfg, "-q", "generate", "--channel", "social_post", "--count", "2"])
    from aiworker.webui.app import create_app

    return TestClient(create_app(cfg))


def _block_item(item_id: int = 1) -> None:
    """Make one item fail the policy guard, so blocked-item paths can be tested."""
    from aiworker.approval import service as approval
    from aiworker.core import db
    from aiworker.core.config import load_settings

    settings = load_settings(CFG_PATH[0])
    conn = db.init_db(settings.db_path)
    approval.edit(conn, settings, item_id, actor="pytest",
                  body="この方法なら絶対に稼げます。" * 4)
    conn.close()


# ---- the five views render ------------------------------------------------
@pytest.mark.parametrize("path,marker", [
    ("/", "承認待ち"),
    ("/flow", "パイプラインの現在地"),
    ("/swipe", "残り"),
    ("/chat", "下書き"),
    ("/reports", "承認待ち"),
    ("/settings", "投稿枠の消化"),
])
def test_every_view_renders(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200, path
    assert marker in r.text, f"{path} is missing {marker!r}"


def test_every_view_is_in_japanese(client):
    """The operator reads this screen under time pressure; a raw enum value in
    the middle of it is a translation step they should not have to do.

    Attribute values (a `?status=blocked` link) are not what a person reads,
    so only the visible text is checked."""
    import re

    for path in ("/", "/flow", "/swipe", "/chat", "/reports", "/settings"):
        visible = re.sub(r"<[^>]+>", " ", client.get(path).text)
        for raw in ("pending_review", "needs_revision", "social_post", "note_article"):
            assert raw not in visible, f"{path} shows the raw value {raw!r}"


def test_shell_has_the_tab_bar_and_view_switcher(client):
    text = client.get("/").text
    for label in ("タスク", "レポート", "設定"):
        assert label in text
    for label in ("リスト", "フロー", "スワイプ", "対話"):
        assert label in text


# ---- actions --------------------------------------------------------------
def test_approve_from_the_list(client):
    r = client.post("/item/1/approve", data={"note": ""}, follow_redirects=True)
    assert "承認しました" in r.text
    assert "承認済" in client.get("/item/1").text


def test_revision_request(client):
    r = client.post("/item/1/revise", data={"note": "冒頭を具体的に"},
                    follow_redirects=True)
    assert "修正依頼を記録しました" in r.text


def test_an_empty_revision_note_is_reported_not_ignored(client):
    """A silent no-op is the worst failure mode here: the reviewer would walk
    away believing they had acted."""
    r = client.post("/item/1/revise", data={"note": "  "}, follow_redirects=True)
    assert "⚠" in r.text


def test_swipe_returns_to_the_deck_after_acting(client):
    r = client.post("/item/1/approve", data={"note": "", "next": "/swipe?index=0"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/swipe")


def test_chat_returns_to_the_chat_after_acting(client):
    r = client.post("/item/1/reject", data={"note": "重複", "next": "/chat"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/chat")


# ---- the guardrail stance survives every view -----------------------------
def test_blocked_items_are_excluded_from_the_swipe_deck(client):
    """One-tap approval is the wrong affordance for content a guardrail
    refused. Blocked items must be handled where the reasons are on screen."""
    _block_item(1)
    text = client.get("/swipe").text
    assert "スワイプ対象から外しています" in text
    assert "ブロックを確認する" in text


def test_the_list_offers_no_one_tap_approve_on_a_blocked_item(client):
    _block_item(1)
    text = client.get("/").text
    assert "理由を確認" in text, "a blocked card must route to the reasons"


def test_detail_does_not_make_the_override_the_primary_action(client):
    """If the override ever becomes the prominent button again, this fails."""
    _block_item(1)
    text = client.get("/item/1").text
    assert "上書き" in text, "the override must still be reachable"
    assert 'class="override"' in text
    assert '<button class="approve" type="submit">承認する</button>' not in text
    assert '<button class="brand" type="submit">修正を依頼</button>' in text


def test_chat_shows_the_block_reason_instead_of_an_approve_button(client):
    _block_item(1)
    text = client.get("/chat").text
    assert "ガードレールが止めました" in text
    assert "理由を読んで判断" in text


# ---- kill switch from the settings screen ---------------------------------
def test_stop_and_resume_from_the_ui(client):
    r = client.post("/stop", data={"reason": "テスト停止"}, follow_redirects=True)
    assert "停止しました" in r.text
    assert "テスト停止" in client.get("/").text, "a halt must show on every screen"
    r = client.post("/resume", data={}, follow_redirects=True)
    assert "解除しました" in r.text


def test_stopping_requires_a_reason(client):
    r = client.post("/stop", data={"reason": "  "}, follow_redirects=True)
    assert "理由が必要" in r.text


# ---- misc -----------------------------------------------------------------
def test_missing_item_returns_404(client):
    assert client.get("/item/9999").status_code == 404


def test_ui_refuses_to_bind_publicly():
    from aiworker.webui.app import run

    assert run(host="0.0.0.0") == 2


def test_healthz(client):
    assert client.get("/healthz").json()["ok"] is True


# ---- the manual-posting worklist ------------------------------------------
def _stage_one_via_ui(client) -> None:
    """Approve, schedule and publish one item with the manual publisher, so a
    staged item exists to test the outbox against."""
    from aiworker.approval import service as approval
    from aiworker.core import clock, db
    from aiworker.core.config import load_settings
    from aiworker.notify.notifier import Notifier
    from aiworker.scheduler import planner

    settings = load_settings(CFG_PATH[0])
    settings.platforms["x"].publisher = "manual"
    conn = db.init_db(settings.db_path)
    approval.approve(conn, settings, 1, actor="pytest")
    plan = planner.plan(conn, settings)
    planner.run_due(conn, settings, Notifier(settings.notify),
                    now=clock.parse_iso(plan.scheduled[0].job.scheduled_at))
    conn.close()


def test_outbox_is_empty_when_nothing_is_staged(client):
    assert "手動投稿待ちはありません" in client.get("/outbox").text


def test_outbox_lists_staged_items_with_the_full_body(client):
    _stage_one_via_ui(client)
    text = client.get("/outbox").text
    assert "投稿待ち" in text
    assert "投稿した" in text, "the worklist needs a way to close the loop"


def test_confirming_from_the_outbox_clears_it(client):
    _stage_one_via_ui(client)
    r = client.post("/item/1/posted", data={"url": "https://example.invalid/1"},
                    follow_redirects=True)
    assert "記録しました" in r.text
    assert "手動投稿待ちはありません" in client.get("/outbox").text


def test_outbox_command_lists_and_points_at_the_next_step(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AIWORKER_ACTOR", "pytest")
    assert run(tmp_path, "outbox") == 0
    assert "手動投稿待ちはありません" in capsys.readouterr().out


def test_outbox_offers_a_copy_button(client):
    """Copying the body is the whole job of this screen; hand-selecting a few
    hundred characters on a phone is not a workflow."""
    _stage_one_via_ui(client)
    text = client.get("/outbox").text
    assert "本文をコピー" in text
    assert "data-copy=" in text
