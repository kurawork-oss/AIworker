"""The ⑤ AI画像販売 pipeline, run end to end.

Every test runs twice: against the in-memory store (always), and against the
real schema in PostgreSQL as the service role (when AIWORKER_TEST_PG is set,
as in CI). The second run is the one that proves the pipeline only makes
moves the database allows -- a pipeline that passes in memory but trips a
database rule would fail in production on its first run.
"""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
import os
import random
import uuid
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from aiworker.cloud.pipeline import FINALS, PREVIEWS, StockPipeline
from aiworker.cloud.store import MemoryStore, StoreError
from aiworker.core.errors import ConfigError, TransientError
from aiworker.imagegen.providers import ImageResult, MockImageProvider

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("AIWORKER_TEST_PG", "")
NOW = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


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
class MemoryHarness:
    def __init__(self):
        self.store = MemoryStore()

    def set_account(self, **changes):
        for a in self.store.accounts_.values():
            a.update(changes)

    def set_item(self, item_id, **changes):
        self.store.items_[item_id].update(changes)

    def ask_revision(self, item_id, note):
        self.store.items_[item_id].update(status="needs_revision", revision_note=note)

    def stop(self):
        self.store.global_stop = True

    def item(self, item_id):
        return copy.deepcopy(self.store.items_[item_id])

    def all_items(self):
        return copy.deepcopy(sorted(self.store.items_.values(), key=lambda i: i["created_at"]))

    def has_file(self, bucket, path):
        return (bucket, path) in self.store.files

    def file(self, bucket, path):
        return self.store.files[(bucket, path)][0]

    def events(self):
        return self.store.events


@pytest.fixture(params=["memory", "postgres"])
def h(request):
    if request.param == "memory":
        yield MemoryHarness()
        return
    if not PG_URL:
        pytest.skip("AIWORKER_TEST_PG not set")
    psycopg = pytest.importorskip("psycopg")
    harness = PgHarness(psycopg)
    yield harness
    harness.close()


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


# --------------------------------------------------------------------------
# PostgreSQL harness: the real schema, the pipeline as the service role
# --------------------------------------------------------------------------
class PgStore:
    """The Store protocol over psycopg, connected the way PostgREST connects
    for a request made with the secret key: as `service_role`."""

    def __init__(self, psycopg, url, owner_id):
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        self.pg, self.Jsonb = psycopg, Jsonb
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        self.conn.execute("set role service_role")
        self.conn.execute("select set_config('request.jwt.claims', %s, false)",
                          (json.dumps({"role": "service_role"}),))
        self.owner_id = owner_id
        self.files = {}

    def _rows(self, sql, params=()):
        try:
            cur = self.conn.execute(sql, params)
        except self.pg.Error as exc:
            raise StoreError(str(exc)) from None
        return [self._plain(r) for r in cur.fetchall()] if cur.description else []

    @staticmethod
    def _plain(row):
        out = {}
        for k, v in row.items():
            if isinstance(v, uuid.UUID):
                v = str(v)
            elif isinstance(v, dt.datetime):
                v = v.isoformat()
            out[k] = v
        return out

    def _wrap(self, v):
        return self.Jsonb(v) if isinstance(v, (dict, list)) else v

    def _insert(self, table, row):
        row = {**row, "owner_id": self.owner_id}
        cols = ", ".join(row)
        marks = ", ".join(["%s"] * len(row))
        return self._rows(f"insert into public.{table} ({cols}) values ({marks}) returning *",
                          [self._wrap(v) for v in row.values()])[0]

    def controls(self):
        rows = self._rows("select * from public.controls where owner_id=%s", (self.owner_id,))
        return rows[0] if rows else {"global_stop": False}

    def business(self, code):
        rows = self._rows("select * from public.businesses where owner_id=%s and code=%s",
                          (self.owner_id, code))
        return rows[0] if rows else None

    def create_business(self, row):
        return self._insert("businesses", row)

    def accounts(self, business_id):
        return self._rows("select * from public.accounts where business_id=%s order by created_at",
                          (business_id,))

    def create_account(self, row):
        return self._insert("accounts", row)

    def items(self, business_id, *, statuses=(), created_since="", limit=200):
        sql = "select * from public.items where owner_id=%s and business_id=%s"
        params = [self.owner_id, business_id]
        if statuses:
            sql += " and status = any(%s)"
            params.append(list(statuses))
        if created_since:
            sql += " and created_at >= %s"
            params.append(created_since)
        return self._rows(sql + " order by created_at desc limit %s", params + [limit])

    def insert_item(self, row):
        return self._insert("items", row)

    def update_item(self, item_id, changes, *, expect_status=""):
        sets = ", ".join(f"{k}=%s" for k in changes)
        sql = f"update public.items set {sets} where id=%s"
        params = [self._wrap(v) for v in changes.values()] + [item_id]
        if expect_status:
            sql += " and status=%s"
            params.append(expect_status)
        rows = self._rows(sql + " returning *", params)
        return rows[0] if rows else None

    def approve(self, item_id, version, note=""):
        return self._rows("select * from public.approve_item(%s, %s, %s, false)",
                          (item_id, version, note))[0]

    def upload(self, bucket, path, data, content_type):
        self.files[(bucket, path)] = (data, content_type)

    def remove(self, bucket, paths):
        for p in paths:
            self.files.pop((bucket, p), None)

    def log(self, level, category, message, payload=None):
        self._insert("events", {"level": level, "category": category, "message": message,
                                "payload": payload or {}})

    def heartbeat(self, worker_id, info):
        self._rows("insert into public.workers(id, owner_id, kind, info) values (%s,%s,'cloud',%s) "
                   "on conflict (id) do update set last_seen_at=now(), info=excluded.info",
                   (worker_id, self.owner_id, self.Jsonb(info)))


