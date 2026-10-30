"""Podman backend: pure ref/path logic + live builder-container behavior.

The container tests need a podman machine (skipped otherwise). They share
global podman state (the fixed ``fxtr-nix`` volumes + builder labels), so
run them serially — ``uv run pytest -n0 -k podman``. The build/materialize
test is marked ``slow`` (first run fetches nixpkgs).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fxtr_nix_runner import ContainerRuntime, PodmanConfig
from fxtr_nix_runner._podman import (
    BuilderTransportError,
    LivenessBeacon,
    PodmanBackend,
    PodmanError,
    PodmanHandle,
    _beacon_health_args,
    _Builder,
    git_repo_root,
)
from tests.conftest import podman_available

requires_podman = pytest.mark.skipif(not podman_available(), reason="podman machine not available")


# ---------------------------------------------------------------------------
# Pure logic (no container)
# ---------------------------------------------------------------------------


def test_git_repo_root_walks_up(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert git_repo_root(sub) == repo.resolve()
    assert git_repo_root(repo) == repo.resolve()


def test_git_repo_root_no_repo_is_identity(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    assert git_repo_root(d) == d.resolve()


def test_flake_ref_root_translation(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    builder = _Builder(container_id="x", repo_root=repo.resolve(), multi_user=True)
    backend = PodmanBackend(PodmanConfig())
    # A handle scoped to the repo root maps to /workdir...
    root_handle = PodmanHandle(
        builder, PodmanConfig(), target=repo.resolve(), backend=backend, repo_root=repo.resolve()
    )
    assert root_handle.flake_ref_root() == "/workdir"
    # ...and one scoped to a subdir maps to /workdir/<relpath>.
    sub = repo / "pkgs" / "thing"
    sub_handle = PodmanHandle(
        builder, PodmanConfig(), target=sub.resolve(), backend=backend, repo_root=repo.resolve()
    )
    assert sub_handle.flake_ref_root() == "/workdir/pkgs/thing"


def test_flake_ref_root_rejects_out_of_repo_target(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    builder = _Builder(container_id="x", repo_root=repo.resolve(), multi_user=True)
    handle = PodmanHandle(
        builder,
        PodmanConfig(),
        target=other.resolve(),
        backend=PodmanBackend(PodmanConfig()),
        repo_root=repo.resolve(),
    )
    with pytest.raises(PodmanError):
        handle.flake_ref_root()


def test_liveness_beacon_alive_then_refuses_after_close():
    beacon = LivenessBeacon()
    try:
        assert beacon.port > 0
        # A dial succeeds while the beacon is alive (mirrors the container's
        # health check reaching host.containers.internal:<port>).
        s = socket.create_connection(("127.0.0.1", beacon.port), timeout=2)
        s.close()
    finally:
        beacon.close()
    # Once closed (as when the owning process dies), dials are refused — which
    # is what makes the builder/deliverer health check fail and self-teardown.
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", beacon.port), timeout=1)


def test_beacon_health_args_shape():
    args = _beacon_health_args(54321)
    assert "--health-on-failure" in args and args[args.index("--health-on-failure") + 1] == "stop"
    cmd = args[args.index("--health-cmd") + 1]
    assert "host.containers.internal/54321" in cmd  # dials the beacon port
    assert "--health-start-period" in args  # startup grace so no false early stop


def _handle_for(tmp_path: Path, builder: _Builder) -> PodmanHandle:
    backend = PodmanBackend(PodmanConfig())
    return PodmanHandle(
        builder,
        PodmanConfig(),
        target=tmp_path.resolve(),
        backend=backend,
        repo_root=tmp_path.resolve(),
    )


@pytest.mark.anyio
async def test_exec_transport_failure_invalidates_and_raises(monkeypatch, tmp_path: Path):
    """rc 125 from a builder exec (container gone) runs `on_dead` and raises
    BuilderTransportError — not returned as a NixResult."""

    async def fake_run(argv):
        return (125, "", 'Error: no container with name or ID "gone" found')

    monkeypatch.setattr("fxtr_nix_runner._podman._run", fake_run)
    builder = _Builder(container_id="gone", repo_root=tmp_path.resolve(), multi_user=True)
    handle = _handle_for(tmp_path, builder)
    dead = {"called": False}

    async def on_dead() -> None:
        dead["called"] = True

    with pytest.raises(BuilderTransportError):
        await handle._exec(["nix", "--version"], on_dead=on_dead)
    assert dead["called"]


@pytest.mark.anyio
async def test_exec_nonzero_nix_exit_is_not_a_transport_failure(monkeypatch, tmp_path: Path):
    """A non-125 nonzero (a real nix error) comes back as a NixResult, and
    `on_dead` is NOT invoked."""

    async def fake_run(argv):
        return (1, "", "error: some nix build failure")

    monkeypatch.setattr("fxtr_nix_runner._podman._run", fake_run)
    builder = _Builder(container_id="alive", repo_root=tmp_path.resolve(), multi_user=True)
    handle = _handle_for(tmp_path, builder)
    dead = {"called": False}

    async def on_dead() -> None:
        dead["called"] = True

    result = await handle._exec(["nix", "build"], on_dead=on_dead)
    assert result.returncode == 1
    assert not dead["called"]


@pytest.mark.anyio
async def test_recycle_deliverer_clears_id(monkeypatch, tmp_path: Path):
    """`_recycle_deliverer` clears the cached (dead) deliverer id so the next
    `_ensure_deliverer` recreates it, and best-effort removes the corpse — the
    builder is untouched."""
    calls: list[list[str]] = []

    async def fake_run(argv):
        calls.append(list(argv))
        return (0, "", "")

    monkeypatch.setattr("fxtr_nix_runner._podman._run", fake_run)
    builder = _Builder(
        container_id="b", repo_root=tmp_path.resolve(), multi_user=False, deliverer_id="d-dead"
    )
    handle = _handle_for(tmp_path, builder)
    await handle._recycle_deliverer()
    assert builder.deliverer_id is None  # recreated on next _ensure_deliverer
    assert builder.container_id == "b"  # builder untouched
    assert ["podman", "rm", "-f", "d-dead"] in calls


def test_mountable_path_args_skips_missing(tmp_path: Path):
    present = tmp_path / "libs"
    present.mkdir()
    missing = tmp_path / "nope"
    backend = PodmanBackend(PodmanConfig(mountable_paths=(present, missing)))
    # Existing dir → RO abspath mount; missing dir → skipped.
    assert backend._mountable_path_args() == ["-v", f"{present}:{present}:ro"]


@pytest.mark.anyio
async def test_clear_issues_expected_podman_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clear`` issues the expected ps / rm / volume-rm / scratch-wipe sequence.

    Mocked at the podman layer (``_clear._run`` + ``shutil.rmtree``) instead of
    run against real podman: a live ``clear`` ``volume rm``s the shared
    ``fxtr-nix`` store, throwing away the warm nixpkgs/closure cache (slowing
    every later test) and its label-sweep can kill a concurrent test's builder.
    The real path is exercised live by the nix-marked tests; this guards the
    command logic.
    """
    from fxtr_nix_runner import _clear
    from fxtr_nix_runner._podman import CACHE_VOLUME, PODMAN, STORE_VOLUME

    calls: list[list[str]] = []

    async def fake_run(argv: list[str]) -> tuple[int, str, str]:
        calls.append(list(argv))
        # `ps -aq` reports one stray container (per label); everything succeeds.
        return (0, "cid-123\n", "") if "ps" in argv else (0, "", "")

    rmtree_calls: list[Path] = []
    monkeypatch.setattr(_clear, "_run", fake_run)
    monkeypatch.setattr(_clear.shutil, "rmtree", lambda p, **_: rmtree_calls.append(p))

    mat_root = tmp_path / "mat"
    mat_root.mkdir()
    removed = await _clear.clear(PodmanConfig(materialize_root=mat_root))

    # A `ps` query + force-rm per container label...
    assert [PODMAN, "ps", "-aq", "--filter", "label=fxtr-nix-builder"] in calls
    assert [PODMAN, "ps", "-aq", "--filter", "label=fxtr-nix-deliverer"] in calls
    assert calls.count([PODMAN, "rm", "-f", "cid-123"]) == 2
    # ...a force-rm of each shared volume...
    assert [PODMAN, "volume", "rm", "-f", STORE_VOLUME] in calls
    assert [PODMAN, "volume", "rm", "-f", CACHE_VOLUME] in calls
    # ...and the materialize scratch wiped (never the shared store/cache dir).
    assert rmtree_calls == [mat_root]
    # `removed` names exactly what was removed.
    assert "container cid-123 (label fxtr-nix-builder)" in removed
    assert "container cid-123 (label fxtr-nix-deliverer)" in removed
    assert f"volume {STORE_VOLUME}" in removed
    assert f"volume {CACHE_VOLUME}" in removed
    assert f"materialize scratch {mat_root}" in removed


