"""Tests for TsvNamespaceBackend - the main tsv namespace implementation."""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import anyio
import pytest

from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.tsv_namespace import FxtrConfigError, TsvNamespaceBackend, read_tsv, write_tsv
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


def create_deeply_nested_entity(memory_arena: MemoryArena, value: str, depth: int = 20) -> FxtrID:
    """Create a deeply nested entity with `depth` levels of nesting.

    Each level is an entity that references the next level via a strong reference.
    The leaf entity contains the actual value. This creates a chain of entities
    that must all be copied when pinning, making the operation slower and more
    likely to expose race conditions.

    Args:
        memory_arena: The arena to store entities in.
        value: The value to store in the leaf entity.
        depth: Number of nesting levels (default 20).

    Returns:
        The FxtrID of the root (outermost) entity.
    """
    ground_context = FxtrID.from_tag_and_payload(".", b"")
    memory_arena.track_builtin(mem_l(memory_arena), ground_context)
    type_id = FxtrID.builtin("nested_test_entity")
    memory_arena.track_builtin(mem_l(memory_arena), type_id)

    # Create the leaf entity with the actual value
    leaf_entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
        "level": 0,
    }
    current_id = memory_arena.store(mem_l(memory_arena), leaf_entity)

    # Create wrapper entities, each referencing the previous one
    for level in range(1, depth):
        wrapper_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
            "child": RawStrongFxtrID(inner_id=current_id),
            "level": level,
        }
        current_id = memory_arena.store(mem_l(memory_arena), wrapper_entity)

    return current_id


@pytest.fixture
def memory_store() -> MemoryStore:
    """Create a MemoryStore for test entities."""
    return MemoryStore()


@pytest.fixture
def memory_arena(memory_store: MemoryStore):
    """Create a MemoryArena for test entities."""
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


def mem_l(memory_arena: MemoryArena) -> typing.Any:
    """Lease clone for ops on entities in the test memory arena."""
    return typing.cast(Any, memory_arena)._test_lease.clone()


def tsv_backend_lease(ns: TsvNamespaceBackend) -> typing.Any:
    """Lease clone for tsv namespace backend operations."""
    return ns._session.root_lease.clone()


# =============================================================================
# Init and Open Tests
# =============================================================================


class TestTsvNamespaceBackendInit:
    """Tests for TsvNamespaceBackend.init()."""

    async def test_init_creates_store_files(self, tmp_path: Path):
        """Init creates fxtr_namespace.toml and related files."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        await TsvNamespaceBackend.init(repo_path)

        # Check store config exists
        assert (repo_path / "fxtr_namespace.toml").exists()

        # Check .gitignore was created
        assert (repo_path / ".gitignore").exists()

        # Check .fxtr directory was created (sibling to .git)
        assert (repo_path / ".fxtr").exists()
        assert (repo_path / ".fxtr" / ".gitignore").exists()

    async def test_init_fails_if_already_exists(self, tmp_path: Path):
        """Init fails if store already exists."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        await TsvNamespaceBackend.init(repo_path)

        with pytest.raises(FxtrConfigError):
            await TsvNamespaceBackend.init(repo_path)

    async def test_init_fails_outside_vcs(self, tmp_path: Path):
        """Init fails if not in a VCS root."""
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        with pytest.raises(FxtrConfigError):
            await TsvNamespaceBackend.init(not_a_repo)


