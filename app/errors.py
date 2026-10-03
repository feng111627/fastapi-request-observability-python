"""Error taxonomy: business vs validation vs timeout vs uncaught."""
from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    NONE = "none"
    BUSINESS = "business"
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    UNCAUGHT = "uncaught"


class BusinessError(Exception):
    """Expected business-rule failure (e.g. insufficient balance)."""

    def __init__(self, message: str, code: str = "BUSINESS_ERROR") -> None:
        super().__init__(message)
        self.code = code


class OperationTimeout(Exception):
    """A stage or the whole request exceeded its time budget."""
