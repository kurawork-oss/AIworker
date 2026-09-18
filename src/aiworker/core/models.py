"""Domain enums and row objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    """Lifecycle of one piece of content.

    The only edge into ``APPROVED`` is a human action recorded in the
    ``approvals`` table -- see :mod:`aiworker.approval.service`.
    """

    PENDING_REVIEW = "pending_review"   # generated, waiting on a human
    BLOCKED = "blocked"                 # auto-rejected by policy/quality, human may override
    NEEDS_REVISION = "needs_revision"   # human asked for changes
    REJECTED = "rejected"               # human said no
    APPROVED = "approved"               # human said yes, not yet scheduled
    SCHEDULED = "scheduled"             # has a publish job with a future slot
    PUBLISHED = "published"
    FAILED = "failed"                   # publishing exhausted its retries

    @property
    def is_terminal(self) -> bool:
        return self in {Status.REJECTED, Status.PUBLISHED}


class Channel(str, Enum):
    """What kind of artefact this is (drives generation + quality rules)."""

    SOCIAL_POST = "social_post"       # X / Threads short post
    SOCIAL_THREAD = "social_thread"   # multi-part post
    NOTE_ARTICLE = "note_article"     # note.com draft
    SHORTS_SCRIPT = "shorts_script"   # YouTube Shorts script + title + description
    STOCK_ASSET = "stock_asset"       # image/video prompt + metadata
    DIGITAL_PRODUCT = "digital_product"


class JobStatus(str, Enum):
    QUEUED = "queued"
    BLOCKED = "blocked"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Severity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}[self.value]


# Metadata keys that are *not* content: they describe what to exclude, or are
# bookkeeping. Scanning them for banned terms flags the safety instruction
# itself ("no logos, no real person") as a violation.
NON_CONTENT_META = frozenset({
    "negative_prompt", "generator", "schema_keys", "ai_disclosure_auto_added",
})


@dataclass
class ContentItem:
    id: int | None = None
    uid: str = ""
    channel: str = Channel.SOCIAL_POST.value
    platform: str = ""
    account: str = "main"
    theme: str = ""
    angle: str = ""
    tone: str = ""
    title: str = ""
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = Status.PENDING_REVIEW.value
    dedupe_hash: str = ""
    similarity: float = 0.0
    quality_report: dict[str, Any] = field(default_factory=dict)
    policy_report: dict[str, Any] = field(default_factory=dict)
    revision_note: str = ""
    run_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    approved_at: str | None = None
    approved_by: str | None = None

    def preview(self, width: int = 78) -> str:
        text = " ".join((self.title or self.body).split())
        return text[:width] + ("…" if len(text) > width else "")

    @property
    def full_text(self) -> str:
        """Everything a guardrail should look at: title + body + content metadata."""
        extras, lists = [], []
        for key, value in sorted(self.meta.items()):
            if key in NON_CONTENT_META:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (str, int, float)):
                extras.append(str(value))
            elif isinstance(value, list):
                lists.append(" ".join(str(x) for x in value))
        return "\n".join(filter(None, [self.title, self.body, *extras, *lists]))


@dataclass
class PublishJob:
    id: int | None = None
    content_id: int = 0
    platform: str = ""
    account: str = "main"
    scheduled_at: str = ""
    status: str = JobStatus.QUEUED.value
    attempts: int = 0
    last_error: str = ""
    external_id: str = ""
    external_url: str = ""
    published_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
