"""The ⑤ AI画像販売 pipeline, run end to end.

Every test runs twice: against the in-memory store (always), and against the
real schema in PostgreSQL as the service role (when AIWORKER_TEST_PG is set,
as in CI). The second run is the one that proves the pipeline only makes
moves the database allows -- a pipeline that passes in memory but trips a
database rule would fail in production on its first run.
"""

from __future__ import annotations

import io
import json
import random

import pytest
from cloud_harness import NOW, harness
from PIL import Image, ImageDraw

from aiworker.cloud.pipeline import FINALS, PREVIEWS, StockPipeline
from aiworker.core.errors import ConfigError, TransientError
from aiworker.imagegen.providers import ImageResult, MockImageProvider


# --------------------------------------------------------------------------
# stand-ins for Gemini and Cloudflare
# --------------------------------------------------------------------------
class FakeLLM:
    name = "fake-llm"

    def __init__(self, **override):
        self.override = override
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        n = len(self.requests)
        out = {"title": f"Calm abstract background {n}", "body": "抽象背景のメモ",
               "prompt": f"soft gradient abstract background variation {n}, no text",
               "negative_prompt": "text, watermark",
               "keywords": ["Abstract", "background", "abstract", "gradient"],
               "description": "Abstract background with copy space."}
        out.update(self.override)
        return out


