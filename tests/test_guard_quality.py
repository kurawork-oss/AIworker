from __future__ import annotations

from aiworker.core import db
from aiworker.core.config import QualityConfig
from aiworker.core.models import ContentItem
from aiworker.guard import quality

TEXT = ("AI副業を3週間続けて分かったのは、記録を残すことが成果を決めるということでした。"
        "最初の1週間は道具選びに時間を使いすぎて、ほとんど前に進みませんでした。")


def make(body=TEXT, **kw) -> ContentItem:
    base = dict(channel="social_post", platform="x", title="", body=body)
    base.update(kw)
    return ContentItem(**base)


def test_normalisation_ignores_width_case_and_punctuation():
    assert quality.normalize("ＡＢＣ、テスト！") == quality.normalize("abc テスト")


def test_identical_text_is_a_duplicate(conn):
    db.insert_item(conn, make(uid="a", dedupe_hash=quality.fingerprint(TEXT)))
    r = quality.check(make(), QualityConfig(), conn)
    assert r.similarity == 1.0
    assert not r.ok
    assert any(i.rule == "duplicate" for i in r.issues)


def test_cosmetic_edits_do_not_escape_the_duplicate_check(conn):
    db.insert_item(conn, make(uid="a", dedupe_hash=quality.fingerprint(TEXT)))
    r = quality.check(make(body=TEXT.replace("、", "") + "!!!"), QualityConfig(), conn)
    assert not r.ok, f"similarity was only {r.similarity:.2f}"


def test_genuinely_different_text_passes(conn):
    db.insert_item(conn, make(uid="a", dedupe_hash=quality.fingerprint(TEXT)))
    other = ("味噌汁の出汁を昆布と鰹節の比率を変えて5回試した記録です。"
             "結論から言うと、水出しの時間のほうが比率より効きました。")
    r = quality.check(make(body=other), QualityConfig(), conn)
    assert r.ok and r.similarity < 0.3


def test_duplicate_check_is_scoped_to_the_channel(conn):
    db.insert_item(conn, make(uid="a", channel="note_article",
                              dedupe_hash=quality.fingerprint(TEXT)))
    r = quality.check(make(channel="social_post"), QualityConfig(), conn)
    assert r.similarity == 0.0


def test_too_short_is_rejected(conn):
    r = quality.check(make(body="短い"), QualityConfig(), conn)
    assert not r.ok and any(i.rule == "too_short" for i in r.issues)


def test_too_long_is_rejected(conn):
    r = quality.check(make(body="あ" * 400), QualityConfig(), conn)
    assert any(i.rule == "too_long" for i in r.issues)


def test_generation_artifacts_are_rejected(conn):
    r = quality.check(make(body="As an AI language model, " + TEXT), QualityConfig(), conn)
    assert not r.ok and any(i.rule == "generation_artifact" for i in r.issues)


def test_required_metadata_is_enforced(conn):
    r = quality.check(
        make(channel="shorts_script", body="台本。" * 60, meta={"description": "概要"}),
        QualityConfig(), conn,
    )
    assert not r.ok
    assert any("video_title" in i.detail for i in r.issues)


def test_stock_negative_prompt_must_carry_exclusions(conn):
    r = quality.check(
        make(channel="stock_asset", body="素材メモ。" * 8,
             meta={"prompt": "abstract", "keywords": list("abcdefghij"),
                   "negative_prompt": "blurry"}),
        QualityConfig(), conn,
    )
    assert not r.ok and any(i.rule == "weak_negative_prompt" for i in r.issues)


def test_thin_keywords_warn_but_do_not_block(conn):
    r = quality.check(
        make(channel="stock_asset", body="素材メモ。" * 8,
             meta={"prompt": "abstract", "keywords": ["a", "b"],
                   "negative_prompt": "text, watermark, logo, brand, person"}),
        QualityConfig(), conn,
    )
    assert r.ok
    assert any(i.rule == "thin_keywords" and not i.fatal for i in r.issues)


def test_similarity_threshold_is_configurable(conn):
    db.insert_item(conn, make(uid="a", dedupe_hash=quality.fingerprint(TEXT)))
    near = TEXT[:60] + "そして記録の粒度を変えたら、判断が速くなりました。"
    strict = quality.check(make(body=near), QualityConfig(max_similarity=0.3), conn)
    lax = quality.check(make(body=near), QualityConfig(max_similarity=0.99), conn)
    assert not strict.ok and lax.ok
