"""Media job state machine and retry policy."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from vault.db.models import JobState

S = JobState

ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    S.DISCOVERED: frozenset({S.QUEUED, S.SKIPPED}),
    S.QUEUED: frozenset({S.DOWNLOADING, S.SKIPPED, S.FAILED}),
    S.DOWNLOADING: frozenset({S.DOWNLOADED, S.RETRY_WAIT, S.SKIPPED, S.FAILED, S.QUEUED}),
    S.DOWNLOADED: frozenset({S.UPLOADING, S.RETRY_WAIT, S.FAILED, S.QUEUED}),
    S.UPLOADING: frozenset({S.DELIVERED, S.RETRY_WAIT, S.FAILED, S.SKIPPED, S.QUEUED}),
    S.RETRY_WAIT: frozenset({S.QUEUED, S.FAILED, S.SKIPPED}),
    S.DELIVERED: frozenset(),
    S.SKIPPED: frozenset(),
    S.FAILED: frozenset(),
}

TERMINAL_STATES = frozenset({S.DELIVERED, S.SKIPPED, S.FAILED})
ACTIVE_STATES = frozenset({S.DISCOVERED, S.QUEUED, S.DOWNLOADING, S.DOWNLOADED, S.UPLOADING, S.RETRY_WAIT})
LEASED_STATES = frozenset({S.DOWNLOADING, S.DOWNLOADED, S.UPLOADING})


class InvalidTransition(Exception):
    pass


def check_transition(current: JobState, new: JobState) -> None:
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"{current} -> {new}")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_seconds: int
    max_seconds: int

    def should_retry(self, attempts: int) -> bool:
        """``attempts`` = attempts already made, including the one that just failed."""
        return attempts < self.max_attempts

    def backoff(self, attempts: int, *, rng: random.Random | None = None) -> timedelta:
        """Exponential backoff with full jitter in [0.5x, 1x] to avoid thundering herds."""
        exp = min(self.max_seconds, self.base_seconds * (2 ** max(0, attempts - 1)))
        r = (rng or random).uniform(0.5, 1.0)
        return timedelta(seconds=max(1.0, exp * r))

    def next_attempt_at(
        self, now: datetime, attempts: int, *, min_wait_seconds: float = 0, rng: random.Random | None = None
    ) -> datetime:
        delay = max(self.backoff(attempts, rng=rng), timedelta(seconds=min_wait_seconds))
        return now + delay


# Non-sensitive reason codes shown (translated) to users.
class SkipReason:
    TOO_LARGE = "too_large"
    UNSUPPORTED = "unsupported"
    PROTECTED = "protected_content"
    SOURCE_DELETED = "source_deleted"
    CHAT_INACTIVE = "chat_inactive"
    ACCOUNT_INACTIVE = "account_inactive"
    DESTINATION_UNAVAILABLE = "destination_unavailable"
    BACKLOG_FULL = "backlog_full"
