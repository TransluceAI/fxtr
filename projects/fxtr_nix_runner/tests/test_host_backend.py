"""Host backend behavior + the shared-matrix smoke test."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fxtr_nix_runner import HostBackend
from fxtr_nix_runner._backend import Backend

# ---------------------------------------------------------------------------
# Host-specific behavior (direct, not via the sweep fixture)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_host_flake_ref_root_is_resolved_target(tmp_path: Path):
    backend = HostBackend()
    sub = tmp_path / "repo"
    sub.mkdir()
    # The handle is bound to the path builder_for was scoped to.
    async with backend.builder_for(sub) as h:
        assert h.flake_ref_root() == str(sub.resolve())


@pytest.mark.anyio
async def test_host_keep_builder_warm_is_noop(tmp_path: Path):
    # There is no container to keep warm on the host backend; the CM must just
    # enter/exit cleanly, and builder_for inside it still works.
    backend = HostBackend()
    async with backend.keep_builder_warm(tmp_path):
        async with backend.builder_for(tmp_path) as h:
            assert h.flake_ref_root() == str(tmp_path.resolve())


@pytest.mark.anyio
async def test_host_materialize_is_identity(tmp_path: Path):
    backend = HostBackend()
    async with backend.builder_for(tmp_path) as h:
        mat = await h.materialize("/nix/store/abc-thing", closure=True)
        assert mat.host_path == Path("/nix/store/abc-thing")
        assert mat.remap is None


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("nix") is None, reason="host nix not on PATH")
async def test_host_run_nix_shells_out(tmp_path: Path):
    backend = HostBackend()
    async with backend.builder_for(tmp_path) as h:
        result = await h.run_nix(["--version"])
    assert result.returncode == 0
    assert "nix" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Transport hooks: endpoint exposure + run_nix is pure passthrough (no nix)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_host_expose_host_endpoint_is_loopback(tmp_path: Path):
    backend = HostBackend()
    async with backend.builder_for(tmp_path) as h:
        async with h.expose_host_endpoint(8080) as ep:
            # Host builds reach a host-bound server over loopback directly.
            assert ep.url_for_builds == "http://127.0.0.1:8080"


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.mark.anyio
async def test_host_run_nix_is_pure_passthrough(tmp_path: Path, monkeypatch):
    # run_nix runs exactly `nix <argv>` — no interpretation or injection (env
    # delivery is the manager's job; see test_manager). Confirms the transport
    # tier stays dumb.
    captured: dict[str, list[str]] = {}

    async def fake_run_process(command, **_kwargs):
        captured["command"] = list(command)
        return _FakeProc()

    monkeypatch.setattr("fxtr_nix_runner._host.anyio.run_process", fake_run_process)
    backend = HostBackend()
    async with backend.builder_for(tmp_path) as h:
        await h.run_nix(["build", "--no-link", "ref"])
    assert captured["command"] == ["nix", "build", "--no-link", "ref"]


# ---------------------------------------------------------------------------
# Shared matrix: runs under each available backend (podman skips until ch.4)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_nix_version_smoke(nix_backend: Backend, tmp_path: Path):
    async with nix_backend.builder_for(tmp_path) as h:
        result = await h.run_nix(["--version"])
    assert result.returncode == 0
    assert "nix" in result.stdout.lower()
