"""Backend-agnostic source-side (MTProto) errors."""

from __future__ import annotations


class SourceError(Exception):
    code = "source_error"
    retryable = True


class SourceGone(SourceError):
    """Message or media no longer exists / changed."""

    code = "source_deleted"
    retryable = False


class SourceProtected(SourceError):
    code = "protected_content"
    retryable = False


class SourceTooLarge(SourceError):
    code = "too_large"
    retryable = False


class SourceFloodWait(SourceError):
    code = "flood_wait"

    def __init__(self, seconds: float) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


class SourceSessionInvalid(SourceError):
    """Authorization revoked/expired at Telegram. Requires user reconnection."""

    code = "session_invalid"
    retryable = False


class SourceChatInaccessible(SourceError):
    code = "chat_inaccessible"
    retryable = False


class SourceUnavailable(SourceError):
    """The account runtime isn't connected right now (transient)."""

    code = "source_unavailable"
