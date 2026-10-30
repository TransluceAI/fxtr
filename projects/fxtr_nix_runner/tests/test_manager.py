"""Tests for the FlakeManager (manager tier — exercises fxtr2, host backend).

No nix builds needed: these cover the wiring — repo-store exposure resolves
by-id reads, and a build span stands up a rehost whose env (URL + token) is
actually reachable. The real FOD-fetch-through-nix E2E lives in the dedicated
integration step.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import anyio
import cbor2
import httpx
import pytest
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawWeakFxtrID,
    fxtr_cbor_loads,
)
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr_nix_runner import HostBackend
from fxtr_nix_runner._backend import NixResult
from fxtr_nix_runner.manager import BuildSpan, FlakeManager
from fxtr_nix_runner.rehost import REMOTE_BASE_URL_ENV, REMOTE_BEARER_TOKEN_ENV, RehostInfo

pytestmark = pytest.mark.anyio


# --- helpers (mirror v2/tests/test_repo_storage.py) -------------------------


@pytest.fixture
def memory_arena():
    store = MemoryStore()
    ull = UnconditionalLocalLifetime("test")
    store.register_token(ull.token, name="test")
    root_lease = ull.root_lease.take()
    arena = store.new_arena(root_lease.clone())
    typing.cast(Any, arena)._test_lease = root_lease
    try:
        yield arena
    finally:
        arena.close()
        root_lease.discard()


def _mem_l(memory_arena: MemoryArena) -> Any:
    return typing.cast(Any, memory_arena)._test_lease.clone()


async def _create_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "T"], cwd=path, check=True)


def _create_test_entity(memory_arena: MemoryArena, value: str) -> FxtrID:
    ground_context = FxtrID.from_tag_and_payload(".", b"")
    memory_arena.track_builtin(_mem_l(memory_arena), ground_context)
    type_id = FxtrID.builtin("manager_test_entity")
    memory_arena.track_builtin(_mem_l(memory_arena), type_id)
    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
    }
    return memory_arena.store(_mem_l(memory_arena), entity)


async def _populate_store(repo: Path, memory_arena: MemoryArena, value: str) -> FxtrID:
    """Seed the repo's on-disk store with one entity (the manager reopens it)."""
    entity_id = _create_test_entity(memory_arena, value)
    async with RepoEntityStore.open(repo, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, _mem_l(memory_arena))
    return entity_id


# --- tests ------------------------------------------------------------------


async def test_manager_repo_store_resolves_by_id(tmp_path: Path, memory_arena: MemoryArena):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    entity_id = await _populate_store(repo, memory_arena, "exposed")

    async with FlakeManager(repo, backend=HostBackend()) as mgr:
        source, lease = mgr.repo_store
        async with FxtrScope(name="consumer", isolated=True) as consumer:
            consumer.register_deep_fetch_source(source, IndependentLocalBridge(lease=lease))
            arena = await consumer._deep_fetch(entity_id)
            assert arena is not None
            data = typing.cast(Any, await arena.load(consumer.clone_authorization(), entity_id))
            assert data["value"] == "exposed"
            arena.close()


async def test_manager_open_creates_store_for_fresh_repo(tmp_path: Path):
    """open() on a repo with no .fxtr/ mints one (create-if-absent)."""
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    assert not (repo / ".fxtr").exists()
    async with FlakeManager(repo, backend=HostBackend()):
        pass
    assert (repo / ".fxtr").exists()


async def test_manager_build_span_rehost_is_reachable(tmp_path: Path, memory_arena: MemoryArena):
    repo = tmp_path / "repo"
    await _create_git_repo(repo)
    entity_id = await _populate_store(repo, memory_arena, "via_rehost")

    async with FlakeManager(repo, backend=HostBackend()) as mgr:
        async with mgr.build_span() as span:
            # Host backend: build-visible URL is loopback, so it's reachable here.
            assert span.build_env[REMOTE_BASE_URL_ENV].startswith("http://127.0.0.1:")
            assert REMOTE_BEARER_TOKEN_ENV in span.build_env
            url = span.build_env[REMOTE_BASE_URL_ENV]
            token = span.build_env[REMOTE_BEARER_TOKEN_ENV]
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{url}/data/{entity_id.id_string()}",
                    headers={"Authorization": f"Bearer {token}"},
                )
    assert r.status_code == 200
    result = typing.cast(Any, cbor2.loads(r.content))
    decoded = typing.cast(Any, fxtr_cbor_loads(result["bytes"]))
    assert decoded["value"] == "via_rehost"


async def test_build_span_run_nix_prepends_rehost_env() -> None:
    """BuildSpan.run_nix prepends the impure-env flags as *global* flags (before
    the argv) — the env-delivery policy lives here, not in the transport tier."""
    captured: dict[str, list[str]] = {}

    class _CaptureHandle:
        async def run_nix(self, argv, **_kw) -> NixResult:
            captured["argv"] = list(argv)
            return NixResult(returncode=0, stdout="", stderr="")

    span = BuildSpan(
        handle=typing.cast(Any, _CaptureHandle()),
        build_env={REMOTE_BASE_URL_ENV: "http://127.0.0.1:9", REMOTE_BEARER_TOKEN_ENV: "tok"},
        rehost=RehostInfo(base_url="http://127.0.0.1:9", token="tok", port=9),
    )
    await span.run_nix(["build", "--no-link", "ref"])

    argv = captured["argv"]
    # The caller's argv is preserved verbatim at the end; flags are prepended
    # *before* the subcommand (global options), so no positional splicing.
    assert argv[-3:] == ["build", "--no-link", "ref"]
    assert "configurable-impure-env" in argv
    i = argv.index("impure-env")
    assert i < argv.index("build")
    assert argv[i + 1] == "FXTR_REMOTE_BASE_URL=http://127.0.0.1:9 FXTR_REMOTE_BEARER_TOKEN=tok"
