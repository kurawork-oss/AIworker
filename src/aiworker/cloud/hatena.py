"""Publishing to Hatena Blog through its official AtomPub API.

Authentication is the blog's own API key (ブログの詳細設定 → AtomPub), sent
as HTTP Basic auth with the Hatena ID -- never the account password.

Exactly-once, the hard part: if a post succeeds but the response is lost
(timeout, runner killed), the item stays `scheduled` and the next run would
post it again. Every article therefore carries an invisible marker
(``<!-- aiw:<uid> -->``), and before posting a `scheduled` item again the
publisher looks for that marker among the blog's recent entries.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

from ..core.errors import ConfigError, TransientError

ATOM = "{http://www.w3.org/2005/Atom}"
MARKER = "<!-- aiw:{uid} -->"
_ID_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class PublishError(RuntimeError):
    """The blog refused this entry (not the credentials, not a busy server)."""


@dataclass
class Posted:
    url: str
    entry_id: str


def marker(uid: str) -> str:
    return MARKER.format(uid=uid)


class HatenaPublisher:
    name = "hatena_blog"

    def __init__(self, hatena_id: str, blog_id: str, api_key: str, *, timeout: int = 60,
                 opener: Callable[..., Any] | None = None):
        if not (hatena_id and blog_id and api_key):
            raise ConfigError("HATENA_ID / HATENA_BLOG_ID / HATENA_API_KEY が必要です")
        if not (_ID_SAFE.match(hatena_id) and _ID_SAFE.match(blog_id)):
            raise ConfigError("HATENA_ID / HATENA_BLOG_ID の形式が不正です")
        self.endpoint = f"https://blog.hatena.ne.jp/{hatena_id}/{blog_id}/atom/entry"
        basic = base64.b64encode(f"{hatena_id}:{api_key}".encode()).decode()
        self._auth = {"Authorization": f"Basic {basic}"}
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> HatenaPublisher | None:
        """None when not configured yet: the app shows 未接続, nothing fails."""
        hid, blog, key = (os.environ.get(k, "") for k in
                          ("HATENA_ID", "HATENA_BLOG_ID", "HATENA_API_KEY"))
        if not (hid or blog or key):
            return None
        return cls(hid, blog, key)

    # -- transport ---------------------------------------------------------
    def _call(self, method: str, url: str, body: bytes | None = None) -> bytes:
        headers = dict(self._auth)
        if body is not None:
            headers["Content-Type"] = "application/atom+xml; charset=utf-8"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code in (401, 403):
                raise ConfigError(f"はてなブログ {exc.code}: HATENA_ID / HATENA_API_KEY を確認してください") from None
            if exc.code in (408, 429) or exc.code >= 500:
                raise TransientError(f"はてなブログ {exc.code}: {detail}") from None
            raise PublishError(f"はてなブログ {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise TransientError(f"はてなブログに接続できません: {exc.reason}") from None

    # -- API ---------------------------------------------------------------
    def post(self, title: str, markdown: str, categories: list[str] | None = None,
             *, draft: bool = False) -> Posted:
        cats = "".join(f'<category term="{escape(c, {chr(34): "&quot;"})}" />'
                       for c in (categories or [])[:5])
        entry = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<entry xmlns="http://www.w3.org/2005/Atom" xmlns:app="http://www.w3.org/2007/app">'
            f"<title>{escape(title)}</title>"
            f'<content type="text/x-markdown">{escape(markdown)}</content>'
            f"{cats}"
            f"<app:control><app:draft>{'yes' if draft else 'no'}</app:draft></app:control>"
            "</entry>")
        return self._posted(ET.fromstring(self._call("POST", self.endpoint, entry.encode())))

    def find(self, uid: str, *, pages: int = 2) -> Posted | None:
        """The entry carrying this item's marker, among the newest entries."""
        needle = marker(uid)
        url: str | None = self.endpoint
        for _ in range(pages):
            if not url:
                break
            feed = ET.fromstring(self._call("GET", url))
            for entry in feed.findall(f"{ATOM}entry"):
                content = entry.findtext(f"{ATOM}content") or ""
                if needle in content:
                    return self._posted(entry)
            nxt = feed.find(f"{ATOM}link[@rel='next']")
            url = nxt.get("href") if nxt is not None else None
        return None

    @staticmethod
    def _posted(entry: ET.Element) -> Posted:
        alt = entry.find(f"{ATOM}link[@rel='alternate']")
        edit = entry.find(f"{ATOM}link[@rel='edit']")
        url = alt.get("href", "") if alt is not None else ""
        entry_id = (edit.get("href", "").rsplit("/", 1)[-1] if edit is not None else "")
        return Posted(url=url, entry_id=entry_id)