class TestTsvNamespaceBackendOpen:
    """Tests for TsvNamespaceBackend.open()."""

    async def test_open_existing_store(self, tmp_path: Path):
        """Can open an initialized store."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            assert ns is not None
            assert ns.config.namespace_root == repo_path

    async def test_open_fails_if_not_initialized(self, tmp_path: Path):
        """Open fails if store not initialized."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        with pytest.raises(FxtrConfigError):
            async with TsvNamespaceBackend.open(repo_path):
                pass

    async def test_open_does_not_create_local_config(self, tmp_path: Path):
        """Open does not create fxtr_namespace.local.toml - uses path-based worktree namespace."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        local_config = repo_path / "fxtr_namespace.local.toml"
        assert not local_config.exists()

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            # Should use path-based worktree namespace
            assert ns.config.worktree_namespace_id == f"worktree:{repo_path.resolve()}"

        # Local config should NOT be created
        assert not local_config.exists()


# =============================================================================
# Pin/Unpin/Retrieve Tests
# =============================================================================


class TestTsvNamespaceBackendPinOperations:
    """Tests for pin/unpin/retrieve operations."""

    async def test_pin_and_retrieve_simple_key(self, tmp_path: Path, memory_arena: MemoryArena):
        """Can pin and retrieve an entity with a simple key."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test_value")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            retrieved_id, arena = await ns.retrieve_pinned("mykey")
            assert retrieved_id == entity_id

            # Verify we can load the entity
            data = await arena.load(tsv_backend_lease(ns), retrieved_id)
            data = typing.cast(Any, data)
            assert data["value"] == "test_value"

    async def test_pin_and_retrieve_prefixed_key(self, tmp_path: Path, memory_arena: MemoryArena):
        """Can pin and retrieve with a prefixed key like 'data/models:encoder'."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "encoder_data")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena(
                "data/models:encoder", entity_id, memory_arena, mem_l(memory_arena)
            )

            retrieved_id, arena = await ns.retrieve_pinned("data/models:encoder")
            assert retrieved_id == entity_id

    async def test_pin_overwrite_false_raises_on_existing(
        self, tmp_path: Path, memory_arena: MemoryArena
    ):
        """Pin with overwrite=False raises if key exists."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity1, memory_arena, mem_l(memory_arena))

            with pytest.raises(KeyError):
                await ns.pin_entity_from_arena(
                    "mykey", entity2, memory_arena, mem_l(memory_arena), overwrite=False
                )

    async def test_pin_overwrite_true_replaces(self, tmp_path: Path, memory_arena: MemoryArena):
        """Pin with overwrite=True replaces existing pin."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity1, memory_arena, mem_l(memory_arena))
            await ns.pin_entity_from_arena(
                "mykey", entity2, memory_arena, mem_l(memory_arena), overwrite=True
            )

            retrieved_id, arena = await ns.retrieve_pinned("mykey")
            assert retrieved_id == entity2

    async def test_unpin_removes_pin(self, tmp_path: Path, memory_arena: MemoryArena):
        """Unpin removes the pin."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))
            await ns.unpin_entity("mykey")

            with pytest.raises(KeyError):
                await ns.retrieve_pinned("mykey")

    async def test_unpin_nonexistent_raises(self, tmp_path: Path):
        """Unpin of nonexistent key raises KeyError."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            with pytest.raises(KeyError):
                await ns.unpin_entity("nonexistent")


class TestTsvNamespaceBackendListPins:
    """Tests for list_pins operation."""

    async def test_list_pins_empty(self, tmp_path: Path):
        """list_pins returns empty dict when no pins."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            pins = await ns.list_pins()
            assert pins == {}

    async def test_list_pins_returns_all(self, tmp_path: Path, memory_arena: MemoryArena):
        """list_pins returns all pinned entities."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("key1", entity1, memory_arena, mem_l(memory_arena))
            await ns.pin_entity_from_arena("data:key2", entity2, memory_arena, mem_l(memory_arena))

            pins = await ns.list_pins()
            assert len(pins) == 2
            assert pins["key1"] == entity1
            assert pins["data:key2"] == entity2

    async def test_list_pins_with_prefix(self, tmp_path: Path, memory_arena: MemoryArena):
        """list_pins with prefix filters results."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("key1", entity1, memory_arena, mem_l(memory_arena))
            await ns.pin_entity_from_arena("data:key2", entity2, memory_arena, mem_l(memory_arena))

            pins = await ns.list_pins("data:")
            assert len(pins) == 1
            assert pins["data:key2"] == entity2


# =============================================================================
# Flush Tests
# =============================================================================


