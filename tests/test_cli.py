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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIWORKER_ACTOR", "pytest")
    cfg = config_file(tmp_path)
    main(["-c", cfg, "-q", "generate", "--channel", "social_post", "--count", "2"])
    from aiworker.webui.app import create_app

    return TestClient(create_app(cfg))


def test_ui_lists_the_queue(client):
    r = client.get("/")
    assert r.status_code == 200 and "承認待ち 2" in r.text


def test_ui_approves_and_says_so(client):
    r = client.post("/item/1/approve", data={"note": ""}, follow_redirects=True)
    assert "承認しました" in r.text
    assert "approved" in client.get("/item/1").text


def test_ui_records_a_revision_request(client):
    r = client.post("/item/1/revise", data={"note": "冒頭を具体的に"}, follow_redirects=True)
    assert "修正依頼を記録しました" in r.text
    assert "needs_revision" in r.text


def test_ui_reports_a_refused_override_instead_of_doing_nothing(client, tmp_path):
    """A silent no-op is the worst failure mode here: the reviewer would walk
    away believing they approved something they did not."""
    r = client.post("/item/1/revise", data={"note": "  "}, follow_redirects=True)
    assert "⚠" in r.text, "an empty revision note must be reported, not ignored"


def test_ui_rejects(client):
    r = client.post("/item/2/reject", data={"note": "重複"}, follow_redirects=True)
    assert r.status_code == 200
    assert "rejected" in client.get("/item/2").text


def test_ui_dashboard_renders(client):
    assert "承認キュー" in client.get("/dashboard").text


def test_ui_refuses_to_bind_publicly():
    from aiworker.webui.app import run

    assert run(host="0.0.0.0") == 2


def test_healthz(client):
    assert client.get("/healthz").json()["ok"] is True
