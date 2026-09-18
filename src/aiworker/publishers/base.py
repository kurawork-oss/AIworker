"""Publisher interface and retry policy.

A publisher is deliberately tiny: take an item, return a result. Everything
risky -- rate limits, kill switch, approval state -- is checked by the runner
*before* a publisher is ever called, so an adapter cannot accidentally bypass a
guardrail by forgetting to call something.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..core.errors import PublishError, TransientError
from ..core.logging_setup import get_logger
from ..core.models import ContentItem

log = get_logger("publish")


@dataclass
class PublishResult:
    ok: bool
    external_id: str = ""
    external_url: str = ""
    message: str = ""
    detail: dict = field(default_factory=dict)


class Publisher(Protocol):
    name: str
    #: False for adapters that only stage content for a human to post.
    performs_network_io: bool
    #: True when `publish()` only prepares the content and a person still has
    #: to post it. The runner then parks the job in STAGED rather than calling
    #: it published, so the operator keeps a worklist and the dashboard does
    #: not claim something went out that has not.
    stages_for_human: bool

    def publish(self, item: ContentItem) -> PublishResult:
        ...


def with_retry(
    fn: Callable[[], PublishResult],
    *,
    attempts: int = 3,
    backoff_seconds: int = 30,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> PublishResult:
    """Retry only what is worth retrying.

    ``TransientError`` (network, 5xx, 429) is retried with exponential backoff
    plus jitter. ``PublishError`` is not: a rejected post will be rejected
    again, and hammering the endpoint after a rejection is how a soft warning
    becomes a hard suspension.
    """
    rng = rng or random.Random()
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except TransientError as exc:
            last = exc
            if attempt >= attempts:
                break
            delay = backoff_seconds * (2 ** (attempt - 1)) * (0.5 + rng.random())
            log.warning("transient publish failure (attempt %s/%s): %s; retrying in %.1fs",
                        attempt, attempts, exc, delay)
            sleep(delay)
        except PublishError as exc:
            return PublishResult(False, message=str(exc), detail={"retryable": False})
    return PublishResult(False, message=f"gave up after {attempts} attempts: {last}",
                         detail={"retryable": True})
