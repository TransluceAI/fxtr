"""Pytest configuration and fixtures for fxtr_workstation tests."""

from __future__ import annotations

import signal
from collections.abc import Iterator

import pytest

_TEST_TIMEOUT = 30


@pytest.fixture(autouse=True)
def _per_test_timeout() -> Iterator[None]:
    """Fail any single test that runs longer than 10 seconds.

    Uses ``signal.alarm`` (POSIX only) so it works for both sync and async
    tests, including deadlocks that wouldn't respond to anyio cancellation.
    On timeout, SIGALRM fires and raises ``TimeoutError`` in the main thread,
    which pytest reports as a test failure.
    """

    def _handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(
            f"Test timed out after {_TEST_TIMEOUT} seconds. If this test is an async test, consider"
            " wrapping it in anyio.fail_after to get a more informative traceback."
        )

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(_TEST_TIMEOUT)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
