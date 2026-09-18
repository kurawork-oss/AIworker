from __future__ import annotations

from aiworker.guard import killswitch


def test_clean_state_is_running(conn, settings):
    assert not killswitch.check(conn, settings.state_dir).halted


def test_engage_and_release_globally(conn, settings):
    killswitch.engage(conn, settings.state_dir, reason="テスト停止")
    state = killswitch.check(conn, settings.state_dir)
    assert state.halted and state.reason == "テスト停止"
    killswitch.release(conn, settings.state_dir)
    assert not killswitch.check(conn, settings.state_dir).halted


def test_platform_halt_does_not_stop_other_platforms(conn, settings):
    killswitch.engage(conn, settings.state_dir, reason="x だけ停止", platform="x")
    assert killswitch.check(conn, settings.state_dir, "x").halted
    assert not killswitch.check(conn, settings.state_dir, "youtube").halted


def test_global_halt_covers_every_platform(conn, settings):
    killswitch.engage(conn, settings.state_dir, reason="全停止")
    assert killswitch.check(conn, settings.state_dir, "youtube").halted


def test_a_bare_stop_file_halts_even_without_the_database(conn, settings):
    """`touch var/STOP` must work from any shell, with no tooling."""
    (settings.state_dir / "STOP").write_text("emergency", encoding="utf-8")
    state = killswitch.check(conn, settings.state_dir)
    assert state.halted and state.source == "stop-file"


def test_release_removes_both_mechanisms(conn, settings):
    killswitch.engage(conn, settings.state_dir, reason="r", platform="x")
    assert killswitch.stop_file(settings.state_dir, "x").exists()
    killswitch.release(conn, settings.state_dir, platform="x")
    assert not killswitch.stop_file(settings.state_dir, "x").exists()
    assert not killswitch.check(conn, settings.state_dir, "x").halted


def test_active_halts_lists_every_scope(conn, settings):
    killswitch.engage(conn, settings.state_dir, reason="a", platform="x")
    killswitch.engage(conn, settings.state_dir, reason="b", platform="youtube")
    scopes = {h.scope for h in killswitch.active_halts(conn, settings.state_dir)}
    assert {"x", "youtube"} <= scopes


def test_halt_is_recorded_in_the_audit_log(conn, settings):
    from aiworker.core import db

    killswitch.engage(conn, settings.state_dir, reason="監査テスト", actor="tester")
    events = db.recent_events(conn, category="killswitch")
    assert events and "監査テスト" in events[0]["message"]
