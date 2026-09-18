from __future__ import annotations

import pytest
import yaml

from aiworker.core.config import PlatformConfig, load_settings
from aiworker.core.errors import ConfigError


def _write(tmp_path, data) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(p)


BASE = {
    "timezone": "Asia/Tokyo",
    "platforms": {"x": {"enabled": True, "daily_limit": 3, "weekly_limit": 10}},
}


def test_env_references_are_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_HOOK_URL", "https://example.invalid/hook")
    path = _write(tmp_path, {**BASE, "notify": {"channels": ["discord"],
                                                "discord_webhook_url": "${ENV:TEST_HOOK_URL}"}})
    s = load_settings(path, load_env=False)
    assert s.notify.discord_webhook_url == "https://example.invalid/hook"


def test_missing_env_reference_becomes_empty_not_literal(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    path = _write(tmp_path, {**BASE, "notify": {"discord_webhook_url": "${ENV:NOPE_NOT_SET}"}})
    assert load_settings(path, load_env=False).notify.discord_webhook_url == ""


def test_approval_gate_cannot_be_switched_off(tmp_path):
    path = _write(tmp_path, {**BASE, "require_human_approval": False})
    with pytest.raises(ConfigError, match="require_human_approval"):
        load_settings(path, load_env=False)


def test_daily_limit_above_weekly_is_rejected(tmp_path):
    path = _write(tmp_path, {"platforms": {"x": {"daily_limit": 10, "weekly_limit": 5}}})
    with pytest.raises(ConfigError, match="weekly_limit"):
        load_settings(path, load_env=False)


def test_unknown_platform_key_is_rejected(tmp_path):
    path = _write(tmp_path, {"platforms": {"x": {"dailylimit": 3}}})
    with pytest.raises(ConfigError, match="unknown platform keys"):
        load_settings(path, load_env=False)


def test_disclosure_required_without_text_is_rejected():
    cfg = PlatformConfig(name="yt", require_ai_disclosure=True, disclosure_text="")
    with pytest.raises(ConfigError, match="disclosure_text"):
        cfg.validate()


def test_shipped_example_config_is_valid():
    s = load_settings("config/config.example.yaml", load_env=False)
    assert s.require_human_approval is True
    assert s.dry_run is True, "the example config must ship with dry-run on"
    assert s.platforms, "example config defines no platforms"
