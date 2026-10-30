"""Tests for RepoEntityStore — the standalone by-id repo entity store.

Covers open-existing, create-if-absent, read/write/try_lookup round trip,
read-only open, the create+read_only guard, and DeepFetchable adoption into a
scope (the shape the runner/serving script use).
"""

from __future__ import annotations

import subprocess
import sys
import typing
from pathlib import Path
from typing import Any

import anyio
import pytest

from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.repo_storage_config import REPO_CONFIG_FILENAME
from fxtr2.data.tsv_namespace import FxtrConfigError
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.local_lease import UnconditionalLocalLifetime

pytestmark = [pytest.mark.anyio, pytest.mark.flaky(reruns=2)]


# =============================================================================
# Fixtures + helpers (mirrors tests/test_tsv_namespace/test_tsv_namespace.py)
# =============================================================================


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Monkeypatch Path.home() to return a temp directory."""
    fake_home_dir = tmp_path / "fake_home"
    fake_home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home_dir)
    return fake_home_dir


@pytest.fixture
def memory_store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def memory_arena(memory_store: MemoryStore):
    _ull = UnconditionalLocalLifetime("test")
    memory_store.register_token(_ull.token, name="test")
    root_lease = _ull.root_lease.take()
    arena = memory_store.new_arena(root_lease.clone())
    typing.cast(Any, arena)._test_lease = root_lease
    try:
        yield arena
    finally:
        arena.close()
        root_lease.discard()


def mem_l(memory_arena: MemoryArena) -> Any:
    """Lease clone for ops on entities in the test memory arena."""
    return typing.cast(Any, memory_arena)._test_lease.clone()


async def create_git_repo(path: Path) -> None:
    """Initialize a git repository at the given path."""
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def create_test_entity(memory_arena: MemoryArena, value: str) -> FxtrID:
    """Create a simple test entity and store it in the arena."""
    ground_context = FxtrID.from_tag_and_payload(".", b"")
    memory_arena.track_builtin(mem_l(memory_arena), ground_context)
    type_id = FxtrID.builtin("repo_store_test_entity")
    memory_arena.track_builtin(mem_l(memory_arena), type_id)

    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
    }
    return memory_arena.store(mem_l(memory_arena), entity)


# =============================================================================
# import hygiene (regression guard)
# =============================================================================


def test_repo_storage_importable_standalone():
    """`import fxtr2.data.repo_storage` must work as the *first* fxtr2.data import.

    Regression guard for a circular import: repo_storage is the lower layer
    (the tsv namespace is a view over it), so it must not pull in the tsv_namespace package
    at module load. The runner does exactly this bare import. Run in a fresh
    subprocess so prior imports in this session can't prime/mask the cycle.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import fxtr2.data.repo_storage"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"standalone import failed:\n{result.stderr}"


# =============================================================================
# open / create
# =============================================================================


async def test_create_if_absent(tmp_path: Path):
    """open(create=True) initializes .fxtr/ + repo config when absent."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    assert not (repo / ".fxtr").exists()

    async with RepoEntityStore.open(repo, create=True) as store:
        assert store.token is not None

    # The repo config was created (sibling of the VCS root).
    assert (repo / ".fxtr" / REPO_CONFIG_FILENAME).exists()


async def test_grant_matches_lease_grant(tmp_path: Path):
    """`store.grant` is the session's canonical grant — equal to the grant of
    any lease from `get_lease()` (all clones share it)."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    async with RepoEntityStore.open(repo, create=True) as store:
        lease = store.get_lease()
        assert store.grant == lease.grant
        assert store.grant.subject is store.token
        lease.discard()


async def test_open_existing(tmp_path: Path):
    """A store created once can be reopened with create=False."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    async with RepoEntityStore.open(repo, create=True):
        pass

    # Reopen without create — must succeed.
    async with RepoEntityStore.open(repo, create=False) as store:
        assert store.token is not None


async def test_open_missing_without_create_raises(tmp_path: Path):
    """open(create=False) on a repo with no .fxtr/ raises a config error."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    with pytest.raises(FxtrConfigError):
        async with RepoEntityStore.open(repo, create=False):
            pass


