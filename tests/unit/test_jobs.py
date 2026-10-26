import itertools
import random
from datetime import timedelta

import pytest

from tests.conftest import NOW
from vault.db.models import JobState as S
from vault.domain.jobs import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidTransition,
    RetryPolicy,
    check_transition,
)


def test_happy_path_transitions():
    path = [S.DISCOVERED, S.QUEUED, S.DOWNLOADING, S.DOWNLOADED, S.UPLOADING, S.DELIVERED]
    for a, b in itertools.pairwise(path):
        check_transition(a, b)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_terminal_states_are_final(terminal):
    for target in S:
        with pytest.raises(InvalidTransition):
            check_transition(terminal, target)


def test_cannot_skip_download():
    with pytest.raises(InvalidTransition):
        check_transition(S.QUEUED, S.UPLOADING)
    with pytest.raises(InvalidTransition):
        check_transition(S.QUEUED, S.DELIVERED)


def test_every_state_has_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_retry_limits():
    p = RetryPolicy(max_attempts=4, base_seconds=5, max_seconds=60)
    assert p.should_retry(1) and p.should_retry(3)
    assert not p.should_retry(4)


def test_backoff_exponential_with_cap_and_jitter():
    p = RetryPolicy(max_attempts=10, base_seconds=5, max_seconds=60)
    rng = random.Random(1)
    for attempt, ceiling in [(1, 5), (2, 10), (3, 20), (4, 40), (5, 60), (9, 60)]:
        d = p.backoff(attempt, rng=rng).total_seconds()
        assert ceiling * 0.5 <= d <= ceiling


def test_min_wait_honoured_for_flood_wait():
    p = RetryPolicy(max_attempts=5, base_seconds=1, max_seconds=10)
    nxt = p.next_attempt_at(NOW, 1, min_wait_seconds=300)
    assert nxt - NOW >= timedelta(seconds=300)
