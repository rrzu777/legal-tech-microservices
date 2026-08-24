"""Closed, redacted error contract for authenticated OJV operations."""

from __future__ import annotations

from enum import Enum


class OjvSessionErrorCode(str, Enum):
    INVALID_CREDENTIAL = "credential_invalid"
    EXPIRED = "session_expired"
    WAF = "waf"
    TIMEOUT = "timeout"
    UPSTREAM_CHANGED = "upstream_changed"


_SAFE_MESSAGES: dict[OjvSessionErrorCode, str] = {
    OjvSessionErrorCode.INVALID_CREDENTIAL: "OJV credential rejected",
    OjvSessionErrorCode.EXPIRED: "OJV session unavailable",
    OjvSessionErrorCode.WAF: "OJV request blocked",
    OjvSessionErrorCode.TIMEOUT: "OJV request timed out",
    OjvSessionErrorCode.UPSTREAM_CHANGED: "OJV response contract changed",
}


class OjvSessionError(Exception):
    """An authenticated OJV failure with no upstream-controlled message."""

    code: OjvSessionErrorCode

    def __init__(
        self,
        code: OjvSessionErrorCode,
        *_ignored_sensitive_context: object,
    ) -> None:
        self.code = code
        super().__init__(_SAFE_MESSAGES[code])

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value!r})"


class InvalidCredentialsError(OjvSessionError):
    def __init__(self, *_ignored_sensitive_context: object) -> None:
        super().__init__(OjvSessionErrorCode.INVALID_CREDENTIAL)


class SessionExpiredError(OjvSessionError):
    def __init__(self, *_ignored_sensitive_context: object) -> None:
        super().__init__(OjvSessionErrorCode.EXPIRED)


class OjvWafError(OjvSessionError):
    def __init__(self, *_ignored_sensitive_context: object) -> None:
        super().__init__(OjvSessionErrorCode.WAF)


class OjvTimeoutError(OjvSessionError):
    def __init__(self, *_ignored_sensitive_context: object) -> None:
        super().__init__(OjvSessionErrorCode.TIMEOUT)


class OjvUpstreamChangedError(OjvSessionError):
    def __init__(self, *_ignored_sensitive_context: object) -> None:
        super().__init__(OjvSessionErrorCode.UPSTREAM_CHANGED)


# Compatibility names used by the existing Familia route/worker contract.
class SessionError(SessionExpiredError):
    pass


class FamiliaBlockedError(OjvWafError):
    pass
