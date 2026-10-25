"""RestStore: the requests it sends Supabase, checked without a network."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from aiworker.cloud.store import RestStore, StoreError
from aiworker.core.errors import ConfigError, TransientError

URL = "https://abc.supabase.co"
OWNER = "11111111-1111-1111-1111-111111111111"
SECRET = "sb_secret_DO_NOT_LEAK"


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0) if self.responses else []
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(json.dumps(r).encode())

    @property
    def last(self):
        return self.requests[-1]

    def query(self, i=-1):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.requests[i].full_url).query))


def store(http, key=SECRET):
    return RestStore(URL, key, OWNER, opener=http)


def http_error(code, body="nope"):
    return urllib.error.HTTPError(URL, code, "x", {}, io.BytesIO(body.encode()))


def test_a_new_secret_key_goes_in_apikey_only():
    http = FakeHTTP([])
    store(http).accounts("b1")
    assert http.last.get_header("Apikey") == SECRET
    assert http.last.get_header("Authorization") is None


def test_a_legacy_jwt_key_also_goes_in_authorization():
    http = FakeHTTP([])
    store(http, key="eyJhbGciOi.legacy.jwt").accounts("b1")
    assert http.last.get_header("Authorization") == "Bearer eyJhbGciOi.legacy.jwt"


def test_missing_configuration_is_a_config_error():
    with pytest.raises(ConfigError):
        RestStore("", SECRET, OWNER)


def test_every_read_is_scoped_to_the_owner():
    http = FakeHTTP([])
    store(http).items("b1", statuses=("pending_review", "awaiting_manual"),
                      created_since="2026-10-24T15:00:00+00:00", limit=5)
    q = http.query()
    assert q["owner_id"] == f"eq.{OWNER}"
    assert q["business_id"] == "eq.b1"
    assert q["status"] == "in.(pending_review,awaiting_manual)"
    assert q["created_at"] == "gte.2026-10-24T15:00:00+00:00"
    assert q["limit"] == "5"


def test_inserts_carry_the_owner():
    http = FakeHTTP([{"id": "i1"}])
    store(http).insert_item({"title": "t"})
    body = json.loads(http.last.data)
    assert body["owner_id"] == OWNER
    assert http.last.get_header("Prefer") == "return=representation"


def test_an_update_the_owner_overtook_returns_none():
    http = FakeHTTP([])
    assert store(http).update_item("i1", {"status": "checking"},
                                   expect_status="image_queued") is None
    assert http.query()["status"] == "eq.image_queued"
    assert http.last.get_method() == "PATCH"


def test_approval_goes_through_the_database_function():
    http = FakeHTTP({"id": "i1", "status": "awaiting_manual"})
    store(http).approve("i1", 3, "ok")
    assert http.last.full_url == f"{URL}/rest/v1/rpc/approve_item"
    assert json.loads(http.last.data) == {"p_item": "i1", "p_expected_version": 3,
                                          "p_note": "ok"}


def test_uploads_overwrite_in_place():
    http = FakeHTTP({"Key": "x"})
    store(http).upload("previews", f"{OWNER}/i1/preview.jpg", b"jpeg", "image/jpeg")
    assert http.last.full_url == f"{URL}/storage/v1/object/previews/{OWNER}/i1/preview.jpg"
    assert http.last.get_header("X-upsert") == "true"
    assert http.last.data == b"jpeg"


def test_busy_is_transient_and_refused_is_not():
    with pytest.raises(TransientError):
        store(FakeHTTP(http_error(503))).accounts("b1")
    with pytest.raises(StoreError, match="HTTP 400"):
        store(FakeHTTP(http_error(400, "bad filter"))).accounts("b1")


def test_errors_never_carry_the_key():
    with pytest.raises(StoreError) as caught:
        store(FakeHTTP(http_error(401, "Invalid API key"))).accounts("b1")
    assert SECRET not in str(caught.value)
