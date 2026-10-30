"""Shared fixtures."""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from fxtr_nix_runner.testing import skip_if_runtime_unconfigured

from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import DEFAULT_CACHE_DIR

#: Default per-test wall-clock deadline. Individual tests can override
#: via the :func:`pytest.mark.deadline` marker (see below). Sized with
#: generous headroom over idle-machine runtimes (a few seconds for most
#: e2e tests here): the e2e servers slow down considerably when the
#: machine is loaded — e.g. a full serial suite run alongside live
#: workstation MCP sessions has been observed to stretch a ~4 s test
#: past 30 s.
_TEST_TIMEOUT = 90.0


def pytest_configure(config: pytest.Config) -> None:
    """Register the custom markers so pytest doesn't warn about them."""
    config.addinivalue_line(
        "markers",
        "deadline(seconds): override the default per-test "
        "anyio.fail_after deadline (in seconds). Apply to tests "
        "that legitimately need longer than the default to "
        "complete — e.g. tests that walk through several MCP "
        "scenarios end-to-end in one body.",
    )
    config.addinivalue_line(
        "markers",
        "nix_runtime: needs a configured nix backend; skips when unconfigured "
        "(set FXTR_TESTS_REQUIRE_RUNTIME=1 to fail instead).",
    )


@pytest.fixture(autouse=True)
def _require_runtime_for_nix_tests(request: pytest.FixtureRequest) -> None:
    """Skip a ``nix_runtime``-marked test when no nix backend is configured.

    Scoped to the marker so unrelated tests run without a configured backend;
    the ``FXTR_TESTS_REQUIRE_RUNTIME`` guard flips skip→fail (CI).
    """
    if request.node.get_closest_marker("nix_runtime"):
        skip_if_runtime_unconfigured()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Session-scoped backend so the event loop survives across tests
    that share session-scoped async fixtures (notably
    :func:`sandbox_env_supervisor`)."""
    return "asyncio"


def _deadline_for(item: pytest.Function) -> float:
    """Resolve the per-test deadline: ``deadline`` marker or the default."""
    marker = item.get_closest_marker("deadline")
    if marker is None:
        return _TEST_TIMEOUT
    deadline = marker.kwargs.get("seconds")
    if deadline is None and marker.args:
        deadline = marker.args[0]
    if deadline is None:
        raise pytest.UsageError(
            f"@pytest.mark.deadline on {item.name!r} must specify the "
            "seconds (e.g. ``@pytest.mark.deadline(seconds=60)``)."
        )
    return float(deadline)


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Any:
    """Fail any single test that runs longer than its deadline.

    Wraps the test *coroutine function itself* in
    :func:`anyio.fail_after` before the anyio pytest plugin's own
    ``pytest_pyfunc_call`` runs it, so the cancel scope is entered and
    exited inside the test's own task. A deadline firing is then an
    ordinary ``TimeoutError`` propagating out of the test body — the
    test fails, its fixtures tear down normally, and the rest of the
    suite keeps running.

    Where the scope lives is the whole point. This package's tests
    share a *session-scoped* event loop (see ``anyio_backend`` above),
    and anyio's test runner executes fixture async-gens and the test
    coroutine as separate steps on it. Two earlier designs failed
    here: SIGALRM-based timeouts hung cleanup paths when the signal
    raced anyio's task-group teardown; an autouse async fixture doing
    ``with anyio.fail_after(...): yield`` put the cancel scope across
    the fixture's yield (anyio's documented cancel-scope-across-yield
    violation), so a firing deadline cancelled across task boundaries,
    corrupted the shared ``TestRunner``, and every subsequent test in
    the session errored at setup with ``ClosedResourceError``.

    Default is :data:`_TEST_TIMEOUT`. Tests that legitimately need
    longer can override via ``@pytest.mark.deadline(seconds=N)``.
    Sync test functions (none in this package today) pass through
    without a deadline — a blocking sync body wouldn't respond to
    anyio cancellation anyway.
    """
    orig = pyfuncitem.obj
    if inspect.iscoroutinefunction(orig):
        deadline = _deadline_for(pyfuncitem)

        @functools.wraps(orig)
        async def _with_deadline(*args: Any, **kwargs: Any) -> Any:
            with anyio.fail_after(deadline):
                return await orig(*args, **kwargs)

        pyfuncitem.obj = _with_deadline
    try:
        return (yield)
    finally:
        pyfuncitem.obj = orig


@pytest.fixture(scope="session")
async def sandbox_env_supervisor() -> AsyncIterator[SandboxEnvSupervisor]:
    """Session-scoped, pre-bootstrapped :class:`SandboxEnvSupervisor`.

    The supervisor builds an ``eryx.SandboxFactory`` from the in-tree
    ``sandbox_runtime`` stub — a ~3 s operation per pytest session
    (cached site-packages on subsequent sessions, so it's faster on
    re-run). E2E tests inject this supervisor into their per-test
    :class:`AppContext` via :func:`fxtr_workstation_mcp.server.make_app_ctx`'s
    ``sandbox_env`` kwarg so each test reuses the same bootstrapped
    factory instead of paying the ~3 s cost per test.

    The supervisor's task lives on a session-scoped task group
    opened here and only torn down at session end. Per-test
    :class:`AppContext` exits do **not** call ``shutdown()`` on this
    supervisor — see :func:`make_app_ctx`'s ``sandbox_env`` doc.

    Requires ``anyio_backend`` to be session-scoped (configured
    above) so the event loop persists across the tests sharing
    this fixture.
    """
    DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    async with anyio.create_task_group() as tg:
        supervisor = SandboxEnvSupervisor(cache_dir=DEFAULT_CACHE_DIR)
        supervisor.start(tg)
        # Pre-warm: block until the eryx factory has built so the
        # first ``run_script`` of the first test doesn't pay the
        # bootstrap cost.
        await supervisor.get_env()
        try:
            yield supervisor
        finally:
            await supervisor.shutdown()
            tg.cancel_scope.cancel()
