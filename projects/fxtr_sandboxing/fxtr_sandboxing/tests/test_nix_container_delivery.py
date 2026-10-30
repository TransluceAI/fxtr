"""Unit test for the sidecar-image build flock (no container runtime).

Image *delivery* (skopeo → runtime store) now lives in the nix-runner
backend (`fxtr_nix_runner`); its unit tests are there. What remains here is
the run-path's sidecar-image build serialization: concurrent cold-store
`ensure_sidecar_image` calls must do a single build (the flock in
`_podman.ensure_sidecar_image`), since the builds are nondeterministic and
every retag loser would otherwise survive as a dangling `<none>` image.

The real delivery + run is covered end-to-end by `test_nix_container_spawner.py`.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.nix_runtime]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


async def test_concurrent_sidecar_builds_build_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """N concurrent cold-store `ensure_sidecar_image` calls do ONE build.

    The flock serializes the inspect+build across processes; waiters re-check
    the image cache once they hold the lock. Without it, every caller would
    see the tag missing and build (and, the builds being nondeterministic,
    every retag loser would survive as a dangling `<none>` image).
    """
    from fxtr_sandboxing import _podman

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))  # lock file home

    class _StatefulFake:
        """`image inspect` misses until a `build` completes."""

        def __init__(self) -> None:
            self.built = False
            self.builds = 0

        async def __call__(self, cmd: list[str], **kwargs: object) -> _FakeResult:
            if cmd[1:3] == ["image", "inspect"]:
                return _FakeResult(returncode=0 if self.built else 1)
            assert cmd[1] == "build"
            self.builds += 1
            await anyio.sleep(0.05)  # keep the race window open
            self.built = True
            return _FakeResult()

    fake = _StatefulFake()
    monkeypatch.setattr(anyio, "run_process", fake)

    refs: list[str] = []

    async def one() -> None:
        refs.append(await _podman.ensure_sidecar_image("podman"))

    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(one)

    assert fake.builds == 1
    assert len(refs) == 3 and len(set(refs)) == 1