class TestTsvNamespaceBackendFlush:
    """Tests for flush operations."""

    async def test_close_flushes_to_tsv(self, tmp_path: Path, memory_arena: MemoryArena):
        """Closing namespace flushes dirty TSVs to disk."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))
            # TSV should be dirty (not flushed yet)

        # After close, TSV should exist with the pin
        tsv_path = repo_path / "root.fxtr.tsv"
        assert tsv_path.exists()
        tsv_content = read_tsv(tsv_path)
        assert "mykey" in tsv_content
        assert tsv_content["mykey"] == entity_id

    async def test_explicit_flush(self, tmp_path: Path, memory_arena: MemoryArena):
        """Explicit flush() writes TSV before close."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))
            await ns.flush()

            # TSV should exist now (before close)
            tsv_path = repo_path / "root.fxtr.tsv"
            assert tsv_path.exists()
            tsv_content = read_tsv(tsv_path)
            assert tsv_content["mykey"] == entity_id

    async def test_full_sync_state_to_git(self, tmp_path: Path, memory_arena: MemoryArena):
        """full_sync_state_to_git force-flushes all dirty TSVs."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("key1", entity1, memory_arena, mem_l(memory_arena))
            await ns.pin_entity_from_arena("data:key2", entity2, memory_arena, mem_l(memory_arena))
            await ns.full_sync_state_to_git()

            # Both TSVs should be flushed
            assert (repo_path / "root.fxtr.tsv").exists()
            assert (repo_path / "data.fxtr.tsv").exists()


# =============================================================================
# TSV Modification Tests
# =============================================================================


class TestTsvNamespaceBackendTsvSync:
    """Tests for TSV file synchronization."""

    async def test_external_tsv_change_reflected(self, tmp_path: Path, memory_arena: MemoryArena):
        """External TSV changes are reflected when reading."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")

        # First, pin entity1 and flush
        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity1, memory_arena, mem_l(memory_arena))
            # Also pin to index namespace so entity2 can be synced later
            await ns.pin_entity_from_arena("entity2", entity2, memory_arena, mem_l(memory_arena))
            await ns.flush()

        # Externally modify TSV to point to entity2
        tsv_path = repo_path / "root.fxtr.tsv"
        write_tsv(tsv_path, {"mykey": entity2})

        # Reopen and verify the change is reflected
        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            retrieved_id, _ = await ns.retrieve_pinned("mykey")
            assert retrieved_id == entity2

    async def test_external_tsv_deletion_removes_pins(
        self, tmp_path: Path, memory_arena: MemoryArena
    ):
        """Deleted TSV file results in pins being removed."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        # Pin and flush
        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))
            await ns.flush()

        # Delete the TSV
        tsv_path = repo_path / "root.fxtr.tsv"
        tsv_path.unlink()

        # Reopen and verify pin is gone
        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            with pytest.raises(KeyError):
                await ns.retrieve_pinned("mykey")


# =============================================================================
# Concurrent Access Tests
# =============================================================================


class TestTsvNamespaceBackendConcurrentAccess:
    """Tests for concurrent access patterns."""

    async def test_two_namespaces_see_each_others_writes(
        self, tmp_path: Path, memory_arena: MemoryArena
    ):
        """Two namespaces on same store see each other's writes immediately."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "shared")

        async with TsvNamespaceBackend.open(
            repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
        ) as ns1:
            async with TsvNamespaceBackend.open(
                repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
            ) as ns2:
                # ns1 pins an entity
                await ns1.pin_entity_from_arena(
                    "shared_key", entity_id, memory_arena, mem_l(memory_arena)
                )

                # ns2 should see it (both are dirty, sqlite is authoritative)
                retrieved_id, _ = await ns2.retrieve_pinned("shared_key")
                assert retrieved_id == entity_id

    async def test_force_flush_removes_other_marks(self, tmp_path: Path, memory_arena: MemoryArena):
        """Force flush removes marks from other managers."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")

        async with TsvNamespaceBackend.open(
            repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
        ) as ns1:
            async with TsvNamespaceBackend.open(
                repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
            ) as ns2:
                # ns1 pins an entity
                await ns1.pin_entity_from_arena("key1", entity1, memory_arena, mem_l(memory_arena))

                # ns2 pins another entity
                await ns2.pin_entity_from_arena("key2", entity2, memory_arena, mem_l(memory_arena))

                # ns1 force-flushes
                await ns1.full_sync_state_to_git()

                # TSV should have both keys (both were in sqlite)
                tsv_path = repo_path / "root.fxtr.tsv"
                tsv_content = read_tsv(tsv_path)
                assert "key1" in tsv_content
                assert "key2" in tsv_content

    async def test_manual_tsv_edit_while_clean_is_synced_no_backup(
        self, tmp_path: Path, memory_arena: MemoryArena, caplog: pytest.LogCaptureFixture
    ):
        """When TSV is edited while clean, edit is synced and no backup needed.

        Scenario:
        1. A writes, B writes to same key prefix
        2. A force-flushes (removes all dirty marks, TSV is now clean/source of truth)
        3. TSV is edited manually while clean (valid edit to source of truth)
        4. B writes again (must re-claim, sync_if_first syncs the edit to SQLite)
        5. B flushes
        6. Verify: NO backup created, manual edit is preserved in final TSV
        """
        import logging

        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")
        entity2 = create_test_entity(memory_arena, "second")
        entity3 = create_test_entity(memory_arena, "third")

        async with TsvNamespaceBackend.open(
            repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
        ) as ns_a:
            async with TsvNamespaceBackend.open(
                repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
            ) as ns_b:
                # Step 1: A writes key1, B writes key2
                await ns_a.pin_entity_from_arena("key1", entity1, memory_arena, mem_l(memory_arena))
                await ns_b.pin_entity_from_arena("key2", entity2, memory_arena, mem_l(memory_arena))

                tsv_path = repo_path / "root.fxtr.tsv"

                # Step 2: A force-flushes (removes all dirty marks, TSV becomes source of truth)
                await ns_a.full_sync_state_to_git()

                # TSV should exist with key1 and key2, and be clean (no sentinel)
                assert tsv_path.exists()
                dirty_dir = tsv_path.parent / f"{tsv_path.name}.dirty"
                assert not dirty_dir.exists(), "TSV should be clean after force-flush"

                original_content = read_tsv(tsv_path)
                assert "key1" in original_content
                assert "key2" in original_content

                # Step 3: Manually edit TSV while clean (valid edit to source of truth)
                # Add a "manual_key" entry - must also add entity to index namespace
                manual_id = create_test_entity(memory_arena, "manual")
                await ns_a._index_ns.pin_entity_from_arena(
                    manual_id.id_string(),
                    manual_id,
                    memory_arena,
                    mem_l(memory_arena),
                    overwrite=True,
                )
                edited_content = dict(original_content)
                edited_content["manual_key"] = manual_id
                write_tsv(tsv_path, edited_content)

                # Step 4: B writes again (must re-claim, sync_if_first syncs the edit)
                with caplog.at_level(logging.WARNING, logger="fxtr2.data.tsv_namespace.dirty"):
                    await ns_b.pin_entity_from_arena(
                        "key3", entity3, memory_arena, mem_l(memory_arena)
                    )

                    # Step 5: B flushes
                    await ns_b.flush()

                # Step 6: Verify NO backup was created (edit was synced, not a conflict)
                backup_files = list(repo_path.glob("*.fxtr.tsv.backup-*.tsv"))
                assert len(backup_files) == 0, (
                    "Expected no backup (edit while clean should be synced), "
                    f"but found: {backup_files}"
                )

                # Verify no warning was logged
                backup_warnings = [
                    r
                    for r in caplog.records
                    if "CONCURRENT EDIT" in r.message or "no cached state" in r.message
                ]
                assert (
                    len(backup_warnings) == 0
                ), f"Expected no backup warnings, but found: {[r.message for r in backup_warnings]}"

                # Verify the final TSV has ALL content (original + manual edit + B's write)
                final_content = read_tsv(tsv_path)
                assert "key1" in final_content, "key1 should be preserved"
                assert "key2" in final_content, "key2 should be preserved"
                assert "key3" in final_content, "key3 (B's write) should be present"
                assert "manual_key" in final_content, "manual_key (the edit) should be preserved"

    async def test_manual_tsv_edit_while_dirty_creates_backup(
        self, tmp_path: Path, memory_arena: MemoryArena, caplog: pytest.LogCaptureFixture
    ):
        """When TSV is edited while dirty, backup is created on flush (conflict).

        Scenario:
        1. A writes (TSV becomes dirty)
        2. TSV is edited manually while dirty (conflict! SQLite is source of truth)
        3. A flushes
        4. Verify: Backup IS created with the conflicting edit
        """
        import logging

        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity1 = create_test_entity(memory_arena, "first")

        async with TsvNamespaceBackend.open(
            repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
        ) as ns_a:
            # Step 1: A writes key1 (TSV becomes dirty)
            await ns_a.pin_entity_from_arena("key1", entity1, memory_arena, mem_l(memory_arena))

            tsv_path = repo_path / "root.fxtr.tsv"
            dirty_dir = tsv_path.parent / f"{tsv_path.name}.dirty"

            # Verify TSV is dirty
            assert dirty_dir.exists(), "TSV should be dirty after write"

            # Flush to create the TSV (so we can edit it)
            await ns_a.flush()
            assert tsv_path.exists()
            assert not dirty_dir.exists(), "Should be clean after flush"

            original_content = read_tsv(tsv_path)
            assert "key1" in original_content

            # Now write again to make it dirty
            entity2 = create_test_entity(memory_arena, "second")
            await ns_a.pin_entity_from_arena("key2", entity2, memory_arena, mem_l(memory_arena))
            assert dirty_dir.exists(), "TSV should be dirty after second write"

            # Step 2: Manually edit TSV while dirty (CONFLICT!)
            manual_id = create_test_entity(memory_arena, "manual")
            await ns_a._index_ns.pin_entity_from_arena(
                manual_id.id_string(),
                manual_id,
                memory_arena,
                mem_l(memory_arena),
                overwrite=True,
            )
            edited_content = dict(original_content)
            edited_content["conflict_key"] = manual_id
            write_tsv(tsv_path, edited_content)

            # Step 3: A flushes
            with caplog.at_level(logging.WARNING, logger="fxtr2.data.tsv_namespace.dirty"):
                await ns_a.flush()

            # Step 4: Verify backup WAS created (this was a conflict)
            backup_files = list(repo_path.glob("*.fxtr.tsv.backup-*.tsv"))
            assert (
                len(backup_files) == 1
            ), f"Expected 1 backup file (conflict edit), found: {backup_files}"

            # Verify warning was logged
            assert any("CONCURRENT EDIT DETECTED" in record.message for record in caplog.records), (
                "Expected 'CONCURRENT EDIT DETECTED' warning, got:"
                f" {[r.message for r in caplog.records]}"
            )

            # Verify the backup contains the conflicting edit
            backup_content = read_tsv(backup_files[0])
            assert "conflict_key" in backup_content

            # Verify the final TSV has A's SQLite state (key1 + key2, but NOT conflict_key)
            final_content = read_tsv(tsv_path)
            assert "key1" in final_content
            assert "key2" in final_content
            assert (
                "conflict_key" not in final_content
            ), "Conflict key should be in backup, not final TSV"


# =============================================================================
# Export/Import Tests
# =============================================================================


class TestTsvNamespaceBackendExportImport:
    """Tests for export/import functionality."""

    async def test_export_creates_archive_notar(self, tmp_path: Path, memory_arena: MemoryArena):
        """export_archive(tar=False) creates sqlite + mdb files."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            archive_path = tmp_path / "archive.sqlite"
            await ns.export_archive(archive_path, tar=False)

            assert archive_path.exists()
            # Segment files directory should exist
            shards_path = tmp_path / "archive.sqlite.data2.shards"
            assert shards_path.exists()
            assert shards_path.is_dir()

    async def test_export_creates_tar(self, tmp_path: Path, memory_arena: MemoryArena):
        """export_archive(tar=True) creates a tarball with sqlite + segment files."""
        import tarfile

        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            archive_path = tmp_path / "archive.tar"
            await ns.export_archive(archive_path, tar=True)

            assert archive_path.exists()
            with tarfile.open(archive_path, "r:") as tf:
                names = tf.getnames()
                assert "store.sqlite" in names
                assert any(n.startswith("store.sqlite.data2.shards/") for n in names)

    async def test_export_creates_tar_gz(self, tmp_path: Path, memory_arena: MemoryArena):
        """export_archive(tar=True, gz=True) creates a gzipped tarball."""
        import tarfile

        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            archive_path = tmp_path / "archive.tar.gz"
            await ns.export_archive(archive_path, tar=True, gz=True)

            assert archive_path.exists()
            with tarfile.open(archive_path, "r:gz") as tf:
                names = tf.getnames()
                assert "store.sqlite" in names
                assert any(n.startswith("store.sqlite.data2.shards/") for n in names)

    async def test_export_import_roundtrip_notar(self, tmp_path: Path, memory_arena: MemoryArena):
        """Exported raw sqlite archive can be imported into another store."""
        # Create first store and export
        repo1_path = tmp_path / "repo1"
        await create_git_repo(repo1_path)
        await TsvNamespaceBackend.init(repo1_path)

        entity_id = create_test_entity(memory_arena, "shared_data")

        async with TsvNamespaceBackend.open(repo1_path, auto_flush_seconds=None) as ns1:
            await ns1.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            archive_path = tmp_path / "archive.sqlite"
            await ns1.export_archive(archive_path, tar=False)

        # Create second store and import
        repo2_path = tmp_path / "repo2"
        await create_git_repo(repo2_path)
        await TsvNamespaceBackend.init(repo2_path)

        async with TsvNamespaceBackend.open(repo2_path, auto_flush_seconds=None) as ns2:
            await ns2.import_archive_into_backing_store(archive_path)

            # Write TSV with the imported entity
            tsv_path = repo2_path / "root.fxtr.tsv"
            write_tsv(tsv_path, {"mykey": entity_id})

        # Verify entity can be retrieved
        async with TsvNamespaceBackend.open(repo2_path, auto_flush_seconds=None) as ns2:
            retrieved_id, arena = await ns2.retrieve_pinned("mykey")
            assert retrieved_id == entity_id

            # Verify entity data
            data = await arena.load(tsv_backend_lease(ns2), retrieved_id)
            data = typing.cast(Any, data)
            assert data["value"] == "shared_data"

    async def test_export_import_roundtrip_tar(self, tmp_path: Path, memory_arena: MemoryArena):
        """Exported tarball can be imported into another store."""
        # Create first store and export
        repo1_path = tmp_path / "repo1"
        await create_git_repo(repo1_path)
        await TsvNamespaceBackend.init(repo1_path)

        entity_id = create_test_entity(memory_arena, "shared_data")

        async with TsvNamespaceBackend.open(repo1_path, auto_flush_seconds=None) as ns1:
            await ns1.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            archive_path = tmp_path / "archive.tar"
            await ns1.export_archive(archive_path, tar=True)

        # Create second store and import
        repo2_path = tmp_path / "repo2"
        await create_git_repo(repo2_path)
        await TsvNamespaceBackend.init(repo2_path)

        async with TsvNamespaceBackend.open(repo2_path, auto_flush_seconds=None) as ns2:
            await ns2.import_archive_into_backing_store(archive_path)

            # Write TSV with the imported entity
            tsv_path = repo2_path / "root.fxtr.tsv"
            write_tsv(tsv_path, {"mykey": entity_id})

        # Verify entity can be retrieved
        async with TsvNamespaceBackend.open(repo2_path, auto_flush_seconds=None) as ns2:
            retrieved_id, arena = await ns2.retrieve_pinned("mykey")
            assert retrieved_id == entity_id

            data = await arena.load(tsv_backend_lease(ns2), retrieved_id)
            data = typing.cast(Any, data)
            assert data["value"] == "shared_data"

    async def test_export_import_roundtrip_tar_gz(self, tmp_path: Path, memory_arena: MemoryArena):
        """Exported .tar.gz can be imported into another store."""
        # Create first store and export
        repo1_path = tmp_path / "repo1"
        await create_git_repo(repo1_path)
        await TsvNamespaceBackend.init(repo1_path)

        entity_id = create_test_entity(memory_arena, "shared_data")

        async with TsvNamespaceBackend.open(repo1_path, auto_flush_seconds=None) as ns1:
            await ns1.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            archive_path = tmp_path / "archive.tar.gz"
            await ns1.export_archive(archive_path, tar=True, gz=True)

        # Create second store and import
        repo2_path = tmp_path / "repo2"
        await create_git_repo(repo2_path)
        await TsvNamespaceBackend.init(repo2_path)

        async with TsvNamespaceBackend.open(repo2_path, auto_flush_seconds=None) as ns2:
            await ns2.import_archive_into_backing_store(archive_path)

            tsv_path = repo2_path / "root.fxtr.tsv"
            write_tsv(tsv_path, {"mykey": entity_id})

        async with TsvNamespaceBackend.open(repo2_path, auto_flush_seconds=None) as ns2:
            retrieved_id, arena = await ns2.retrieve_pinned("mykey")
            assert retrieved_id == entity_id

            data = await arena.load(tsv_backend_lease(ns2), retrieved_id)
            data = typing.cast(Any, data)
            assert data["value"] == "shared_data"

    async def test_import_skips_existing_matching(self, tmp_path: Path, memory_arena: MemoryArena):
        """Import skips entities that already exist with matching ID."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            # Export
            archive_path = tmp_path / "archive.sqlite"
            await ns.export_archive(archive_path, tar=False)

            # Import into same store (should be no-op for existing entity)
            await ns.import_archive_into_backing_store(archive_path)  # Should not raise

            # Verify entity still accessible
            retrieved_id, _ = await ns.retrieve_pinned("mykey")
            assert retrieved_id == entity_id

    async def test_import_rejects_unknown_extension(
        self, tmp_path: Path, memory_arena: MemoryArena
    ):
        """Import raises ValueError for unrecognized file extensions."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        bad_file = tmp_path / "archive.zip"
        bad_file.write_bytes(b"not a real archive")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            with pytest.raises(ValueError, match="Unrecognized archive format"):
                await ns.import_archive_into_backing_store(bad_file)


