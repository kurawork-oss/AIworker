"""Two stores behind one test harness: in memory (always) and the real
schema in PostgreSQL as the service role (when AIWORKER_TEST_PG is set).

Every cloud test runs on both. The second run proves the pipeline makes only
moves the database allows.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import uuid
from pathlib import Path

import pytest

from aiworker.cloud.store import MemoryStore, StoreError

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("AIWORKER_TEST_PG", "")
NOW = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def harness(kind: str):
    """Generator for a parametrised fixture: yields a ready harness, cleans up."""
    if kind == "memory":
        yield MemoryHarness()
        return
    if not PG_URL:
        pytest.skip("AIWORKER_TEST_PG not set")
    psycopg = pytest.importorskip("psycopg")
    h = PgHarness(psycopg)
    yield h
    h.close()


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

    def approve(self, item_id):
        """The owner tapping 承認 (approve_item() as a person, not auto)."""
        from aiworker.cloud.store import TEXT_KINDS
        it = self.store.items_[item_id]
        assert it["status"] in ("checking", "pending_review", "blocked", "on_hold")
        if it["blocking"] or it["status"] == "blocked":
            raise StoreError("guardrails block this item; pass force with a reason to override")
        acc = self.store.accounts_.get(it.get("account_id") or "") or {}
        it.update(status="approved", approved_version=it["version"])
        if ((it["final_path"] or it["kind"] in TEXT_KINDS) and not it["is_sample"]
                and acc.get("publish_method", "manual") == "manual"):
            it["status"] = "awaiting_manual"

    def add_program(self, **fields):
        row = {"id": str(uuid.uuid4()), "owner_id": self.store.owner_id, "asp": "a8",
               "pixel_url": "", "facts": "", "media": ["blog"], "status": "active", **fields}
        self.store.programs_.append(row)
        return row["id"]


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

    def items(self, business_id, *, statuses=(), created_since="", published_since="",
              limit=200):
        sql = "select * from public.items where owner_id=%s and business_id=%s"
        params = [self.owner_id, business_id]
        if statuses:
            sql += " and status = any(%s)"
            params.append(list(statuses))
        if created_since:
            sql += " and created_at >= %s"
            params.append(created_since)
        if published_since:
            sql += " and published_at >= %s"
            params.append(published_since)
        return self._rows(sql + " order by created_at desc limit %s", params + [limit])

    def programs(self):
        return self._rows("select * from public.affiliate_programs where owner_id=%s "
                          "and status='active' order by created_at", (self.owner_id,))

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
            for f in sorted((ROOT / "supabase/migrations").glob("*.sql")):
                if "storage" in f.name:  # needs Supabase's storage schema
                    continue
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

    def approve(self, item_id):
        version = self.item(item_id)["version"]
        self._as_owner("select public.approve_item(%s, %s, '', false)", (item_id, version))

    def add_program(self, **fields):
        row = {"owner_id": self.owner, "asp": "a8", "facts": "", **fields}
        cols = ", ".join(row)
        marks = ", ".join(["%s"] * len(row))
        return str(self.admin.execute(
            f"insert into public.affiliate_programs ({cols}) values ({marks}) returning id",
            list(row.values())).fetchone()["id"])