async def test_create_and_read_only_mutually_exclusive(tmp_path: Path):
    """create + read_only is rejected (read-only sessions skip schema init)."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    with pytest.raises(ValueError, match="not allowed"):
        async with RepoEntityStore.open(repo, create=True, read_only=True):
            pass


# =============================================================================
# read / write / try_lookup
# =============================================================================


async def test_write_read_try_lookup_roundtrip(tmp_path: Path, memory_arena: MemoryArena):
    """Write an entity by id, then read it back via read()/try_lookup()."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    entity_id = create_test_entity(memory_arena, "hello")

    async with RepoEntityStore.open(repo, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, mem_l(memory_arena))

        for arena in (await store.read(entity_id), await store.try_lookup(entity_id)):
            assert arena is not None
            data = typing.cast(Any, await arena.load(store.get_lease(), entity_id))
            assert data["value"] == "hello"
            arena.close()


async def test_try_lookup_missing_returns_none(tmp_path: Path):
    """try_lookup of an id that was never written returns None."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)

    async with RepoEntityStore.open(repo, create=True) as store:
        assert await store.try_lookup(fake_id) is None


async def test_write_is_idempotent(tmp_path: Path, memory_arena: MemoryArena):
    """Writing the same entity twice is fine (overwrites the by-id entry)."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    entity_id = create_test_entity(memory_arena, "twice")

    async with RepoEntityStore.open(repo, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, mem_l(memory_arena))
        await store.write_from_arena(entity_id, memory_arena, mem_l(memory_arena))
        arena = await store.try_lookup(entity_id)
        assert arena is not None
        arena.close()


# =============================================================================
# read-only
# =============================================================================


async def test_read_only_open_reads_existing(tmp_path: Path, memory_arena: MemoryArena):
    """A read-only reopen can read entities written by a prior writable session."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    entity_id = create_test_entity(memory_arena, "persisted")

    async with RepoEntityStore.open(repo, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, mem_l(memory_arena))

    # Reopen read-only and read the persisted entity back.
    async with RepoEntityStore.open(repo, read_only=True) as store:
        arena = await store.try_lookup(entity_id)
        assert arena is not None
        data = typing.cast(Any, await arena.load(store.get_lease(), entity_id))
        assert data["value"] == "persisted"
        arena.close()


# =============================================================================
# DeepFetchable adoption into a scope
# =============================================================================


async def test_deep_fetch_adoption_into_scope(tmp_path: Path, memory_arena: MemoryArena):
    """A RepoEntityStore registered as a deep-fetch source resolves by-id reads."""
    repo = tmp_path / "repo"
    await create_git_repo(repo)
    entity_id = create_test_entity(memory_arena, "from_store")

    async with RepoEntityStore.open(repo, create=True) as store:
        await store.write_from_arena(entity_id, memory_arena, mem_l(memory_arena))

        async with FxtrScope(name="consumer", isolated=True) as consumer:
            consumer.register_deep_fetch_source(
                store, IndependentLocalBridge(lease=store.get_lease())
            )
            # The store is consulted for an id the consumer scope has never seen,
            # and the bytes resolve through it into the consumer scope. Load with
            # a consumer-scope lease (the deep-fetched arena is the consumer's).
            arena = await consumer._deep_fetch(entity_id)
            assert arena is not None
            data = typing.cast(Any, await arena.load(consumer.clone_authorization(), entity_id))
            assert data["value"] == "from_store"
            arena.close()


# NOTE: the wrong-brand-lease leak in MemoryArena.load (root cause of the
# teardown hang seen during stage-14 dev) is tracked by a targeted red test in
# tests/test_memory_arena.py — it doesn't need the RepoEntityStore/tsv-namespace machinery.