# ---------------------------------------------------------------------------
# Live builder container
# ---------------------------------------------------------------------------


@requires_podman
@pytest.mark.anyio
async def test_builder_run_nix_version(tmp_path: Path):
    """Default (multi-user) builder: starts the container, bootstraps the
    daemon, and execs nix inside it."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    backend = PodmanBackend(PodmanConfig())
    async with backend.builder_for(repo) as h:
        result = await h.run_nix(["--version"])
    assert result.returncode == 0, result.stderr
    assert "nix" in result.stdout.lower()


@requires_podman
@pytest.mark.anyio
async def test_builder_is_shared_and_torn_down(tmp_path: Path):
    """Refcounted: nested borrows reuse one container; it's gone after."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    backend = PodmanBackend(PodmanConfig())
    async with backend.builder_for(repo) as h1:
        cid1 = h1._builder.container_id
        async with backend.builder_for(repo) as h2:
            assert h2._builder.container_id == cid1  # same container
        assert backend._registry.active_keys() == [repo.resolve()]
    assert backend._registry.active_keys() == []


@requires_podman
@pytest.mark.anyio
async def test_warm_builder_recycled_after_death(tmp_path: Path):
    """A warm builder that dies mid-env is recycled: the run_nix that hits the
    dead container raises BuilderTransportError (and invalidates it), and the
    next borrow rebuilds a fresh builder under the still-held pin."""
    import anyio

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    backend = PodmanBackend(PodmanConfig())
    async with backend.keep_builder_warm(repo):
        async with backend.builder_for(repo) as h1:
            assert (await h1.run_nix(["--version"])).returncode == 0
            cid1 = h1._builder.container_id
        # Kill the warm builder out from under the env (simulates a VM restart /
        # a concurrent `fxtr_nix clear` / an OOM).
        await anyio.run_process(["podman", "rm", "-f", "-t", "0", cid1], check=True)
        # The pinned entry still points at the dead container: the next exec
        # detects it (rc 125), invalidates, and raises.
        async with backend.builder_for(repo) as h2:
            with pytest.raises(BuilderTransportError):
                await h2.run_nix(["--version"])
        # After invalidation the pin survives, so a fresh borrow rebuilds.
        async with backend.builder_for(repo) as h3:
            assert (await h3.run_nix(["--version"])).returncode == 0
            assert h3._builder.container_id != cid1  # genuinely a new builder
    assert backend._registry.active_keys() == []  # pin released → torn down