# =============================================================================
# Auto-flush Tests
# =============================================================================


class TestTsvNamespaceBackendAutoFlush:
    """Tests for auto-flush behavior."""

    async def test_auto_flush_runs_periodically(self, tmp_path: Path, memory_arena: MemoryArena):
        """Auto-flush runs and flushes dirty TSVs."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=0.01) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            # Wait for auto-flush to run
            await anyio.sleep(0.03)

            # TSV should be flushed
            tsv_path = repo_path / "root.fxtr.tsv"
            assert tsv_path.exists()

    async def test_auto_flush_disabled_when_none(self, tmp_path: Path, memory_arena: MemoryArena):
        """Auto-flush doesn't run when auto_flush_seconds=None."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            # Wait a bit - auto-flush should NOT run
            await anyio.sleep(0.01)

            # TSV should NOT exist yet (not flushed)
            tsv_path = repo_path / "root.fxtr.tsv"
            assert not tsv_path.exists()

        # After close, it should exist
        assert tsv_path.exists()


# =============================================================================
# DeepFetchable tests
# =============================================================================


class TestTsvNamespaceBackendDeepFetchable:
    """Tests for DeepFetchable.try_lookup (leased deep fetch)."""

    async def test_try_lookup_finds_pinned_entity(self, tmp_path: Path, memory_arena: MemoryArena):
        """try_lookup returns arena for pinned entities."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "test")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("mykey", entity_id, memory_arena, mem_l(memory_arena))

            arena = await ns.try_lookup(entity_id)
            assert arena is not None

    async def test_try_lookup_returns_none_for_unknown(
        self, tmp_path: Path, memory_arena: MemoryArena
    ):
        """try_lookup returns None for unknown entities."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        entity_id = create_test_entity(memory_arena, "not_pinned")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            arena = await ns.try_lookup(entity_id)
            assert arena is None


