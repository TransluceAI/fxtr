"""Pytest configuration and shared fixtures for fxtr tests."""

from __future__ import annotations

import functools
import inspect
import math
import os
import signal
import uuid
from collections.abc import AsyncIterator, Iterator
from types import FrameType
from typing import TYPE_CHECKING, Any

import anyio
import pyarrow.dataset  # noqa: F401 (see below)
import pytest

# DuckDB imports pyarrow.dataset the first time a process queries an Arrow table, which loads
# several native libraries and takes seconds on a fresh install. Importing it above, while
# collecting, pays that once per worker instead of inside whichever test first runs a query,
# against that test's timeout.

if TYPE_CHECKING:
    from reference_backends import Executor

    from fxtr.runner.postgres.database import Database

_TEST_TIMEOUT = 10
"""Seconds an async test may run before it is cancelled and failed, unless it is marked
``@pytest.mark.timeout_seconds(n)`` (see :func:`_timeout`)."""
_LAST_RESORT_FACTOR = 3
"""How many times a test's timeout the signal-based fallback waits (``_last_resort_timeout``)."""

TEST_DATABASE_URL_ENV_VAR = "FXTR_TEST_DATABASE_URL"
"""Names the Postgres server the Postgres-backed tests run against; they are skipped without it."""

# Jobs the tests launch never open a browser on a viewer someone has running.
os.environ["FXTR_NO_BROWSER"] = "1"


@pytest.fixture
def anyio_backend() -> str:
    """Run ``@pytest.mark.anyio`` tests on the asyncio backend."""
    return "asyncio"


def _timeout(item: pytest.Item) -> float:
    """Seconds ``item`` may run: its ``timeout_seconds`` mark's, for a test that is slow by design
    (one at a large scale, say), else :data:`_TEST_TIMEOUT`."""
    mark = item.get_closest_marker("timeout_seconds")
    return _TEST_TIMEOUT if mark is None else mark.args[0]


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: Any) -> Iterator[None]:
    """Fail any async test that runs longer than its timeout (:func:`_timeout`), by cancellation.

    Wraps the test coroutine in ``anyio.fail_after`` before anyio's plugin runs it, so a test that
    hangs is *cancelled*: its ``async with`` blocks unwind, its fixtures tear down, and the run
    goes on. This matters because anyio's plugin runs every fixture and test of a session through
    one runner task, sequentially: a test that is merely interrupted (an exception raised into the
    event loop by a signal, say) leaves that task stuck awaiting the hung coroutine, and every
    later teardown and test then hangs behind it.
    """
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        original = pyfuncitem.obj
        timeout = _timeout(pyfuncitem)

        @functools.wraps(original)
        async def timed(**kwargs: Any) -> None:
            with anyio.fail_after(timeout):
                await original(**kwargs)

        pyfuncitem.obj = timed
    yield


@pytest.fixture(autouse=True)
def _last_resort_timeout(request: pytest.FixtureRequest) -> Iterator[None]:
    """The fallback for what cancellation cannot reach: a sync test that blocks, or an async test
    wedged in a shielded scope. Uses ``signal.alarm`` (POSIX only) with a deadline
    :data:`_LAST_RESORT_FACTOR` times the test's timeout, so it fires only when the cancellation
    above did not work. On timeout, SIGALRM raises ``TimeoutError`` in the main thread, which
    pytest reports as a failure; for an async test this also wedges anyio's runner task (see
    :func:`pytest_pyfunc_call`), so the rest of the run may hang, but the failure is at least
    reported first.
    """
    deadline = math.ceil(_LAST_RESORT_FACTOR * _timeout(request.node))

    def _handler(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError(f"Test timed out after {deadline} seconds and could not be cancelled.")

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(deadline)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


@pytest.fixture(autouse=True)
def _fresh_default_query_executor() -> None:
    """Give each test its own default executor for local queries.

    The process shares one, and its metrics (queries run, connections opened) are what the
    local query tests assert on, so one test's queries must not count toward the next.
    """
    from fxtr.duckdb_engine.local_query import default_executor

    default_executor.cache_clear()


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    """A :class:`~fxtr.runner.postgres.database.Database` on a fresh, throwaway Postgres schema of
    the server named by ``FXTR_TEST_DATABASE_URL``, with the runner's tables created; the schema
    is dropped afterwards. Skips the test when the variable is unset or the extra is missing."""
    url = os.environ.get(TEST_DATABASE_URL_ENV_VAR)
    if url is None:
        pytest.skip(f"{TEST_DATABASE_URL_ENV_VAR} is not set")
    pytest.importorskip("sqlalchemy")
    from fxtr.runner.postgres.database import Database

    async with Database(url, pg_schema=f"test_{uuid.uuid4().hex}") as db:
        await db.create_schema()
        try:
            yield db
        finally:
            # A failed test may leave transactions open on the schema's tables; drop_schema
            # terminates them and bounds its lock wait, and this bounds everything else, so a
            # failure never turns into a hang in teardown.
            with anyio.fail_after(30):
                await db.drop_schema(lock_timeout="5s")


@pytest.fixture
async def execute() -> AsyncIterator[Executor]:
    """Run example workflows on isolated Postgres schemas with readable execution summaries."""
    from reference_backends import postgres_backend

    async with postgres_backend() as executor:
        yield executor
