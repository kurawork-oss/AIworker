"""The Hatena Blog AtomPub client, without a network."""

from __future__ import annotations

import base64
import io
import urllib.error
import xml.etree.ElementTree as ET

import pytest

from aiworker.cloud.hatena import ATOM, HatenaPublisher, PublishError, marker
from aiworker.core.errors import ConfigError, TransientError

ENDPOINT = "https://blog.hatena.ne.jp/me/me.hatenablog.com/atom/entry"


def entry_xml(n: int, content: str) -> str:
    return (f'<entry><link rel="edit" href="{ENDPOINT}/{n}"/>'
            f'<link rel="alternate" type="text/html" href="https://me.hatenablog.com/entry/{n}"/>'
            f"<content>{content}</content></entry>")


def feed(entries: str, next_url: str = "") -> bytes:
    nxt = f'<link rel="next" href="{next_url}"/>' if next_url else ""
    return f'<feed xmlns="http://www.w3.org/2005/Atom">{nxt}{entries}</feed>'.encode()


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(r)


def pub(http):
    return HatenaPublisher("me", "me.hatenablog.com", "KEY", opener=http)


def error(code):
    return urllib.error.HTTPError(ENDPOINT, code, "x", {}, io.BytesIO(b"nope"))


def test_posting_sends_an_escaped_atom_entry_with_basic_auth():
    http = FakeHTTP(f'<entry xmlns="http://www.w3.org/2005/Atom">{entry_xml(7, "")[7:]}'.encode())
    posted = pub(http).post("A & B <c>", "本文 <a href=\"x\">link</a>", ["副業", 'q"x'])
    req = http.requests[0]
    assert req.full_url == ENDPOINT and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"me:KEY").decode()
    doc = ET.fromstring(req.data)
    assert doc.findtext(f"{ATOM}title") == "A & B <c>"
    assert doc.findtext(f"{ATOM}content") == '本文 <a href="x">link</a>'
    assert [c.get("term") for c in doc.findall(f"{ATOM}category")] == ["副業", 'q"x']
    assert posted.url == "https://me.hatenablog.com/entry/7" and posted.entry_id == "7"


def test_find_looks_for_the_marker_across_pages():
    page1 = feed(entry_xml(2, "other"), next_url=ENDPOINT + "?page=2")
    page2 = feed(entry_xml(1, "body " + marker("abc").replace("<", "&lt;").replace(">", "&gt;")))
    found = pub(FakeHTTP(page1, page2)).find("abc")
    assert found and found.entry_id == "1"


def test_find_returns_none_when_absent():
    assert pub(FakeHTTP(feed(entry_xml(1, "x")))).find("zzz") is None


@pytest.mark.parametrize("code,exc", [(401, ConfigError), (403, ConfigError),
                                      (429, TransientError), (503, TransientError),
                                      (400, PublishError)])
def test_errors_are_sorted_by_what_to_do_next(code, exc):
    with pytest.raises(exc):
        pub(FakeHTTP(error(code))).post("t", "b")


def test_ids_cannot_reshape_the_url():
    with pytest.raises(ConfigError):
        HatenaPublisher("me/../x", "blog", "KEY")


def test_unconfigured_is_none_not_an_error(monkeypatch):
    for k in ("HATENA_ID", "HATENA_BLOG_ID", "HATENA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert HatenaPublisher.from_env() is None
