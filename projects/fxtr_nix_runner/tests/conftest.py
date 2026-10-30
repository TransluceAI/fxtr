"""Shared fixtures, incl. the parameterized backend sweep.

``nix_backend`` is the backend sweep: tests that exercise the backend's own
behavior run against **both** backends as variants, and each variant
**skips** when its environment is unavailable (no host nix; no podman
machine). So a box with both runs the full matrix and CI runs whatever it
has, with neither backend able to rot silently. (Same shape as the
``container_runtime`` sweep in
``projects/fxtr_sandboxing/fxtr_sandboxing/tests/conftest.py``.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from typing import cast

import pytest
from fxtr_nix_runner import NixRuntimeConfig, resolve_backend
from fxtr_nix_runner._backend import Backend
from fxtr_nix_runner._config import Backend as BackendName
from fxtr_nix_runner._config import PodmanConfig

_SWEEP_BACKENDS: tuple[BackendName, ...] = ("host", "podman")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(params=_SWEEP_BACKENDS)
def nix_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Backend:
    """A resolved backend for each supported variant; skips if unavailable.

    Overrides any ambient ``FXTR_NIX_BACKEND`` (with a warning) so the
    sweep's coverage doesn't silently depend on the caller's environment —
    deselect a variant via the pytest invocation (e.g. ``-k host``).
    """
    name: BackendName = cast(BackendName, request.param)
    ambient = os.environ.get("FXTR_NIX_BACKEND")
    if ambient is not None and ambient != name:
        warnings.warn(
            f"FXTR_NIX_BACKEND={ambient!r} is set, but the backend tests sweep over "
            f"all supported backends ({', '.join(_SWEEP_BACKENDS)}) and override it. "
            "Deselect variants via the pytest invocation instead (e.g. -k host).",
            stacklevel=1,
        )
    monkeypatch.setenv("FXTR_NIX_BACKEND", name)

    if name == "host" and shutil.which("nix") is None:
        pytest.skip("host nix not on PATH")
    if name == "podman" and not podman_available():
        pytest.skip("podman machine not available")

    # Mount the harness lib source (the bare mirror in a jj workspace, the repo
    # in a plain-git/CI checkout) into the podman builder, so a workdir flake
    # whose uv venv pulls fxtr2 from that git ref can fetch it at build time.
    # Without this, builds that only need the lib *flake input* (fetched once by
    # prepare_workdir_repo's lock, then cached in /nix) still pass, but a workdir
    # `mkVenv` build's live `builtins.fetchGit` of the lib ref fails. Host
    # ignores mountable_paths, so only resolve for podman.
    mountable: tuple = ()
    if name == "podman":
        from fxtr_workdir_harness import resolve_lib_ref

        mountable = (resolve_lib_ref(validate=False).repo_path,)

    try:
        backend = resolve_backend(
            NixRuntimeConfig(nix_backend=name, podman=PodmanConfig(mountable_paths=mountable))
        )
    except NotImplementedError as exc:
        pytest.skip(str(exc))
    return backend


def podman_available() -> bool:
    """True iff the podman CLI is present and its machine/daemon is reachable."""
    if shutil.which("podman") is None:
        return False
    try:
        return subprocess.run(["podman", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
