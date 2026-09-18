"""Automated quality screening, including near-duplicate detection.

The duplicate check is the load-bearing one. Template reuse is what turns a
content pipeline into a spam pipeline, and it is invisible item-by-item -- you
only see it across the corpus. We compare each new item against the last N
items on the same channel using Jaccard similarity over character 4-grams,
which works on Japanese (no whitespace tokenisation) and on English alike.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

from ..core import db
from ..core.config import QualityConfig
from ..core.models import Channel, ContentItem

SHINGLE = 4

# Sensible defaults; overridable per channel from config.
DEFAULT_MIN_CHARS = {
    Channel.SOCIAL_POST.value: 40,
    Channel.SOCIAL_THREAD.value: 200,
    Channel.NOTE_ARTICLE.value: 800,
    Channel.SHORTS_SCRIPT.value: 150,
    Channel.STOCK_ASSET.value: 30,
    Channel.DIGITAL_PRODUCT.value: 200,
}
DEFAULT_MAX_CHARS = {
    Channel.SOCIAL_POST.value: 280,
    Channel.SOCIAL_THREAD.value: 4000,
    Channel.NOTE_ARTICLE.value: 20000,
    Channel.SHORTS_SCRIPT.value: 2000,
    Channel.STOCK_ASSET.value: 1500,
    Channel.DIGITAL_PRODUCT.value: 20000,
}

# Metadata a channel cannot be published without.
REQUIRED_META: dict[str, tuple[str, ...]] = {
    Channel.SHORTS_SCRIPT.value: ("video_title", "description"),
    Channel.STOCK_ASSET.value: ("prompt", "keywords"),
    Channel.NOTE_ARTICLE.value: ("headline", "outline"),
}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[!-/:-@\[-`{-~、。！？「」『』（）・…ー〜]")


@dataclass
class QualityIssue:
    rule: str
    detail: str
    fatal: bool = True

    def __str__(self) -> str:
        return f"[{'fatal' if self.fatal else 'warn'}] {self.rule}: {self.detail}"


@dataclass
class QualityReport:
    similarity: float = 0.0
    nearest_uid: str = ""
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.fatal for i in self.issues)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "similarity": round(self.similarity, 4),
            "nearest_uid": self.nearest_uid,
            "issues": [{"rule": i.rule, "detail": i.detail, "fatal": i.fatal} for i in self.issues],
        }

    def reasons(self) -> list[str]:
        return [str(i) for i in self.issues]


def normalize(text: str) -> str:
    """NFKC + case-fold + drop punctuation/whitespace.

    Full-width/half-width and ASCII-case differences are not content
    differences, and treating them as such is exactly how "varied" output that
    is really the same template slips past a duplicate check.
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def shingles(text: str, n: int = SHINGLE) -> set[str]:
    norm = normalize(text).replace(" ", "")
    if len(norm) <= n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def similarity(a: str, b: str) -> float:
    """Jaccard similarity over character n-grams, in [0, 1]."""
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def nearest_neighbour(
    conn: sqlite3.Connection, item: ContentItem, *, lookback: int = 200
) -> tuple[float, str]:
    """Highest similarity against recent items on the same channel."""
    rows = conn.execute(
        "SELECT uid, title, body, dedupe_hash FROM content_items "
        "WHERE channel=? AND id != COALESCE(?, -1) ORDER BY id DESC LIMIT ?",
        (item.channel, item.id, lookback),
    ).fetchall()
    text = f"{item.title}\n{item.body}"
    best, best_uid = 0.0, ""
    own_hash = fingerprint(text)
    for r in rows:
        if r["dedupe_hash"] and r["dedupe_hash"] == own_hash:
            return 1.0, r["uid"]
        score = similarity(text, f"{r['title']}\n{r['body']}")
        if score > best:
            best, best_uid = score, r["uid"]
    return best, best_uid


def check(item: ContentItem, config: QualityConfig,
          conn: sqlite3.Connection | None = None) -> QualityReport:
    report = QualityReport()
    body_len = len(f"{item.title}{item.body}".strip())

    min_chars = {**DEFAULT_MIN_CHARS, **config.min_chars}
    max_chars = {**DEFAULT_MAX_CHARS, **config.max_chars}
    lo = min_chars.get(item.channel, 20)
    hi = max_chars.get(item.channel, 20000)
    if body_len < lo:
        report.issues.append(QualityIssue("too_short", f"{body_len} chars < minimum {lo}"))
    if body_len > hi:
        report.issues.append(QualityIssue("too_long", f"{body_len} chars > maximum {hi}"))

    low = f"{item.title}\n{item.body}".lower()
    for artefact in config.banned_artifacts:
        if artefact.lower() in low:
            report.issues.append(
                QualityIssue("generation_artifact", f"output still contains '{artefact}'")
            )

    for key in REQUIRED_META.get(item.channel, ()):
        value = item.meta.get(key)
        if not value or (isinstance(value, (list, dict)) and len(value) == 0):
            report.issues.append(QualityIssue("missing_metadata", f"meta.{key} is empty"))

    if item.channel == Channel.STOCK_ASSET.value:
        # A stock prompt without these exclusions is how a watermark, a logo or
        # a recognisable face ends up in an asset that then gets rejected -- or
        # worse, accepted and later taken down.
        negative = str(item.meta.get("negative_prompt", "")).lower()
        if negative:
            missing = [w for w in ("text", "watermark", "logo", "brand", "person")
                       if w not in negative]
            if missing:
                report.issues.append(
                    QualityIssue("weak_negative_prompt",
                                 f"negative_prompt is missing exclusions: {missing}")
                )
        kws = item.meta.get("keywords") or []
        if isinstance(kws, list) and 0 < len(kws) < 8:
            report.issues.append(
                QualityIssue("thin_keywords", f"only {len(kws)} keywords (stock sites want 8+)",
                             fatal=False)
            )

    if config.min_distinct_hashtags:
        tags = {t.lower() for t in re.findall(r"#\S+", f"{item.title} {item.body}")}
        if len(tags) < config.min_distinct_hashtags:
            report.issues.append(
                QualityIssue("few_hashtags",
                             f"{len(tags)} distinct hashtags < {config.min_distinct_hashtags}",
                             fatal=False)
            )

    if conn is not None:
        score, uid = nearest_neighbour(conn, item, lookback=config.similarity_lookback)
        report.similarity, report.nearest_uid = score, uid
        if score >= config.max_similarity:
            report.issues.append(
                QualityIssue(
                    "duplicate",
                    f"{score:.0%} similar to {uid or 'an earlier item'} "
                    f"(threshold {config.max_similarity:.0%})",
                )
            )
    return report
