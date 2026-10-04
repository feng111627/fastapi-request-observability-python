"""Error taxonomy: business vs validation vs timeout vs uncaught."""
from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    NONE = "none"
    BUSINESS = "business"
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    UNCAUGHT = "uncaught"
    DOWNSTREAM = "downstream"
    TRACE_MISMATCH = "trace_mismatch"
    BACKGROUND = "background"


class BusinessError(Exception):
    """Expected business-rule failure (e.g. insufficient balance)."""

    def __init__(self, message: str, code: str = "BUSINESS_ERROR") -> None:
        super().__init__(message)
        self.code = code


class OperationTimeout(Exception):
    """A stage or the whole request exceeded its time budget."""


class DownstreamError(Exception):
    """The downstream call failed (connection, 5xx, or bad payload)."""


class TraceMismatchError(Exception):
    """The trace id echoed by the downstream differs from the one sent."""

    def __init__(self, sent: str, echoed: str | None) -> None:
        super().__init__(f"trace id mismatch: sent={sent!r} echoed={echoed!r}")
        self.sent = sent
        self.echoed = echoed
