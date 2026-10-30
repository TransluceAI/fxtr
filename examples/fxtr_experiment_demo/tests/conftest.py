"""Pytest setup for the experiment demo's host-side tests.

The workdir packages under ``agent_workdir/packages/`` are marker-style
packages, not installed into this venv — make them importable the same
way ``run_experiment_demo.py`` does, via ``sys.path`` inserts against
their ``src/`` layouts (the embedding venv supplies fxtr2). The host
modules next to the orchestrator (``demo_task_fns`` etc.) are made
importable via the demo dir itself.
"""

from __future__ import annotations

import gc
import signal
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_DEMO_DIR = Path(__file__).resolve().parents[1]
_PACKAGES = _DEMO_DIR / "agent_workdir" / "packages"
sys.path.insert(0, str(_DEMO_DIR))
sys.path.insert(0, str(_DEMO_DIR / "behavior"))
for _pkg in (
    "experiment_notes",
    "experiment_demo_helper",
    "behavior_research_types",
    "behavior_demo_helper",
):
    sys.path.insert(0, str(_PACKAGES / _pkg / "src"))

from fxtr2.leases import local_lease  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_TEST_TIMEOUT = 30


@pytest.fixture(autouse=True)
def _per_test_timeout() -> Iterator[None]:
    """Fail any single test that runs longer than 30 seconds (the
    v2/tests/conftest.py signal.alarm guard — catches deadlocks that
    wouldn't respond to anyio cancellation)."""

    def _handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(
            f"Test timed out after {_TEST_TIMEOUT} seconds. If this test is an async test,"
            " consider wrapping it in anyio.fail_after for a more informative traceback."
        )

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(_TEST_TIMEOUT)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


@pytest.fixture(autouse=True)
def _assert_no_lease_leaks() -> Iterator[None]:
    """Fail the test if any lease is GC'd without explicit discard during
    its run (same guard as v2/tests/conftest.py)."""
    gc.collect()
    before = local_lease.get_lease_leak_count()
    yield
    gc.collect()
    delta = local_lease.get_lease_leak_count() - before
    if delta:
        pytest.fail(f"Test leaked {delta} lease(s) without discard; see warnings in log.")
