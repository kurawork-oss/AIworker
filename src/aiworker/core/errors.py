"""Exception hierarchy.

Every guardrail failure is a *typed* exception so callers can tell a hard stop
(`HaltError`) apart from "this one item is not allowed" (`GuardError`) apart
from a transient problem worth retrying (`TransientError`).
"""

from __future__ import annotations


class AIWorkerError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(AIWorkerError):
    """Configuration is missing, malformed, or internally inconsistent."""


class GuardError(AIWorkerError):
    """A guardrail rejected an item. The run continues with other items."""

    def __init__(self, reason: str, *, code: str = "guard", detail: object = None):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.detail = detail


class QuotaExceeded(GuardError):
    """Posting this item would break a rate limit."""

    def __init__(self, reason: str, detail: object = None):
        super().__init__(reason, code="quota", detail=detail)


class PolicyViolation(GuardError):
    """Content violates a legal / platform-policy rule."""

    def __init__(self, reason: str, detail: object = None):
        super().__init__(reason, code="policy", detail=detail)


class QualityRejected(GuardError):
    """Content failed an automated quality check (duplication, length, ...)."""

    def __init__(self, reason: str, detail: object = None):
        super().__init__(reason, code="quality", detail=detail)


class HaltError(AIWorkerError):
    """The kill switch is engaged. Nothing may be published."""


class TransientError(AIWorkerError):
    """A retryable failure (network, 5xx, rate-limit response)."""


class PublishError(AIWorkerError):
    """A non-retryable publishing failure."""
