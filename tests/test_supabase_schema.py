"""The v2 schema's invariants, exercised as the real roles against PostgreSQL.

These run against a live database (set AIWORKER_TEST_PG to a libpq URL, e.g.
`postgresql://postgres@/postgres?host=/var/tmp&port=54329`) and are skipped
otherwise. CI provides one through a service container.

Each test plays a role -- the owner on their phone, a stranger who signed up,
the pipeline's service role -- because the point of putting the rules in the
database is that NO client can get around them, and that is only shown by
trying from each one.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

PG_URL = os.environ.get("AIWORKER_TEST_PG", "")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not PG_URL, reason="AIWORKER_TEST_PG not set")

OWNER = str(uuid.uuid4())
STRANGER = str(uuid.uuid4())


@pytest.fixture(scope="module")
def dbname():
    """A fresh database per test module: stub + migrations, as Supabase would."""
    name = f"aiw_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    url = _with_db(PG_URL, name)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute((ROOT / "supabase/tests/local_supabase_stub.sql").read_text())
        for f in sorted((ROOT / "supabase/migrations").glob("*_core.sql")):
            c.execute(f.read_text())
        c.execute("grant all on all tables in schema public to anon, authenticated, service_role")
        c.execute("grant all on all sequences in schema public to anon, authenticated, service_role")
        c.execute("insert into auth.users(id) values (%s), (%s)", (OWNER, STRANGER))
        c.execute("insert into public.owners(user_id) values (%s)", (OWNER,))
    yield url
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f'drop database "{name}" with (force)')


def _with_db(url: str, name: str) -> str:
    base, _, query = url.partition("?")
    base = base.rsplit("/", 1)[0] + "/" + name
    return base + ("?" + query if query else "")


class As:
    """Run statements as a given role, the way PostgREST would for a request."""

    def __init__(self, url: str, role: str, sub: str | None = None):
        self.conn = psycopg.connect(url)
        self.role, self.sub = role, sub

    def __enter__(self):
        claims = {"role": self.role}
        if self.sub:
            claims["sub"] = self.sub
        self.conn.execute(f"set role {self.role}")
        self.conn.execute("select set_config('request.jwt.claims', %s, false)",
                          (json.dumps(claims),))
        return self

    def q(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount

    def one(self, sql, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()


def admin(url):
    return psycopg.connect(url, autocommit=True)


@pytest.fixture
def world(dbname):
    """One business, two accounts (required / auto), as the owner set them up."""
    with admin(dbname) as c:
        c.execute("truncate public.items, public.approvals, public.jobs, public.events, "
                  "public.controls, public.accounts, public.businesses, "
                  "public.settings_versions cascade")
        biz = c.execute(
            "insert into public.businesses(owner_id, code, name) values (%s,'stock','画像販売') "
            "returning id", (OWNER,)).fetchone()[0]
        manual = c.execute(
            "insert into public.accounts(owner_id, business_id, platform, approval_policy) "
            "values (%s,%s,'adobe_stock','required') returning id", (OWNER, biz)).fetchone()[0]
        auto = c.execute(
            "insert into public.accounts(owner_id, business_id, platform, approval_policy) "
            "values (%s,%s,'adobe_stock','auto') returning id", (OWNER, biz)).fetchone()[0]
    return {"url": dbname, "biz": biz, "manual": manual, "auto": auto}


def new_item(world, *, account="manual", status="pending_review", blocking=False):
    with admin(world["url"]) as c:
        return c.execute(
            "insert into public.items(owner_id, business_id, account_id, kind, title, body, "
            "status, blocking) values (%s,%s,%s,'stock_image','t','b',%s,%s) returning id",
            (OWNER, world["biz"], world[account], status, blocking)).fetchone()[0]


def item(world, item_id):
    with admin(world["url"]) as c:
        c.row_factory = psycopg.rows.dict_row
        return c.execute("select * from public.items where id=%s", (item_id,)).fetchone()


def approvals(world, item_id):
    with admin(world["url"]) as c:
        return [r[0] for r in c.execute(
            "select action from public.approvals where item_id=%s order by id", (item_id,))]


# --------------------------------------------------------------------------
# who can see what
# --------------------------------------------------------------------------
def test_owner_sees_their_items(world):
    new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        assert s.one("select count(*) from public.items")[0] == 1


def test_a_stranger_who_signs_up_sees_nothing(world):
    """Supabase lets anyone sign up unless told otherwise. Being signed in must
    grant nothing -- only the row in `owners` does."""
    new_item(world)
    with As(world["url"], "authenticated", STRANGER) as s:
        assert s.one("select count(*) from public.items")[0] == 0
        assert s.one("select count(*) from public.businesses")[0] == 0


def test_a_stranger_cannot_store_their_own_rows_either(world):
    """Reads are already fenced by owner_id. What `owners` adds is that a
    stranger cannot use this database at all -- not even to write rows under
    their own id. (A mutation test caught that this case was untested: with
    is_owner() forced true, every other test still passed.)"""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with As(world["url"], "authenticated", STRANGER) as s:
            s.q("insert into public.businesses(owner_id, code, name) values (%s,'x','x')",
                (STRANGER,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with As(world["url"], "authenticated", STRANGER) as s:
            s.q("select public.set_global_stop(true, 'mischief')")


def test_anonymous_visitors_see_nothing(world):
    new_item(world)
    with As(world["url"], "anon") as s:
        assert s.one("select count(*) from public.items")[0] == 0


# --------------------------------------------------------------------------
# approved has exactly one writer
# --------------------------------------------------------------------------
def test_the_owner_cannot_write_status_directly(world):
    """No UPDATE policy on items: the browser cannot set a status, only call
    the decision functions."""
    i = new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        assert s.q("update public.items set status='approved' where id=%s", (i,)) == 0
    assert item(world, i)["status"] == "pending_review"


def test_even_the_service_role_cannot_write_approved_directly(world):
    """The service role bypasses RLS. It does not bypass the trigger."""
    i = new_item(world)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="approve_item"):
        with As(world["url"], "service_role") as s:
            s.q("update public.items set status='approved', approved_version=version "
                "where id=%s", (i,))


def test_new_items_cannot_start_approved(world):
    with pytest.raises(psycopg.errors.CheckViolation, match="unapproved"):
        with admin(world["url"]) as c:
            c.execute("insert into public.items(owner_id,business_id,kind,status) "
                      "values (%s,%s,'x','approved')", (OWNER, world["biz"]))


# --------------------------------------------------------------------------
# approving
# --------------------------------------------------------------------------
def test_owner_approves_through_the_function(world):
    i = new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    row = item(world, i)
    assert row["status"] == "approved"
    assert row["approved_version"] == row["version"] == 1
    assert row["approved_by"] == f"user:{OWNER}"
    assert approvals(world, i) == ["approve"]


def test_a_double_tap_approves_once(world):
    i = new_item(world)
    for _ in range(2):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))
    assert approvals(world, i) == ["approve"]


def test_approving_a_stale_version_is_refused(world):
    """If the content changed while the person was reading it, they approved
    something else."""
    i = new_item(world)
    with admin(world["url"]) as c:
        c.execute("update public.items set body='changed' where id=%s", (i,))
    with pytest.raises(psycopg.errors.SerializationFailure, match="changed since you viewed"):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))


def test_a_stranger_cannot_approve(world):
    i = new_item(world)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with As(world["url"], "authenticated", STRANGER) as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))


def test_a_blocked_item_needs_force_and_a_reason(world):
    i = new_item(world, status="blocked", blocking=True)
    with pytest.raises(psycopg.errors.CheckViolation, match="force"):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))
    with pytest.raises(psycopg.errors.CheckViolation, match="reason"):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.approve_item(%s, 1, '  ', true)", (i,))
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '文脈上問題なし', true)", (i,))
    assert approvals(world, i) == ["approve_override"]


# --------------------------------------------------------------------------
# auto-approval by the pipeline
# --------------------------------------------------------------------------
def test_the_pipeline_auto_approves_a_clean_item_on_an_auto_account(world):
    i = new_item(world, account="auto", status="checking")
    with As(world["url"], "service_role") as s:
        s.q("select public.approve_item(%s, 1, 'policy: auto', false)", (i,))
    assert item(world, i)["status"] == "approved"
    assert approvals(world, i) == ["auto_approve"]


def test_the_pipeline_cannot_auto_approve_on_a_required_account(world):
    i = new_item(world, account="manual")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="not enabled"):
        with As(world["url"], "service_role") as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))


def test_the_pipeline_cannot_auto_approve_a_blocked_item(world):
    i = new_item(world, account="auto", status="blocked", blocking=True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="blocked"):
        with As(world["url"], "service_role") as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))


def test_the_pipeline_cannot_force(world):
    i = new_item(world, account="auto", status="blocked", blocking=True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with As(world["url"], "service_role") as s:
            s.q("select public.approve_item(%s, 1, 'trust me', true)", (i,))


def test_the_pipeline_cannot_auto_approve_a_sample(world):
    """Output from a stand-in generator is never real work."""
    i = new_item(world, account="auto", status="checking")
    with admin(world["url"]) as c:
        c.execute("update public.items set is_sample=true where id=%s", (i,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="sample"):
        with As(world["url"], "service_role") as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))


def test_a_sample_a_person_approves_never_becomes_an_upload_task(world):
    i = new_item(world, status="checking")
    with admin(world["url"]) as c:
        c.execute("update public.items set is_sample=true, final_path='o/i/final.jpg' "
                  "where id=%s", (i,))
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    assert item(world, i)["status"] == "approved"


def test_global_stop_halts_auto_approval(world):
    i = new_item(world, account="auto", status="checking")
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.set_global_stop(true, 'テスト停止')")
    with pytest.raises(psycopg.errors.CheckViolation, match="global stop"):
        with As(world["url"], "service_role") as s:
            s.q("select public.approve_item(%s, 1, '', false)", (i,))


def test_stopping_needs_a_reason(world):
    with pytest.raises(psycopg.errors.CheckViolation):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.set_global_stop(true, '')")


# --------------------------------------------------------------------------
# changes after approval
# --------------------------------------------------------------------------
def test_editing_an_approved_item_invalidates_the_approval(world):
    i = new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    with admin(world["url"]) as c:
        c.execute("update public.items set meta = meta || '{\"keywords\":[\"x\"]}' where id=%s",
                  (i,))
    row = item(world, i)
    assert row["status"] == "pending_review", "an edited item must not stay approved"
    assert row["approved_version"] is None
    assert row["version"] == 2
    assert approvals(world, i)[-1] == "invalidate"


def test_a_new_preview_image_also_invalidates(world):
    """The image IS the product for a stock asset."""
    i = new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    with admin(world["url"]) as c:
        c.execute("update public.items set preview_path='other.png' where id=%s", (i,))
    assert item(world, i)["status"] == "pending_review"


def test_writing_the_final_upscaled_file_does_not_invalidate(world):
    """final_path is derived deterministically from the approved preview; it
    is not new content."""
    i = new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    with admin(world["url"]) as c:
        c.execute("update public.items set final_path='final.png', status='awaiting_manual' "
                  "where id=%s", (i,))
    assert item(world, i)["status"] == "awaiting_manual"


# --------------------------------------------------------------------------
# transitions, revision, manual completion
# --------------------------------------------------------------------------
def test_illegal_transitions_are_refused(world):
    i = new_item(world)
    with pytest.raises(psycopg.errors.CheckViolation, match="illegal status transition"):
        with admin(world["url"]) as c:
            c.execute("update public.items set status='published' where id=%s", (i,))


def test_published_is_terminal(world):
    i = new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    with admin(world["url"]) as c:
        c.execute("update public.items set status='awaiting_manual' where id=%s", (i,))
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.mark_manual_done(%s, 'https://stock.adobe.com/x')", (i,))
    with pytest.raises(psycopg.errors.CheckViolation):
        with admin(world["url"]) as c:
            c.execute("update public.items set status='draft' where id=%s", (i,))


def test_revision_needs_a_note(world):
    i = new_item(world)
    with pytest.raises(psycopg.errors.CheckViolation, match="note"):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.decide_item(%s, 'revise', 1, '')", (i,))
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.decide_item(%s, 'revise', 1, '色味を明るく')", (i,))
    assert item(world, i)["status"] == "needs_revision"


def test_hold_and_reject(world):
    a, b = new_item(world), new_item(world)
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.decide_item(%s, 'hold', 1, '')", (a,))
        s.q("select public.decide_item(%s, 'reject', 1, '重複')", (b,))
    assert item(world, a)["status"] == "on_hold"
    assert item(world, b)["status"] == "rejected"


def test_only_a_person_can_reject(world):
    i = new_item(world)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with As(world["url"], "service_role") as s:
            s.q("select public.decide_item(%s, 'reject', 1, '')", (i,))


def test_manual_completion_needs_evidence_and_the_right_state(world):
    i = new_item(world)
    with pytest.raises(psycopg.errors.CheckViolation, match="not waiting"):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.mark_manual_done(%s, 'done')", (i,))
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    with admin(world["url"]) as c:
        c.execute("update public.items set status='awaiting_manual' where id=%s", (i,))
    with pytest.raises(psycopg.errors.CheckViolation, match="evidence"):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("select public.mark_manual_done(%s, '')", (i,))
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.mark_manual_done(%s, 'https://stock.adobe.com/asset/1')", (i,))
    row = item(world, i)
    assert row["status"] == "published"
    assert row["external_url"] == "https://stock.adobe.com/asset/1"


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
def test_the_same_job_cannot_be_enqueued_twice(world):
    with admin(world["url"]) as c:
        c.execute("insert into public.jobs(owner_id, kind, idempotency_key) "
                  "values (%s,'image','img:abc')", (OWNER,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("insert into public.jobs(owner_id, kind, idempotency_key) "
                      "values (%s,'image','img:abc')", (OWNER,))


def test_two_runners_never_claim_the_same_job(world):
    with admin(world["url"]) as c:
        c.execute("insert into public.jobs(owner_id, kind, idempotency_key) "
                  "values (%s,'image','only-one')", (OWNER,))
    with As(world["url"], "service_role") as a:
        first = a.q("select id from public.claim_job('cloud','runner-a',600)")
    with As(world["url"], "service_role") as b:
        second = b.q("select id from public.claim_job('cloud','runner-b',600)")
    assert len(first) == 1 and second == []


def test_an_abandoned_lease_is_reclaimed(world):
    with admin(world["url"]) as c:
        c.execute("insert into public.jobs(owner_id, kind, idempotency_key, status, "
                  "locked_until, attempts) values (%s,'image','stale','running', "
                  "now() - interval '1 minute', 1)", (OWNER,))
    with As(world["url"], "service_role") as s:
        assert len(s.q("select id from public.claim_job('cloud','runner-c',600)")) == 1


def test_a_job_out_of_attempts_is_not_claimed(world):
    with admin(world["url"]) as c:
        c.execute("insert into public.jobs(owner_id, kind, idempotency_key, attempts, "
                  "max_attempts) values (%s,'image','spent',3,3)", (OWNER,))
    with As(world["url"], "service_role") as s:
        assert s.q("select id from public.claim_job('cloud','r',600)") == []


# --------------------------------------------------------------------------
# settings history
# --------------------------------------------------------------------------
def test_settings_changes_are_versioned(world):
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("update public.accounts set daily_limit = 2 where id=%s", (world["manual"],))
    with admin(world["url"]) as c:
        rows = c.execute(
            "select before->>'daily_limit', after->>'daily_limit' from public.settings_versions "
            "where entity_id=%s order by id", (world["manual"],)).fetchall()
    assert rows[-1] == ("1", "2")


def test_daily_limit_has_a_sanity_ceiling(world):
    with pytest.raises(psycopg.errors.CheckViolation):
        with As(world["url"], "authenticated", OWNER) as s:
            s.q("update public.accounts set daily_limit = 500 where id=%s", (world["manual"],))


def test_approving_an_item_with_a_finished_file_puts_it_on_the_upload_list(world):
    """Approve on the phone, and it is immediately waiting to be uploaded --
    not parked as 'approved' until the next scheduled run."""
    i = new_item(world, status="checking")
    with admin(world["url"]) as c:
        c.execute("update public.items set final_path='o/i/final.jpg' where id=%s", (i,))
    with As(world["url"], "authenticated", OWNER) as s:
        s.q("select public.approve_item(%s, 1, '', false)", (i,))
    row = item(world, i)
    assert row["status"] == "awaiting_manual"
    assert row["approved_version"] == row["version"]
