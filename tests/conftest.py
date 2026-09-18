from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiworker.core import db  # noqa: E402
from aiworker.core.config import (  # noqa: E402
    AnomalyConfig, GenerationConfig, NotifyConfig, PlatformConfig, PolicyConfig,
    QualityConfig, Settings,
)
from aiworker.notify.notifier import Notifier  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A self-contained settings object: no config file, no shared state."""
    s = Settings(
        timezone="Asia/Tokyo",
        state_dir=tmp_path,
        db_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        dry_run=False,
        platforms={
            "x": PlatformConfig(
                name="x", enabled=True, publisher="dryrun", daily_limit=3, weekly_limit=10,
                min_interval_minutes=60, jitter_minutes=10, active_hours=(9, 21),
                max_retries=2, retry_backoff_seconds=0,
            ),
            "youtube": PlatformConfig(
                name="youtube", enabled=True, publisher="dryrun", daily_limit=2,
                weekly_limit=6, min_interval_minutes=120, active_hours=(10, 20),
                require_ai_disclosure=True, disclosure_text="AI生成素材を含みます",
            ),
            "note": PlatformConfig(name="note", enabled=False, publisher="dryrun"),
        },
        anomaly=AnomalyConfig(consecutive_failure_threshold=2, reach_min_samples=3),
        quality=QualityConfig(max_similarity=0.75),
        policy=PolicyConfig(banned_terms=["禁止ワード"], trademark_terms=["Ghibli"]),
        generation=GenerationConfig(provider="mock", themes=["テストテーマA", "テストテーマB"],
                                    max_pending_per_channel=20),
        notify=NotifyConfig(channels=[], min_level="critical"),
    )
    s.validate()
    return s


@pytest.fixture
def conn(settings):
    c = db.init_db(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def notifier(settings) -> Notifier:
    return Notifier(settings.notify)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(20260918)


@pytest.fixture
def make_item(conn):
    """Insert a minimal content item and return it. Publish jobs carry a real
    foreign key to content_items, so tests need a row to point at."""
    import itertools

    from aiworker.core.models import ContentItem, Status

    counter = itertools.count(1)

    def _make(*, platform="x", status=Status.APPROVED.value, channel="social_post",
              title="テスト見出し", body="テスト本文です。" * 6, **kw):
        n = next(counter)
        item = ContentItem(
            uid=f"test-{n:04d}", channel=channel, platform=platform, title=title,
            body=body, status=status, approved_at="2026-09-01T00:00:00+00:00",
            approved_by="tester", **kw,
        )
        return db.insert_item(conn, item)

    return _make