@requires_podman
@pytest.mark.anyio
async def test_lock_writes_flake_lock_to_workdir(tmp_path: Path):
    """`lock()` runs `nix flake lock` in the (RW) builder and the new
    flake.lock lands on the host workdir via the bind mount."""
    repo = tmp_path / "repo"
    (repo / "dep").mkdir(parents=True)
    # An in-tree input (no network) so there's something to lock → a
    # flake.lock is actually written.
    (repo / "dep" / "flake.nix").write_text("{ outputs = _: { }; }\n")
    (repo / "flake.nix").write_text('{ inputs.dep.url = "path:./dep"; outputs = _: { }; }\n')
    backend = PodmanBackend(PodmanConfig())
    async with backend.builder_for(repo) as h:
        await h.lock()
    assert (repo / "flake.lock").is_file()


@requires_podman
@pytest.mark.slow
@pytest.mark.anyio
async def test_materialize_roundtrip(tmp_path: Path):
    """Build a tiny nixpkgs derivation in-container, materialize it, and
    read the result host-side; the remap points at the host store."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    mat_root = tmp_path / "mat"
    config = PodmanConfig(materialize_root=mat_root)
    expr = (
        'let pkgs = (builtins.getFlake "github:NixOS/nixpkgs/nixos-26.05")'
        '.legacyPackages.x86_64-linux; in pkgs.runCommand "mat-test" {} '
        '"echo materialized-ok > $out"'
    )
    backend = PodmanBackend(config)
    async with backend.builder_for(repo) as h:
        built = await h.run_nix(
            ["build", "--no-link", "--print-out-paths", "--impure", "--expr", expr]
        )
        assert built.returncode == 0, built.stderr
        store_path = built.stdout.strip().splitlines()[-1]
        mat = await h.materialize(store_path, closure=True)

    assert mat.host_path.is_file()
    assert mat.host_path.read_text().strip() == "materialized-ok"
    assert mat.remap == (Path("/nix/store"), mat_root / "nix" / "store")
    # The host path lies under the remap's new prefix for the basename.
    assert mat.host_path == (mat_root / "nix" / "store" / Path(store_path).name)


@requires_podman
@pytest.mark.slow
@pytest.mark.anyio
async def test_deliver_image_via_deliverer(tmp_path: Path):
    """Build a nix2container image in the builder, deliver it through the
    deliverer container, and run the delivered ref host-side."""
    import subprocess

    from fxtr_nix_runner._delivery import NIX2CONTAINER_REV

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    backend = PodmanBackend(PodmanConfig(materialize_root=tmp_path / "mat"))
    expr = (
        f'let n2c = (builtins.getFlake "github:nlewo/nix2container/{NIX2CONTAINER_REV}")'
        ".packages.x86_64-linux.nix2container; "
        'pkgs = (builtins.getFlake "github:NixOS/nixpkgs/nixos-26.05")'
        ".legacyPackages.x86_64-linux; in "
        'n2c.buildImage { name = "fxtr-deliver-test"; '
        'config = { Cmd = [ "${pkgs.coreutils}/bin/echo" "delivered-ok" ]; }; }'
    )
    async with backend.builder_for(repo) as h:
        built = await h.run_nix(
            ["build", "--no-link", "--print-out-paths", "--impure", "--expr", expr]
        )
        assert built.returncode == 0, built.stderr
        image_json = built.stdout.strip().splitlines()[-1]
        delivered = await h.deliver_image(
            image_json, runtime=ContainerRuntime(cmd="podman", type="podman")
        )

    # The image is now in the machine's podman store and runs.
    inspect = subprocess.run(
        ["podman", "image", "inspect", delivered.image_ref], capture_output=True
    )
    assert inspect.returncode == 0, inspect.stderr
    run = subprocess.run(
        ["podman", "run", "--rm", "--platform", delivered.platform, delivered.image_ref],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert "delivered-ok" in run.stdout
    assert delivered.platform == "linux/amd64"
