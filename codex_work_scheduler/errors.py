"""Stable machine-readable errors."""

from typing import Any, Dict, Optional


class SchedulerError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "details": self.details,
            "message": self.message,
            "retryable": self.retryable,
        }
