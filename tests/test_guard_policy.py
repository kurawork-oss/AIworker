from __future__ import annotations

from aiworker.core.config import PlatformConfig, PolicyConfig
from aiworker.core.models import ContentItem
from aiworker.guard import policy


def item(**kw) -> ContentItem:
    base = dict(channel="social_post", platform="x", title="見出し", body="本文です。")
    base.update(kw)
    return ContentItem(**base)


def test_configured_banned_term_blocks(settings):
    r = policy.check(item(body="これは禁止ワードを含みます"), settings.policy)
    assert r.blocking
    assert any("禁止ワード" in f.detail for f in r.findings)


def test_trademark_blocks(settings):
    r = policy.check(item(title="Ghibli style landscape"), settings.policy)
    assert r.blocking


def test_guaranteed_income_claim_blocks():
    r = policy.check(item(body="この方法なら絶対に稼げます"), PolicyConfig())
    assert r.blocking
    assert any(f.rule == "unsupported_claim" for f in r.findings)


def test_medical_claim_blocks():
    r = policy.check(item(body="このサプリでうつ病が治る"), PolicyConfig())
    assert r.blocking


def test_affiliate_link_without_disclosure_blocks():
    r = policy.check(item(body="詳細はこちら https://px.a8.net/svt/abc"), PolicyConfig())
    assert r.blocking
    assert any(f.rule == "affiliate_disclosure" for f in r.findings)


def test_affiliate_link_with_pr_marker_passes():
    r = policy.check(item(body="#PR 詳細はこちら https://px.a8.net/svt/abc"), PolicyConfig())
    assert not r.blocking


def test_affiliate_url_in_metadata_also_requires_disclosure():
    r = policy.check(
        item(meta={"affiliate_url": "https://px.a8.net/svt/abc"}), PolicyConfig()
    )
    assert r.blocking


def test_negative_prompt_is_not_scanned_as_content():
    """A stock negative prompt says what to EXCLUDE. Reading "no celebrity, no
    logo" as a violation would block exactly the safest prompts."""
    r = policy.check(
        item(channel="stock_asset",
             meta={"prompt": "abstract gradient, copy space",
                   "negative_prompt": "text, watermark, logo, brand, celebrity, real person"}),
        PolicyConfig(),
    )
    assert not r.blocking, [f.detail for f in r.findings]


def test_positive_prompt_is_scanned():
    r = policy.check(
        item(channel="stock_asset", meta={"prompt": "portrait of a famous actor"}),
        PolicyConfig(),
    )
    assert r.blocking


def test_ai_disclosure_required_and_missing(settings):
    cfg = settings.platform("youtube")
    r = policy.check(item(platform="youtube"), settings.policy, cfg)
    assert r.blocking
    assert any(f.rule == "ai_disclosure" for f in r.findings)


def test_ai_disclosure_accepts_a_rephrasing(settings):
    cfg = settings.platform("youtube")
    it = item(platform="youtube", body="本編です。\n\nこの動画はAIで生成した素材を使用しています")
    assert policy.has_ai_disclosure(it, cfg)
    assert not policy.check(it, settings.policy, cfg).blocking


def test_ensure_ai_disclosure_appends_once(settings):
    cfg = settings.platform("youtube")
    it = item(platform="youtube")
    assert policy.ensure_ai_disclosure(it, cfg) is True
    assert cfg.disclosure_text in it.body
    assert policy.ensure_ai_disclosure(it, cfg) is False, "must not append twice"
    assert it.body.count(cfg.disclosure_text) == 1


def test_ensure_disclosure_targets_description_when_present(settings):
    cfg = settings.platform("youtube")
    it = item(platform="youtube", channel="shorts_script", meta={"description": "概要欄"})
    assert policy.ensure_ai_disclosure(it, cfg)
    assert cfg.disclosure_text in it.meta["description"]


def test_disclosure_not_required_elsewhere(settings):
    assert policy.ensure_ai_disclosure(item(), settings.platform("x")) is False


def test_link_density_is_a_warning_not_a_block():
    body = " ".join(f"https://example.com/{i}" for i in range(5))
    r = policy.check(item(body=body), PolicyConfig(require_affiliate_disclosure=False))
    assert not r.blocking
    assert any(f.rule == "link_density" for f in r.warnings)


def test_invalid_regex_in_config_warns_instead_of_crashing():
    r = policy.check(item(), PolicyConfig(banned_patterns=["([unclosed"]))
    assert not r.blocking
    assert any(f.rule == "bad_config" for f in r.findings)