class PgHarness:
    def __init__(self, psycopg):
        from psycopg.rows import dict_row
        self.pg = psycopg
        self.name = f"aiwp_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(PG_URL, autocommit=True) as a:
            a.execute(f'create database "{self.name}"')
        base, _, query = PG_URL.partition("?")
        self.url = base.rsplit("/", 1)[0] + "/" + self.name + ("?" + query if query else "")
        self.owner = str(uuid.uuid4())
        with psycopg.connect(self.url, autocommit=True) as c:
            c.execute((ROOT / "supabase/tests/local_supabase_stub.sql").read_text())
            for f in sorted((ROOT / "supabase/migrations").glob("*_core.sql")):
                c.execute(f.read_text())
            c.execute("insert into auth.users(id) values (%s)", (self.owner,))
            c.execute("insert into public.owners(user_id) values (%s)", (self.owner,))
        self.admin = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.store = PgStore(psycopg, self.url, self.owner)

    def close(self):
        self.store.conn.close()
        self.admin.close()
        with self.pg.connect(PG_URL, autocommit=True) as a:
            a.execute(f'drop database "{self.name}" with (force)')

    def _as_owner(self, sql, params=()):
        with self.pg.connect(self.url) as c:
            c.execute("set role authenticated")
            c.execute("select set_config('request.jwt.claims', %s, false)",
                      (json.dumps({"role": "authenticated", "sub": self.owner}),))
            c.execute(sql, params)

    def set_account(self, **changes):
        sets = ", ".join(f"{k}=%s" for k in changes)
        self.admin.execute(f"update public.accounts set {sets}", list(changes.values()))

    def set_item(self, item_id, **changes):
        # As the owner's phone would: through the same table rules.
        sets = ", ".join(f"{k}=%s" for k in changes)
        self.admin.execute(f"update public.items set {sets} where id=%s",
                           list(changes.values()) + [item_id])

    def ask_revision(self, item_id, note):
        version = self.item(item_id)["version"]
        self._as_owner("select public.decide_item(%s, 'revise', %s, %s)", (item_id, version, note))

    def stop(self):
        self._as_owner("select public.set_global_stop(true, 'テスト停止')")

    def item(self, item_id):
        return PgStore._plain(self.admin.execute(
            "select * from public.items where id=%s", (item_id,)).fetchone())

    def all_items(self):
        return [PgStore._plain(r) for r in self.admin.execute(
            "select * from public.items order by created_at").fetchall()]

    def has_file(self, bucket, path):
        return (bucket, path) in self.store.files

    def file(self, bucket, path):
        return self.store.files[(bucket, path)][0]

    def events(self):
        return self.admin.execute("select level, category, message, payload from public.events "
                                  "order by id").fetchall()