def picture(seed: int) -> bytes:
    """A busy 1024x1024 image that differs per seed, like real output."""
    rng = random.Random(seed)
    img = Image.new("RGB", (1024, 1024), tuple(rng.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = rng.randrange(1024), rng.randrange(1024)
        draw.rectangle([x, y, x + rng.randrange(40, 400), y + rng.randrange(40, 400)],
                       fill=tuple(rng.randrange(256) for _ in range(3)))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class FakeImages:
    name = "fake-images"
    cost_per_image_usd = 0.0
    is_sample = False

    def __init__(self, *, fail=None, same=False, during=None):
        self.fail = list(fail or [])   # exceptions / ImageResults to return first
        self.same = same
        self.during = during           # called mid-generation (to cancel, etc.)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        if self.during:
            self.during()
        if self.fail:
            f = self.fail.pop(0)
            if isinstance(f, Exception):
                raise f
            return f
        return ImageResult(True, picture(1 if self.same else request.seed))


# --------------------------------------------------------------------------
# the two stores, behind one harness
# --------------------------------------------------------------------------
@pytest.fixture(params=["memory", "postgres"])
def h(request):
    yield from harness(request.param)


def run(h, llm=None, images=None, seed=7, now=NOW):
    pipe = StockPipeline(h.store, llm or FakeLLM(), images or FakeImages(),
                         rng=random.Random(seed), now=now)
    return pipe.run()


# --------------------------------------------------------------------------
# the happy paths
# --------------------------------------------------------------------------
def test_first_run_sets_up_the_business_and_makes_an_upload_task(h):
    report = run(h)
    assert report.created == 1 and report.auto_approved == 1, report.summary()
    [item] = h.all_items()
    assert item["status"] == "awaiting_manual"
    assert item["is_sample"] is False
    assert item["approved_version"] == item["version"]
    final = Image.open(io.BytesIO(h.file(FINALS, item["final_path"])))
    assert final.width * final.height >= 4_000_000
    assert final.format == "JPEG"
    preview = Image.open(io.BytesIO(h.file(PREVIEWS, item["preview_path"])))
    assert max(preview.size) <= 1024
    assert item["preview_path"].startswith(h.store.owner_id + "/")


def test_metadata_is_tidied_for_adobe(h):
    run(h)
    [item] = h.all_items()
    meta = item["meta"]
    assert meta["keywords"] == ["abstract", "background", "gradient"]
    for must in ("text", "watermark", "logo", "brand", "real person"):
        assert must in meta["negative_prompt"]
    assert meta["ai_generated"] is True
    assert meta["category"] is None  # the fake model gave none: left blank, not guessed


def test_titles_and_categories_fit_adobes_csv(h):
    long = "Soft pastel gradient, abstract background with generous copy space for banners"
    run(h, llm=FakeLLM(title=long, category="8"))
    [item] = h.all_items()
    assert "," not in item["title"] and len(item["title"]) <= 70
    assert item["title"].startswith("Soft pastel gradient abstract")
    assert item["meta"]["category"] == 8


def test_the_daily_limit_is_the_owners_publication_budget(h):
    run(h)
    again = run(h, seed=8)
    assert again.created == 0
    h.set_account(daily_limit=3)
    more = run(h, seed=9)
    assert more.created == 2  # per_run, and 1 + 2 = 3
    assert len(h.all_items()) == 3


def test_stock_items_need_a_person_when_the_owner_says_so(h):
    h_run = run(h)  # creates the account
    assert h_run.auto_approved == 1
    h.set_account(approval_policy="required", daily_limit=5)
    report = run(h, seed=8)
    assert report.to_review == 2 and report.auto_approved == 0
    assert sorted(i["status"] for i in h.all_items()) == [
        "awaiting_manual", "pending_review", "pending_review"]


# --------------------------------------------------------------------------
# what must never happen
# --------------------------------------------------------------------------
def test_samples_are_never_auto_approved(h):
    report = run(h, images=MockImageProvider())
    [item] = h.all_items()
    assert item["is_sample"] is True
    assert item["status"] == "pending_review"
    assert report.auto_approved == 0


def test_a_risky_brief_is_blocked_before_any_image_is_drawn(h):
    images = FakeImages()
    report = run(h, llm=FakeLLM(prompt="castle in the style of Ghibli"), images=images)
    [item] = h.all_items()
    assert item["status"] == "blocked" and item["blocking"] is True
    assert images.calls == 0
    assert report.blocked == 1


def test_a_near_duplicate_image_is_blocked(h):
    h_first = run(h, images=FakeImages(same=True))
    assert h_first.auto_approved == 1
    h.set_account(daily_limit=5)
    report = run(h, images=FakeImages(same=True), seed=8)
    assert report.blocked == 2
    dupes = [i for i in h.all_items() if i["status"] == "blocked"]
    assert all("ほぼ同一" in " ".join(i["quality_report"]["issues"]) for i in dupes)


def test_global_stop_does_nothing_at_all(h):
    run(h)
    h.stop()
    h.set_account(daily_limit=5)
    report = run(h, seed=8)
    assert report.stopped
    assert len(h.all_items()) == 1


def test_pausing_the_account_stops_its_work(h):
    run(h)
    h.set_account(status="paused", daily_limit=5)
    report = run(h, seed=8)
    assert report.stopped and len(h.all_items()) == 1


def test_unchecked_terms_stop_the_work(h):
    run(h)
    h.set_account(terms_status="unverified", daily_limit=5)
    report = run(h, seed=8)
    assert "規約" in report.stopped and len(h.all_items()) == 1


def test_logs_carry_counts_not_content(h):
    run(h)
    text = json.dumps([e for e in h.events() if e["category"] == "pipeline"], ensure_ascii=False)
    assert "Calm abstract" not in text and "gradient" not in text
    assert '"created": 1' in text


# --------------------------------------------------------------------------
# failures and leftovers
# --------------------------------------------------------------------------
def test_a_busy_image_service_is_retried_next_run(h):
    report = run(h, images=FakeImages(fail=[TransientError("429 busy")]))
    [item] = h.all_items()
    assert item["status"] == "image_queued"
    assert item["meta"]["image_attempts"] == 1
    assert report.errors

    retry = run(h, seed=8)
    assert retry.retried == 1 and retry.auto_approved == 1
    assert h.item(item["id"])["status"] == "awaiting_manual"


def test_an_image_that_keeps_failing_is_given_up(h):
    bad = ImageResult(False, message="NSFW filter")
    run(h, images=FakeImages(fail=[bad]))
    run(h, images=FakeImages(fail=[bad]), seed=8)
    run(h, images=FakeImages(fail=[bad]), seed=9)
    first, *replacement = h.all_items()
    assert first["status"] == "failed"
    assert first["meta"]["image_attempts"] == 3
    # The day's budget is for usable work: a given-up item frees its slot.
    assert [i["status"] for i in replacement] == ["awaiting_manual"]


def test_a_missing_key_stops_the_run_and_keeps_the_item(h):
    report = run(h, images=FakeImages(fail=[ConfigError("CLOUDFLARE_API_TOKEN is not set")]))
    assert any("設定エラー" in e for e in report.errors)
    [item] = h.all_items()
    assert item["status"] == "image_queued"
    assert any(e["level"] == "error" for e in h.events())


def test_cancelling_from_the_phone_mid_render_wins(h):
    def cancel():
        for i in h.all_items():
            if i["status"] == "image_queued":
                h.set_item(i["id"], status="cancelled")

    run(h, images=FakeImages(during=cancel))
    [item] = h.all_items()
    assert item["status"] == "cancelled"
    base = f"{h.store.owner_id}/{item['id']}"
    assert not h.has_file(FINALS, f"{base}/final.jpg")
    assert not h.has_file(PREVIEWS, f"{base}/preview.jpg")


def test_a_revision_request_is_redone_and_comes_back_to_the_person(h):
    run(h)
    h.set_account(approval_policy="required")
    [item] = h.all_items()
    h.set_item(item["id"], status="pending_review")  # as if it had waited for review
    h.ask_revision(item["id"], "もっと明るい色にしてください")
    h.set_account(approval_policy="auto")

    llm = FakeLLM()
    report = run(h, llm=llm, seed=8)
    assert report.revised == 1
    assert "もっと明るい色" in llm.requests[0].prompt
    after = h.item(item["id"])
    assert after["status"] == "pending_review"  # a person asked, a person checks
    assert after["meta"]["revisions"] == 1
    assert after["version"] > item["version"]


def test_finished_files_are_cleaned_up(h):
    run(h)
    [item] = h.all_items()
    path = item["final_path"]
    h.set_item(item["id"], status="published")
    report = run(h, seed=8)
    assert report.cleaned == 1
    assert not h.has_file(FINALS, path)
    assert h.item(item["id"])["final_path"] == ""
    assert h.has_file(PREVIEWS, item["preview_path"])