# =============================================================================
# Race Condition Stress Tests
# =============================================================================


class TestTsvNamespaceBackendRaceConditions:
    """Stress tests for race conditions between pin and flush operations."""

    async def test_concurrent_pin_and_flush_same_tsv(
        self, tmp_path: Path, memory_store: MemoryStore, caplog
    ):
        """Stress test: race pinning entity2 to a different key while flushing.

        Scenario per iteration:
        1. Pin entity1 to foo:key1 (foo.fxtr.tsv becomes dirty)
        2. Simultaneously: flush() and pin entity2 to foo:key2 (different key, same TSV)
        3. Verify both foo:key1 and foo:key2 are visible in list_pins

        Varies nesting depth across iterations to find timing sweet spots
        that might expose race conditions.
        """
        import logging

        caplog.set_level(logging.INFO)
        caplog.set_level(logging.DEBUG, logger="fxtr2.data.tsv_namespace.dirty")
        caplog.set_level(logging.DEBUG, logger="fxtr2.data.tsv_namespace.sync")
        caplog.set_level(logging.DEBUG, logger="fxtr2.data.tsv_namespace.tsv_namespace")

        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        # Try various nesting depths to find timing sweet spots
        depths_to_try = list(range(10))
        iterations_per_depth = 1

        async with TsvNamespaceBackend.open(
            repo_path, auto_flush_seconds=None, full_sync_staleness_threshold=0
        ) as ns:
            for depth in depths_to_try:
                for iteration in range(iterations_per_depth):
                    logging.info(f"Starting depth={depth}, iteration={iteration}")
                    # Create fresh arena for each iteration
                    _ull = UnconditionalLocalLifetime("test")
                    memory_store.register_token(_ull.token, name="test")
                    root_lease = _ull.root_lease.take()
                    arena = memory_store.new_arena(root_lease.clone())
                    typing.cast(Any, arena)._test_lease = root_lease
                    try:
                        # Create entities with current depth
                        entity1 = create_deeply_nested_entity(
                            arena, f"v1_d{depth}_i{iteration}", depth=depth
                        )
                        entity2 = create_deeply_nested_entity(
                            arena, f"v2_d{depth}_i{iteration}", depth=depth
                        )

                        # Step 1: Pin entity1 to foo:key1
                        await ns.pin_entity_from_arena("foo:key1", entity1, arena, mem_l(arena))

                        # Step 2: Race flush() against pin(entity2 to foo:key2)
                        async def do_flush():
                            await ns.flush()

                        async def do_pin_key2():
                            await ns.pin_entity_from_arena("foo:key2", entity2, arena, mem_l(arena))

                        async with anyio.create_task_group() as tg:
                            tg.start_soon(do_pin_key2)
                            await anyio.sleep(0.001)
                            tg.start_soon(do_flush)

                        # Step 3: Verify both keys are present
                        pins = await ns.list_pins("foo:")

                        if "foo:key1" not in pins:
                            raise AssertionError(
                                f"depth={depth}, iter={iteration}: foo:key1 disappeared! "
                                f"Pins: {list(pins.keys())}"
                            )
                        if "foo:key2" not in pins:
                            raise AssertionError(
                                f"depth={depth}, iter={iteration}: foo:key2 missing! "
                                f"Pins: {list(pins.keys())}"
                            )

                        # Verify correct entity IDs
                        assert (
                            pins["foo:key1"] == entity1
                        ), f"depth={depth}, iter={iteration}: foo:key1 has wrong entity"
                        assert (
                            pins["foo:key2"] == entity2
                        ), f"depth={depth}, iter={iteration}: foo:key2 has wrong entity"

                        # Step 4: Cleanup for next iteration
                        await ns.unpin_entity("foo:key1")
                        await ns.unpin_entity("foo:key2")
                        await ns.flush()
                    finally:
                        arena.close()
                        root_lease.discard()
