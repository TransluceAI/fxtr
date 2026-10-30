"""Pytest configuration and fixtures for fxtr_sandboxing tests.

Copied from projects/fxtr_workstation/tests/conftest.py — applies a
SIGALRM-based per-test timeout so deadlocks fail fast instead of hanging
the whole suite.
"""

from __future__ import annotations

import os
import shutil
import signal
import warnings
from collections.abc import Iterator

import pytest
from fxtr_nix_runner import load_config
from fxtr_nix_runner.testing import skip_if_runtime_unconfigured

_TEST_TIMEOUT = 30


@pytest.fixture(autouse=True)
def _require_runtime_for_nix_tests(request: pytest.FixtureRequest) -> None:
    """Skip a ``nix_runtime``-marked test when no nix backend is configured.

    Scoped to the marker so unrelated unit tests run without a configured
    backend; the ``FXTR_TESTS_REQUIRE_RUNTIME`` guard flips skip→fail (CI).
    """
    if request.node.get_closest_marker("nix_runtime"):
        skip_if_runtime_unconfigured()


#: Container runtimes the E2E container tests sweep over. podman is the
#: recommended default; Apple Container (``container``) is a supported
#: non-default backend with genuinely different delivery + socket
#: plumbing (tar-fallback delivery, direct socket-file mounts) — swept
#: so it can't rot silently. docker stays out: it's a compatibility
#: escape hatch, not a tested backend.
SWEEP_CONTAINER_RUNTIMES = ("podman", "container")


@pytest.fixture(params=SWEEP_CONTAINER_RUNTIMES)
def container_runtime(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Parameterize a container test over all supported runtimes.

    Each param skips when its runtime isn't on PATH. Installed but not
    running (podman machine stopped / Apple Container service not
    started) **fails** rather than skips — deselect a backend explicitly
    (e.g. ``-k podman``) if that's not what you want. Overrides any
    ambient ``FXTR_CONTAINER_RUNTIME`` (with a warning) so the sweep's
    coverage doesn't silently depend on the caller's environment.
    """
    runtime = request.param
    if shutil.which(runtime) is None:
        pytest.skip(f"container runtime {runtime!r} not on PATH")
    # The podman *nix* backend delivers images only into the podman runtime
    # (it loads into podman's own image store; there's nothing to bridge to a
    # different runtime — see fxtr_nix_runner's deliver_image). So when the nix
    # backend is podman, the non-podman runtime variants can't run the
    # build→deliver path; skip them. Their delivery is exercised under the host
    # nix backend, which can load a host-built image into any runtime.
    if runtime != "podman" and load_config().nix_backend == "podman":
        pytest.skip("podman nix backend delivers only into the podman runtime")
    ambient = os.environ.get("FXTR_CONTAINER_RUNTIME_CMD")
    if ambient is not None and ambient != runtime:
        warnings.warn(
            f"FXTR_CONTAINER_RUNTIME_CMD={ambient!r} is set, but the container tests "
            f"sweep over all supported runtimes ({', '.join(SWEEP_CONTAINER_RUNTIMES)}) "
            "and override it. Deselect runtimes via the pytest invocation instead "
            "(e.g. -k podman).",
            stacklevel=1,
        )
    monkeypatch.setenv("FXTR_CONTAINER_RUNTIME_CMD", runtime)
    return runtime


@pytest.fixture(autouse=True)
def _per_test_timeout(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail any single test that runs longer than its timeout.

    Uses ``signal.alarm`` (POSIX only) so it works for both sync and async
    tests, including deadlocks that wouldn't respond to anyio cancellation.
    On timeout, SIGALRM fires and raises ``TimeoutError`` in the main thread,
    which pytest reports as a test failure.

    Defaults to ``_TEST_TIMEOUT``; a ``@pytest.mark.deadline(seconds=N)``
    marker overrides it — e.g. nix builds offloaded to a remote builder can
    legitimately exceed the default on a cold cache.
    """
    timeout = _TEST_TIMEOUT
    marker = request.node.get_closest_marker("deadline")
    if marker is not None:
        seconds = marker.kwargs.get("seconds")
        if seconds is None and marker.args:
            seconds = marker.args[0]
        if seconds is None:
            raise pytest.UsageError(
                f"@pytest.mark.deadline on {request.node.name!r} must specify the "
                "seconds (e.g. ``@pytest.mark.deadline(seconds=60)``)."
            )
        timeout = int(seconds)

    def _handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(
            f"Test timed out after {timeout} seconds. If this test is an async test, consider"
            " wrapping it in anyio.fail_after to get a more informative traceback."
        )

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
