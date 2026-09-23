"""Image generation for the stock channel: providers + the service wiring."""

from __future__ import annotations

import struct

import pytest

from aiworker.approval import service as approval
from aiworker.core import db
from aiworker.core.models import Channel, Status
from aiworker.generators.service import GenerationService
from aiworker.imagegen import service as imagegen
from aiworker.imagegen.providers import ImageRequest, build_provider


def test_mock_provider_returns_a_valid_png():
    r = build_provider("mock").generate(ImageRequest(prompt="abstract", seed=1))
    assert r.ok
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", r.data[16:24])
    assert w > 0 and h > 0


def test_mock_is_deterministic_per_seed():
    a = build_provider("mock").generate(ImageRequest(prompt="x", seed=7)).data
    b = build_provider("mock").generate(ImageRequest(prompt="x", seed=7)).data
    assert a == b


def test_unknown_provider_is_a_config_error():
    from aiworker.core.errors import ConfigError

    with pytest.raises(ConfigError, match="image.provider"):
        build_provider("chatgpt-browser")


def _approved_stock(conn, settings, n=2):
    settings.platforms.setdefault("adobe_stock", settings.platforms["x"])
    items = GenerationService(conn, settings).generate(
        Channel.STOCK_ASSET.value, n, platform="adobe_stock").items
    for it in items:
        approval.approve(conn, settings, it.id, actor="tester")
    return items


def test_service_writes_a_png_and_metadata_per_item(conn, settings, tmp_path):
    settings.state_dir = tmp_path
    _approved_stock(conn, settings, 2)
    run = imagegen.generate(conn, settings)
    assert len(run.generated) == 2
    for uid in run.generated:
        item = db.get_item_by_uid(conn, uid)
        png = imagegen.image_path(settings, item)
        assert png.exists() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        meta = png.with_suffix(".json")
        assert meta.exists() and "keywords" in meta.read_text()


def test_items_without_a_prompt_are_skipped_not_failed(conn, settings, tmp_path):
    settings.state_dir = tmp_path
    items = _approved_stock(conn, settings, 1)
    conn.execute("UPDATE content_items SET meta='{}' WHERE id=?", (items[0].id,))
    run = imagegen.generate(conn, settings)
    assert run.skipped and not run.failed


def test_already_generated_items_are_not_redone(conn, settings, tmp_path):
    settings.state_dir = tmp_path
    _approved_stock(conn, settings, 1)
    first = imagegen.generate(conn, settings)
    assert len(first.generated) == 1
    second = imagegen.generate(conn, settings)
    assert second.generated == [], "an image already on disk must not be regenerated"


def test_mock_backend_is_free(conn, settings, tmp_path):
    settings.state_dir = tmp_path
    _approved_stock(conn, settings, 2)
    assert imagegen.generate(conn, settings).cost_usd == 0.0
