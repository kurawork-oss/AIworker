"""Where the pipeline reads and writes: Supabase in production, memory in tests.

The pipeline talks to a :class:`Store`, never to HTTP or SQL directly, so the
same pipeline code runs against

* :class:`RestStore`   - Supabase's REST and Storage APIs with the secret
                         (service-role) key, from GitHub Actions;
* :class:`MemoryStore` - an in-process stand-in for tests and ``--dry-run``.

The memory store mirrors the few database rules the pipeline leans on
(version bump on a content edit, approval only through :meth:`Store.approve`,
an approved finished file going straight to the upload list) so a pipeline
bug shows up in the fast tests too. The real rules live in the database and
are tested there (tests/test_supabase_schema.py); the pipeline is run against
them as well when a test database is available.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from ..core.errors import ConfigError, TransientError

Row = dict[str, Any]


class StoreError(RuntimeError):
    """The store refused a write (a database rule, a stale version)."""


class Store(Protocol):
    owner_id: str

    def controls(self) -> Row: ...
    def business(self, code: str) -> Row | None: ...
    def create_business(self, row: Row) -> Row: ...
    def accounts(self, business_id: str) -> list[Row]: ...
    def create_account(self, row: Row) -> Row: ...
    def items(self, business_id: str, *, statuses: Iterable[str] = (),
              created_since: str = "", limit: int = 200) -> list[Row]: ...
    def insert_item(self, row: Row) -> Row: ...
    def update_item(self, item_id: str, changes: Row, *,
                    expect_status: str = "") -> Row | None: ...
    def approve(self, item_id: str, version: int, note: str = "") -> Row: ...
    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None: ...
    def remove(self, bucket: str, paths: list[str]) -> None: ...
    def log(self, level: str, category: str, message: str, payload: Row | None = None) -> None: ...
    def heartbeat(self, worker_id: str, info: Row) -> None: ...


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


# --------------------------------------------------------------------------
# Supabase
# --------------------------------------------------------------------------
class RestStore:
    """Supabase over plain HTTPS, so the pipeline needs no client library.

    Authentication is the project's secret key. A new-style ``sb_secret_``
    key goes in the ``apikey`` header only -- the platform rejects it as a
    bearer token -- while a legacy JWT service key goes in both.
    """

    def __init__(self, url: str, key: str, owner_id: str, *, timeout: int = 60,
                 opener: Callable[..., Any] | None = None):
        if not url or not key or not owner_id:
            raise ConfigError("SUPABASE_URL, SUPABASE_SECRET_KEY and AIWORKER_OWNER_ID are required")
        self.url = url.rstrip("/")
        self.owner_id = owner_id
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._auth = {"apikey": key}
        if not key.startswith("sb_"):
            self._auth["Authorization"] = f"Bearer {key}"

    @classmethod
    def from_env(cls) -> RestStore:
        return cls(os.environ.get("SUPABASE_URL", ""),
                   os.environ.get("SUPABASE_SECRET_KEY", ""),
                   os.environ.get("AIWORKER_OWNER_ID", ""))

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, *, body: Any = None, raw: bytes | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        hdrs = dict(self._auth)
        hdrs.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=hdrs)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                text = resp.read().decode() or "null"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            # Never echo headers: they carry the key.
            msg = f"{method} {path.split('?')[0]} -> HTTP {exc.code}: {detail}"
            if exc.code in (408, 429) or exc.code >= 500:
                raise TransientError(msg) from None
            raise StoreError(msg) from None
        except urllib.error.URLError as exc:
            raise TransientError(f"{method} {path.split('?')[0]}: {exc.reason}") from None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _rest(self, method: str, table: str, query: dict[str, str] | None = None, *,
              body: Any = None, prefer: str = "") -> Any:
        qs = ("?" + urllib.parse.urlencode(query, safe=",.()*:")) if query else ""
        headers = {"Prefer": prefer} if prefer else {}
        return self._request(method, f"/rest/v1/{table}{qs}", body=body, headers=headers)

    def _mine(self, **filters: str) -> dict[str, str]:
        return {"owner_id": f"eq.{self.owner_id}", **filters}

    # -- reads -------------------------------------------------------------
    def controls(self) -> Row:
        rows = self._rest("GET", "controls", self._mine(select="*"))
        return rows[0] if rows else {"global_stop": False, "stop_reason": ""}

    def business(self, code: str) -> Row | None:
        rows = self._rest("GET", "businesses", self._mine(code=f"eq.{code}", select="*"))
        return rows[0] if rows else None

    def accounts(self, business_id: str) -> list[Row]:
        return self._rest("GET", "accounts",
                          self._mine(business_id=f"eq.{business_id}", select="*", order="created_at"))

    def items(self, business_id: str, *, statuses: Iterable[str] = (),
              created_since: str = "", limit: int = 200) -> list[Row]:
        q = self._mine(business_id=f"eq.{business_id}", select="*",
                       order="created_at.desc", limit=str(limit))
        statuses = list(statuses)
        if statuses:
            q["status"] = f"in.({','.join(statuses)})"
        if created_since:
            q["created_at"] = f"gte.{created_since}"
        return self._rest("GET", "items", q)

    # -- writes ------------------------------------------------------------
    def _insert(self, table: str, row: Row) -> Row:
        rows = self._rest("POST", table, body={**row, "owner_id": self.owner_id},
                          prefer="return=representation")
        return rows[0]

    def create_business(self, row: Row) -> Row:
        return self._insert("businesses", row)

    def create_account(self, row: Row) -> Row:
        return self._insert("accounts", row)

    def insert_item(self, row: Row) -> Row:
        return self._insert("items", row)

    def update_item(self, item_id: str, changes: Row, *, expect_status: str = "") -> Row | None:
        q = self._mine(id=f"eq.{item_id}")
        if expect_status:
            # Optimistic: if the owner acted on the item meanwhile (cancelled
            # it from the phone), the update matches nothing and is dropped.
            q["status"] = f"eq.{expect_status}"
        rows = self._rest("PATCH", "items", q, body=changes, prefer="return=representation")
        return rows[0] if rows else None

    def approve(self, item_id: str, version: int, note: str = "") -> Row:
        return self._request("POST", "/rest/v1/rpc/approve_item",
                             body={"p_item": item_id, "p_expected_version": version,
                                   "p_note": note})

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        self._request("POST", f"/storage/v1/object/{bucket}/{urllib.parse.quote(path)}",
                      raw=data, headers={"Content-Type": content_type, "x-upsert": "true"})

    def remove(self, bucket: str, paths: list[str]) -> None:
        if paths:
            self._request("DELETE", f"/storage/v1/object/{bucket}", body={"prefixes": paths})

    def log(self, level: str, category: str, message: str, payload: Row | None = None) -> None:
        self._insert("events", {"level": level, "category": category,
                                "message": message, "payload": payload or {}})

    def heartbeat(self, worker_id: str, info: Row) -> None:
        self._rest("POST", "workers",
                   body={"id": worker_id, "owner_id": self.owner_id, "kind": "cloud",
                         "last_seen_at": _now(), "info": info},
                   prefer="resolution=merge-duplicates")


# --------------------------------------------------------------------------
# In memory
# --------------------------------------------------------------------------
_CONTENT = ("title", "body", "meta", "preview_path")


class MemoryStore:
    """Tests and dry runs. Holds files in a dict; nothing leaves the process."""

    def __init__(self, owner_id: str | None = None):
        self.owner_id = owner_id or str(uuid.uuid4())
        self.global_stop = False
        self.businesses: dict[str, Row] = {}
        self.accounts_: dict[str, Row] = {}
        self.items_: dict[str, Row] = {}
        self.files: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.events: list[Row] = []
        self.workers: dict[str, Row] = {}
        self._seq = 0

    def _stamp(self) -> str:
        # Strictly increasing, so "newest first" is well defined within a test.
        self._seq += 1
        return (dt.datetime.now(dt.UTC)
                + dt.timedelta(microseconds=self._seq)).isoformat()

    def controls(self) -> Row:
        return {"global_stop": self.global_stop, "stop_reason": ""}

    def business(self, code: str) -> Row | None:
        return next((copy.deepcopy(b) for b in self.businesses.values() if b["code"] == code), None)

    def create_business(self, row: Row) -> Row:
        b = {"id": str(uuid.uuid4()), "owner_id": self.owner_id, "stage": "designing",
             "settings": {}, **copy.deepcopy(row)}
        self.businesses[b["id"]] = b
        return copy.deepcopy(b)

    def accounts(self, business_id: str) -> list[Row]:
        return [copy.deepcopy(a) for a in self.accounts_.values() if a["business_id"] == business_id]

    def create_account(self, row: Row) -> Row:
        a = {"id": str(uuid.uuid4()), "owner_id": self.owner_id, "daily_limit": 1,
             "approval_policy": "required", "publish_method": "manual",
             "terms_status": "unverified", "status": "active", "settings": {},
             **copy.deepcopy(row)}
        self.accounts_[a["id"]] = a
        return copy.deepcopy(a)

    def items(self, business_id: str, *, statuses: Iterable[str] = (),
              created_since: str = "", limit: int = 200) -> list[Row]:
        wanted = set(statuses)
        rows = [i for i in self.items_.values() if i["business_id"] == business_id
                and (not wanted or i["status"] in wanted)
                and (not created_since or i["created_at"] >= created_since)]
        rows.sort(key=lambda i: i["created_at"], reverse=True)
        return [copy.deepcopy(i) for i in rows[:limit]]

    def insert_item(self, row: Row) -> Row:
        if row.get("status", "draft") not in (
                "draft", "image_queued", "checking", "pending_review", "blocked"):
            raise StoreError(f"items cannot start as {row.get('status')}")
        it = {"id": str(uuid.uuid4()), "owner_id": self.owner_id, "version": 1,
              "status": "draft", "title": "", "body": "", "meta": {}, "blocking": False,
              "policy_report": {}, "quality_report": {}, "revision_note": "",
              "preview_path": "", "final_path": "", "is_sample": False,
              "approved_version": None, "created_at": self._stamp(),
              **copy.deepcopy(row)}
        self.items_[it["id"]] = it
        return copy.deepcopy(it)

    def update_item(self, item_id: str, changes: Row, *, expect_status: str = "") -> Row | None:
        it = self.items_.get(item_id)
        if it is None or (expect_status and it["status"] != expect_status):
            return None
        if changes.get("status") == "approved":
            raise StoreError("items may only become approved through approve_item()")
        if any(k in changes and changes[k] != it[k] for k in _CONTENT):
            it["version"] += 1
        it.update(copy.deepcopy(changes))
        return copy.deepcopy(it)

    def approve(self, item_id: str, version: int, note: str = "") -> Row:
        it = self.items_[item_id]
        acc = self.accounts_.get(it.get("account_id") or "")
        if self.global_stop:
            raise StoreError("global stop is on")
        if not acc or acc["approval_policy"] != "auto" or it["blocking"] or it["is_sample"]:
            raise StoreError("auto-approval is not allowed for this item")
        if it["version"] != version:
            raise StoreError("item changed since it was checked")
        it.update(status="approved", approved_version=version)
        if it["final_path"] and acc["publish_method"] == "manual":
            it["status"] = "awaiting_manual"
        return copy.deepcopy(it)

    def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> None:
        self.files[(bucket, path)] = (data, content_type)

    def remove(self, bucket: str, paths: list[str]) -> None:
        for p in paths:
            self.files.pop((bucket, p), None)

    def log(self, level: str, category: str, message: str, payload: Row | None = None) -> None:
        self.events.append({"level": level, "category": category, "message": message,
                            "payload": payload or {}})

    def heartbeat(self, worker_id: str, info: Row) -> None:
        self.workers[worker_id] = {"last_seen_at": _now(), "info": info}
