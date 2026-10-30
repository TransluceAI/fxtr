"""Shared fixtures for the publishing-demo E2E: the parameterized backend sweep.

Mirrors ``projects/fxtr_nix_runner/tests/conftest.py``'s ``nix_backend`` — each
variant (host / podman) skips when its environment is unavailable, so a box with
both runs the full matrix and CI runs whatever it has. Also puts the example root
(for ``seed_data``) and the ``fxtr_workdir_harness`` source on ``sys.path`` (the
harness isn't a dependency of this demo, matching ``scaffold_workdir.py``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import cast

import pytest

_HERE = Path(__file__).resolve().parent
_EXAMPLE_ROOT = _HERE.parent
# examples/fxtr_publishing_demo -> repo root.
_REPO_ROOT = _EXAMPLE_ROOT.parents[1]
sys.path.insert(0, str(_EXAMPLE_ROOT))  # seed_data
sys.path.insert(0, str(_REPO_ROOT / "nix" / "fxtr_workdir_harness" / "src"))

from fxtr_nix_runner import NixRuntimeConfig, resolve_backend  # noqa: E402
from fxtr_nix_runner._backend import Backend  # noqa: E402
from fxtr_nix_runner._config import Backend as BackendName  # noqa: E402
from fxtr_nix_runner._config import PodmanConfig  # noqa: E402

_SWEEP_BACKENDS: tuple[BackendName, ...] = ("host", "podman")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(params=_SWEEP_BACKENDS)
def nix_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Backend:
    """A resolved backend for each supported variant; skips if unavailable."""
    name: BackendName = cast(BackendName, request.param)
    ambient = os.environ.get("FXTR_NIX_BACKEND")
    if ambient is not None and ambient != name:
        warnings.warn(
            f"FXTR_NIX_BACKEND={ambient!r} is set, but the sweep covers all backends "
            f"({', '.join(_SWEEP_BACKENDS)}) and overrides it. Deselect via -k.",
            stacklevel=1,
        )
    monkeypatch.setenv("FXTR_NIX_BACKEND", name)

    if name == "host" and shutil.which("nix") is None:
        pytest.skip("host nix not on PATH")
    if name == "podman" and not _podman_available():
        pytest.skip("podman machine not available")

    # Mount the harness lib source into the podman builder so a workdir mkVenv's
    # live fetchGit of the lib ref resolves at build time (host ignores this).
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


def _podman_available() -> bool:
    if shutil.which("podman") is None:
        return False
    try:
        return subprocess.run(["podman", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
