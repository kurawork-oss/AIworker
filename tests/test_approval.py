"""The approval gate is the core safety property; these are its tests."""

from __future__ import annotations

import pytest

from aiworker.approval import service as approval
from aiworker.core import db
from aiworker.core.models import Status
from aiworker.generators.service import GenerationService


def generate(conn, settings, n=3, channel="social_post", platform="x"):
    return GenerationService(conn, settings).generate(channel, n, platform=platform)


def test_generation_never_produces_approved_content(conn, settings):
    result = generate(conn, settings, 5)
    assert result.items
    assert all(i.status != Status.APPROVED.value for i in result.items)


def test_approve_records_actor_and_timestamp(conn, settings):
    item = generate(conn, settings, 1).items[0]
    d = approval.approve(conn, settings, item.id, actor="nakakura")
    assert d.ok
    stored = db.get_item(conn, item.id)
    assert stored.status == Status.APPROVED.value
    assert stored.approved_by == "nakakura" and stored.approved_at


def test_every_decision_is_written_to_the_audit_trail(conn, settings):
    item = generate(conn, settings, 1).items[0]
    approval.request_revision(conn, item.id, actor="a", note="直して")
    approval.approve(conn, settings, item.id, actor="b", note="ok")
    actions = [r["action"] for r in db.approval_history(conn, item.id)]
    assert actions == ["request_revision", "approve"]


def test_blocked_content_cannot_be_approved_without_force(conn, settings):
    item = generate(conn, settings, 1).items[0]
    approval.edit(conn, settings, item.id, actor="a", body="禁止ワードを含む本文です。" * 4)
    d = approval.approve(conn, settings, item.id, actor="a")
    assert not d.ok and "--force" in d.message
    assert db.get_item(conn, item.id).status == Status.BLOCKED.value


def test_override_requires_a_written_reason(conn, settings):
    item = generate(conn, settings, 1).items[0]
    approval.edit(conn, settings, item.id, actor="a", body="禁止ワードを含む本文です。" * 4)
    assert not approval.approve(conn, settings, item.id, actor="a", force=True).ok
    d = approval.approve(conn, settings, item.id, actor="a", force=True, note="社内用のため問題なし")
    assert d.ok
    history = db.approval_history(conn, item.id)
    assert history[-1]["action"] == "approve_override"
    assert history[-1]["note"] == "社内用のため問題なし"


def test_editing_re_runs_the_guardrails(conn, settings):
    """An edit after generation must not carry the earlier clean verdict."""
    item = generate(conn, settings, 1).items[0]
    assert item.status == Status.PENDING_REVIEW.value
    approval.edit(conn, settings, item.id, actor="a",
                  body="絶対に稼げる方法をここに書きます。" * 4)
    assert db.get_item(conn, item.id).status == Status.BLOCKED.value


def test_approving_then_editing_sends_it_back_to_the_queue(conn, settings):
    item = generate(conn, settings, 1).items[0]
    approval.approve(conn, settings, item.id, actor="a")
    approval.edit(conn, settings, item.id, actor="a", body="差し替えた本文です。" * 6)
    stored = db.get_item(conn, item.id)
    assert stored.status == Status.PENDING_REVIEW.value, "an edit must not keep its approval"


def test_bulk_approve_never_forces(conn, settings):
    items = generate(conn, settings, 3).items
    approval.edit(conn, settings, items[0].id, actor="a", body="禁止ワード入りの本文。" * 4)
    results = approval.bulk_approve(conn, settings, [i.id for i in items], actor="a")
    assert sum(1 for r in results if r.ok) == 2
    assert db.get_item(conn, items[0].id).status == Status.BLOCKED.value


def test_revision_request_needs_a_note(conn, settings):
    item = generate(conn, settings, 1).items[0]
    assert not approval.request_revision(conn, item.id, actor="a", note="  ").ok


def test_rejecting_cancels_an_open_job(conn, settings):
    from aiworker.scheduler import planner

    item = generate(conn, settings, 1).items[0]
    approval.approve(conn, settings, item.id, actor="a")
    planner.plan(conn, settings)
    approval.reject(conn, item.id, actor="a", note="やっぱりやめる")
    job = db.list_jobs(conn, status="cancelled")
    assert job and job[0].content_id == item.id


def test_published_items_cannot_be_rejected_or_edited(conn, settings):
    item = generate(conn, settings, 1).items[0]
    db.update_item_status(conn, item.id, Status.PUBLISHED)
    assert not approval.reject(conn, item.id, actor="a").ok
    assert not approval.edit(conn, settings, item.id, actor="a", body="x").ok


def test_generation_stops_when_the_queue_is_full(conn, settings):
    settings.generation.max_pending_per_channel = 3
    generate(conn, settings, 3)
    result = generate(conn, settings, 3)
    assert not result.items and result.skipped
    assert "queue" in result.skipped[0]


# --------------------------------------------------------------------------
# approval: auto -- risk-tiered review
# --------------------------------------------------------------------------
def test_auto_platforms_approve_clean_items_without_a_human(conn, settings):
    settings.platforms["x"].approval = "auto"
    generate(conn, settings, 3)
    decisions = approval.auto_approve(conn, settings)
    assert decisions and all(d.ok for d in decisions)
    assert all(i.status == Status.APPROVED.value
               for i in db.list_items(conn, status=Status.APPROVED.value))


def test_auto_approval_still_records_who_and_why(conn, settings):
    settings.platforms["x"].approval = "auto"
    item = generate(conn, settings, 1).items[0]
    approval.auto_approve(conn, settings)
    history = db.approval_history(conn, item.id)
    assert history[-1]["actor"] == approval.AUTO_ACTOR
    assert "auto" in history[-1]["note"]


def test_auto_approval_never_clears_a_blocked_item(conn, settings):
    """`auto` moves the routine case off the queue. It must not lower the bar
    for the risky one -- a guardrail finding still waits for a person."""
    settings.platforms["x"].approval = "auto"
    item = generate(conn, settings, 1).items[0]
    approval.edit(conn, settings, item.id, actor="a", body="絶対に稼げる方法です。" * 4)
    assert db.get_item(conn, item.id).status == Status.BLOCKED.value

    approval.auto_approve(conn, settings)
    assert db.get_item(conn, item.id).status == Status.BLOCKED.value


def test_required_platforms_are_untouched(conn, settings):
    settings.platforms["x"].approval = "auto"
    settings.platforms["youtube"].approval = "required"
    generate(conn, settings, 2, channel="shorts_script", platform="youtube")
    approval.auto_approve(conn, settings)
    assert all(i.status == Status.PENDING_REVIEW.value
               for i in db.list_items(conn, platform="youtube"))


def test_default_is_still_required(conn, settings):
    generate(conn, settings, 2)
    assert approval.auto_approve(conn, settings) == []
    assert all(i.status == Status.PENDING_REVIEW.value for i in approval.queue(conn))


def test_plan_applies_the_policy(conn, settings):
    """A cron running generate -> plan -> publish needs no human step for an
    auto platform."""
    from aiworker.scheduler import planner

    settings.platforms["x"].approval = "auto"
    generate(conn, settings, 2)
    result = planner.plan(conn, settings)
    assert len(result.auto_approved) == 2
    assert len(result.scheduled) == 2


def test_an_unknown_approval_policy_is_a_config_error(settings):
    from aiworker.core.errors import ConfigError

    settings.platforms["x"].approval = "sometimes"
    with pytest.raises(ConfigError, match="approval"):
        settings.validate()
