"""Integration tests for tsv namespace staleness checking with TsvNamespaceBackend."""

from pathlib import Path
from typing import Any

import anyio
import pytest

from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.tsv_namespace import TsvNamespaceBackend
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.leases.local_lease import UnconditionalLocalLifetime

pytestmark = [pytest.mark.anyio, pytest.mark.flaky(reruns=2)]


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch Path.home() to return a temp directory."""
    fake_home_dir = tmp_path / "fake_home"
    fake_home_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home_dir)
    return fake_home_dir


async def create_git_repo(path: Path) -> None:
    """Initialize a git repository at the given path."""
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    await anyio.run_process(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path,
        check=True,
    )
    await anyio.run_process(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
    )


def mem_l(memory_arena: MemoryArena) -> Any:
    """Lease clone for ops on the test memory arena."""
    return memory_arena._test_lease.clone()  # type: ignore[attr-defined]


def tsv_backend_lease(ns: TsvNamespaceBackend) -> Any:
    return ns._session.root_lease.clone()


def create_test_entity(memory_arena: MemoryArena, value: str) -> FxtrID:
    """Create a simple test entity and store it in the arena."""
    ground_context = FxtrID.from_tag_and_payload(".", b"")
    memory_arena.track_builtin(mem_l(memory_arena), ground_context)
    type_id = FxtrID.builtin("test_entity")
    memory_arena.track_builtin(mem_l(memory_arena), type_id)

    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
    }
    return memory_arena.store(mem_l(memory_arena), entity)


@pytest.fixture
def memory_arena():
    """Fixture providing a memory arena and driving lease (``_test_lease`` on arena)."""
    store = MemoryStore()
    _ull = UnconditionalLocalLifetime("test")
    store.register_token(_ull.token, name="test")
    root_lease = _ull.root_lease.take()
    arena = store.new_arena(root_lease.clone())
    arena._test_lease = root_lease  # type: ignore[attr-defined]
    try:
        yield arena
    finally:
        arena.close()
        root_lease.discard()


class TestTsvNamespaceStaleness:
    """Integration tests for staleness checking in TsvNamespaceBackend."""

    async def test_rapid_successive_retrieve_pinned(self, tmp_path: Path, memory_arena):
        """Rapid successive retrieval should benefit from staleness cache."""
        entity_id = create_test_entity(memory_arena, "test_value")

        # Create git repo
        await create_git_repo(tmp_path)

        # Initialize tsv namespace
        await TsvNamespaceBackend.init(tmp_path)

        # Open with staleness threshold
        async with TsvNamespaceBackend.open(
            tmp_path, sync_staleness_threshold=0.5  # 500ms window
        ) as ns:
            # Pin an entity
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))
            await ns.flush()

            # First retrieve - will sync
            id1, arena1 = await ns.retrieve_pinned("mykey")
            arena1.close()
            assert id1 == entity_id

            # Rapid subsequent retrievals - should skip sync due to staleness
            for _ in range(10):
                id_n, arena_n = await ns.retrieve_pinned("mykey")
                arena_n.close()
                assert id_n == entity_id

            # All retrievals succeeded and returned correct entity
            # (Detailed verification that staleness cache was used would require
            # inspecting logs or mocking, but functional correctness is verified)

    async def test_staleness_does_not_affect_correctness(self, tmp_path: Path, memory_arena):
        """Staleness should not affect correctness, only skip redundant syncs."""
        entity_id1 = create_test_entity(memory_arena, "test_value_1")
        entity_id2 = create_test_entity(memory_arena, "test_value_2")

        # Create git repo
        await create_git_repo(tmp_path)

        # Initialize tsv namespace
        await TsvNamespaceBackend.init(tmp_path)

        # Open with staleness threshold
        async with TsvNamespaceBackend.open(
            tmp_path, sync_staleness_threshold=1.0  # 1s window
        ) as ns:
            # Pin first entity
            await ns.pin_entity_from_arena("key1", entity_id1, memory_arena, mem_l(memory_arena))
            await ns.flush()

            # Retrieve it (syncs TSV)
            id1, arena1 = await ns.retrieve_pinned("key1")
            arena1.close()
            assert id1 == entity_id1

            # Within staleness window, pin another entity
            # Even though we're within staleness window, this should work correctly
            await ns.pin_entity_from_arena("key2", entity_id2, memory_arena, mem_l(memory_arena))
            await ns.flush()

            # Verify both entities are present
            all_pins = await ns.list_pins()
            assert len(all_pins) == 2
            assert all_pins["key1"] == entity_id1
            assert all_pins["key2"] == entity_id2

    async def test_clear_staleness_cache_by_key(self, tmp_path: Path, memory_arena):
        """clear_staleness_cache by key should work without errors."""
        entity_id = create_test_entity(memory_arena, "test_value")

        # Create git repo
        await create_git_repo(tmp_path)

        # Initialize tsv namespace
        await TsvNamespaceBackend.init(tmp_path)

        # Open with staleness threshold
        async with TsvNamespaceBackend.open(
            tmp_path, sync_staleness_threshold=1.0  # 1s window
        ) as ns:
            # Pin and retrieve
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))
            await ns.flush()

            id1, arena1 = await ns.retrieve_pinned("mykey")
            arena1.close()
            assert id1 == entity_id

            # Clear staleness cache for this key
            ns.clear_staleness_cache("mykey")

            # Retrieve again - should still work correctly
            id2, arena2 = await ns.retrieve_pinned("mykey")
            arena2.close()
            assert id2 == entity_id

    async def test_clear_staleness_cache_all(self, tmp_path: Path, memory_arena):
        """clear_staleness_cache() should clear all caches without errors."""
        entity_id1 = create_test_entity(memory_arena, "test_value_1")
        entity_id2 = create_test_entity(memory_arena, "test_value_2")

        # Create git repo
        await create_git_repo(tmp_path)

        # Initialize tsv namespace
        await TsvNamespaceBackend.init(tmp_path)

        # Open with staleness threshold
        async with TsvNamespaceBackend.open(
            tmp_path, sync_staleness_threshold=1.0  # 1s window
        ) as ns:
            # Pin two entities to different TSVs
            await ns.pin_entity_from_arena("key1", entity_id1, memory_arena, mem_l(memory_arena))
            await ns.pin_entity_from_arena(
                "data/models:key2", entity_id2, memory_arena, mem_l(memory_arena)
            )
            await ns.flush()

            # Retrieve both (syncs both TSVs)
            _, a1 = await ns.retrieve_pinned("key1")
            a1.close()
            _, a2 = await ns.retrieve_pinned("data/models:key2")
            a2.close()

            # Clear all staleness caches
            ns.clear_staleness_cache()

            # Both should still work correctly after clearing cache
            id1_new, a1_new = await ns.retrieve_pinned("key1")
            a1_new.close()
            id2_new, a2_new = await ns.retrieve_pinned("data/models:key2")
            a2_new.close()

            assert id1_new == entity_id1
            assert id2_new == entity_id2
