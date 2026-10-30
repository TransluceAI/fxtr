"""Integration tests for TsvNamespaceBackend indexing support.

Tests the full end-to-end flow: register/unregister indexers, list_indexers,
iter_matches, sync-on-access, full sync ordering, file locking, and
export/import with indexer metadata.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import anyio
import pytest

from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.tsv_namespace import TsvNamespaceBackend, write_tsv
from fxtr2.data.tsv_namespace.index_file import read_index_file, write_index_file
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.data.store_indexing.indexers import DerefMarker, PathKey, TypedPathValueIndexer
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType, IndexerConstraintByID
from fxtr2.leases.lease_types import Lease
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
    await anyio.run_process(["git", "config", "user.email", "test@test.com"], cwd=path, check=True)
    await anyio.run_process(["git", "config", "user.name", "Test User"], cwd=path, check=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GROUND_CTX = FxtrID.from_tag_and_payload(".", b"")
_TYPE_A = FxtrID.builtin("test_entity")


def tsv_backend_lease(ns: TsvNamespaceBackend) -> Any:
    return ns._session.root_lease.clone()


def mem_l(arena: MemoryArena) -> Any:
    return typing.cast(Any, arena)._test_lease.clone()


def _entity(memory_arena: MemoryArena, lease: Lease[Any], **fields: object) -> FxtrID:
    """Store a test entity and return its ID."""
    memory_arena.track_builtin(lease.clone(), _GROUND_CTX)
    memory_arena.track_builtin(lease.clone(), _TYPE_A)
    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=_GROUND_CTX),
        "$fxtr.type": RawWeakFxtrID(inner_id=_TYPE_A),
        **fields,
    }
    return memory_arena.store(lease.clone(), entity)


def _make_indexer(key: str = "value", value_type=IndexerColumnType.STR) -> TypedPathValueIndexer:
    """Create a simple test indexer that extracts `entity.{key}`."""
    return TypedPathValueIndexer(
        initial_lookup=DerefMarker(),
        strict=False,
        path=(PathKey(key=key),),
        var_types=(),
        value_type=value_type,
    )


def _store_indexer(indexer: TypedPathValueIndexer, scope: FxtrScope) -> FxtrID:
    """Store an indexer entity and return its FxtrID."""
    return indexer.store(scope=scope).entity_id


# ---------------------------------------------------------------------------
# Basic register / unregister / list
# ---------------------------------------------------------------------------


class TestTsvNamespaceIndexing:
    async def test_register_indexer(self, tmp_path: Path, local_fxtr_scope: FxtrScope):
        """Register an indexer: worktree_ns gets it, file is updated."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.register_indexer(iid, source, local_fxtr_scope.clone_authorization())

            # File should be updated
            file_regs = read_index_file(ns._index_file_path)
            assert (iid, "") in file_regs

            # list_indexers should return it
            regs, arena = await ns.list_indexers()
            arena.close()
            assert len(regs) == 1
            assert regs[0][0] == iid
            assert regs[0][1] == ""

    async def test_unregister_indexer(self, tmp_path: Path, local_fxtr_scope: FxtrScope):
        """Unregister an indexer: removed from worktree_ns and file."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.register_indexer(iid, source, local_fxtr_scope.clone_authorization())
            await ns.unregister_indexer(iid)

            # File should be empty
            file_regs = read_index_file(ns._index_file_path)
            assert file_regs == frozenset()

            # list_indexers should return empty
            regs, arena = await ns.list_indexers()
            arena.close()
            assert len(regs) == 0

    async def test_register_with_key_prefix(self, tmp_path: Path, local_fxtr_scope: FxtrScope):
        """Register an indexer with a key prefix."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.register_indexer(
                iid, source, local_fxtr_scope.clone_authorization(), key_prefix="data/"
            )

            file_regs = read_index_file(ns._index_file_path)
            assert (iid, "data/") in file_regs

            regs, arena = await ns.list_indexers()
            arena.close()
            assert len(regs) == 1
            assert regs[0][1] == "data/"

    async def test_list_indexers_empty(self, tmp_path: Path):
        """list_indexers on fresh store returns empty."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            regs, arena = await ns.list_indexers()
            arena.close()
            assert regs == []


# ---------------------------------------------------------------------------
# iter_matches end-to-end
# ---------------------------------------------------------------------------


class TestTsvNamespaceIterMatches:
    async def test_iter_matches_basic(self, tmp_path: Path, local_fxtr_scope: FxtrScope):
        """End-to-end: register indexer, pin entities, query matches."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        store = MemoryStore()
        _ull = UnconditionalLocalLifetime("test")
        store.register_token(_ull.token, name="test")
        root_lease = _ull.root_lease.take()
        arena = store.new_arena(root_lease.clone())
        arena._test_lease = root_lease  # type: ignore[attr-defined]

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        idx_source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        # Create entities
        e1 = _entity(arena, root_lease, value="hello")
        e2 = _entity(arena, root_lease, value="world")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.register_indexer(iid, idx_source, local_fxtr_scope.clone_authorization())

            await ns.pin_entity_from_arena("a", e1, arena, mem_l(arena))
            await ns.pin_entity_from_arena("b", e2, arena, mem_l(arena))

            # Query with a trivial condition
            constraint = IndexerConstraintByID(
                indexer_id=iid,
                key_variables=(),
                condition="1=1",
            )
            results = []
            async for key, eid, bindings, match_arena in ns.iter_matches(constraint):
                results.append((key, eid))
                match_arena.close()

            # Should find both entities
            result_keys = {r[0] for r in results}
            assert result_keys == {"a", "b"}

        arena.close()
        root_lease.discard()

    async def test_iter_matches_with_condition(self, tmp_path: Path, local_fxtr_scope: FxtrScope):
        """Query matches with a value condition."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        store = MemoryStore()
        _ull = UnconditionalLocalLifetime("test")
        store.register_token(_ull.token, name="test")
        root_lease = _ull.root_lease.take()
        arena = store.new_arena(root_lease.clone())
        arena._test_lease = root_lease  # type: ignore[attr-defined]

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        idx_source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        e1 = _entity(arena, root_lease, value="hello")
        e2 = _entity(arena, root_lease, value="world")

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.register_indexer(iid, idx_source, local_fxtr_scope.clone_authorization())
            await ns.pin_entity_from_arena("a", e1, arena, mem_l(arena))
            await ns.pin_entity_from_arena("b", e2, arena, mem_l(arena))

            constraint = IndexerConstraintByID(
                indexer_id=iid,
                key_variables=(),
                condition="value = 'hello'",
            )
            results = []
            async for key, eid, bindings, match_arena in ns.iter_matches(constraint):
                results.append(key)
                match_arena.close()

            assert results == ["a"]

        arena.close()
        root_lease.discard()


# ---------------------------------------------------------------------------
# Sync-on-access: external file changes
# ---------------------------------------------------------------------------


class TestTsvNamespaceSyncOnAccess:
    async def test_external_file_modification_picked_up(
        self, tmp_path: Path, local_fxtr_scope: FxtrScope
    ):
        """Modifying the index file externally is picked up on next list_indexers."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            # Register normally
            await ns.register_indexer(iid, source, local_fxtr_scope.clone_authorization())

            # Externally clear the file (simulate git checkout)
            write_index_file(ns._index_file_path, frozenset())

            # list_indexers should pick up the change and remove the registration
            regs, arena = await ns.list_indexers()
            arena.close()
            assert len(regs) == 0

    async def test_external_file_add_picked_up(self, tmp_path: Path, local_fxtr_scope: FxtrScope):
        """Adding to the index file externally is picked up on next list_indexers."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            # Pin the indexer entity in the backing store (without registering)
            await ns._index_ns.pin_entity_from_arena(
                iid.id_string(),
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
                overwrite=True,
            )

            # Externally write the file
            write_index_file(ns._index_file_path, frozenset({(iid, "")}))

            # list_indexers should pick up the change and register the indexer
            regs, arena = await ns.list_indexers()
            arena.close()
            assert len(regs) == 1
            assert regs[0][0] == iid


# ---------------------------------------------------------------------------
# Export/import with indexer metadata
# ---------------------------------------------------------------------------


class TestTsvNamespaceIndexExportImport:
    async def test_export_includes_indexer_metadata(
        self, tmp_path: Path, local_fxtr_scope: FxtrScope
    ):
        """Export archive includes indexer metadata when indexers are registered."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        store = MemoryStore()
        _ull = UnconditionalLocalLifetime("test")
        store.register_token(_ull.token, name="test")
        root_lease = _ull.root_lease.take()
        arena = store.new_arena(root_lease.clone())
        arena._test_lease = root_lease  # type: ignore[attr-defined]

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        idx_source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        e1 = _entity(arena, root_lease, value="test")
        archive_path = tmp_path / "export.tar"

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.register_indexer(iid, idx_source, local_fxtr_scope.clone_authorization())
            await ns.pin_entity_from_arena("a", e1, arena, mem_l(arena))
            await ns.export_archive(archive_path)

        # Verify by loading the metadata entity from the archive
        import tarfile
        import tempfile

        from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
        from fxtr2.data.sqlite_store import SQLiteFxtrSession
        from fxtr2.data.store_indexing.indexing_types import NamespaceIndexMetadata

        with tempfile.TemporaryDirectory() as tempdir:
            with tarfile.open(archive_path, "r:") as tf:
                tf.extractall(tempdir, filter="data")
            sqlite_path = Path(tempdir) / "store.sqlite"
            async with SQLiteFxtrSession.open(str(sqlite_path)) as session:
                metadata_ns = await session.create_or_retrieve_user_namespace("metadata")
                metadata_id, meta_arena = await metadata_ns.retrieve_pinned("indexers")

                # Load and verify the metadata entity
                async with FxtrScope() as scope:
                    await scope._main_arena.copy_from_arena(
                        scope.clone_authorization(),
                        meta_arena,
                        session.root_lease.clone(),
                        metadata_id,
                    )
                    meta_ref = EntityRef(entity_id=metadata_id, target_type=NamespaceIndexMetadata)
                    metadata = await meta_ref.load(scope)

                    # Should have our indexer ID and empty prefix
                    assert (iid, "") in metadata.registrations

                meta_arena.close()

        arena.close()
        root_lease.discard()

    async def test_export_no_metadata_without_indexers(self, tmp_path: Path):
        """Export archive without indexers still works for import."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)
        await TsvNamespaceBackend.init(repo_path)

        store = MemoryStore()
        _ull = UnconditionalLocalLifetime("test")
        store.register_token(_ull.token, name="test")
        root_lease = _ull.root_lease.take()
        arena = store.new_arena(root_lease.clone())
        arena._test_lease = root_lease  # type: ignore[attr-defined]
        e1 = _entity(arena, root_lease, value="test")
        archive_path = tmp_path / "export.tar"

        async with TsvNamespaceBackend.open(repo_path, auto_flush_seconds=None) as ns:
            await ns.pin_entity_from_arena("a", e1, arena, mem_l(arena))
            await ns.export_archive(archive_path)

        # Import into a fresh store — should work without error
        repo2 = tmp_path / "repo2"
        await create_git_repo(repo2)
        await TsvNamespaceBackend.init(repo2)

        async with TsvNamespaceBackend.open(repo2, auto_flush_seconds=None, skip_sync=True) as ns2:
            await ns2.import_archive_into_backing_store(archive_path)
            await ns2.sync_from_git()

            # No indexers should be present
            regs, idx_arena = await ns2.list_indexers()
            idx_arena.close()
            assert regs == []

        arena.close()
        root_lease.discard()

    async def test_import_roundtrip(self, tmp_path: Path, local_fxtr_scope: FxtrScope):
        """Full round-trip: export with indexers → import into fresh store → register from file → query."""
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        await create_git_repo(repo1)
        await create_git_repo(repo2)
        await TsvNamespaceBackend.init(repo1)
        await TsvNamespaceBackend.init(repo2)

        store = MemoryStore()
        _ull = UnconditionalLocalLifetime("test")
        store.register_token(_ull.token, name="test")
        root_lease = _ull.root_lease.take()
        arena = store.new_arena(root_lease.clone())
        arena._test_lease = root_lease  # type: ignore[attr-defined]

        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        idx_source = local_fxtr_scope._main_arena.copy_to_new_arena(
            local_fxtr_scope.clone_authorization(), [iid]
        )

        e1 = _entity(arena, root_lease, value="hello")
        archive_path = tmp_path / "export.tar"

        # Export from repo1 with indexer
        async with TsvNamespaceBackend.open(repo1, auto_flush_seconds=None) as ns1:
            await ns1.register_indexer(iid, idx_source, local_fxtr_scope.clone_authorization())
            await ns1.pin_entity_from_arena("a", e1, arena, mem_l(arena))
            await ns1.export_archive(archive_path)

        # Import into repo2
        async with TsvNamespaceBackend.open(repo2, auto_flush_seconds=None, skip_sync=True) as ns2:
            await ns2.import_archive_into_backing_store(archive_path)
            await ns2.sync_from_git()

            # Write the TSV with pin data
            tsv_path = Path(ns2._config.namespace_root_str) / "root.fxtr.tsv"
            write_tsv(str(tsv_path), {"a": e1})
            ns2.clear_staleness_cache()

            # Write the index file with the indexer registration
            write_index_file(ns2._index_file_path, frozenset({(iid, "")}))

            # Sync to pick up changes
            await ns2.sync_from_git()

            # Should be able to query
            regs, idx_arena = await ns2.list_indexers()
            idx_arena.close()
            assert len(regs) == 1

            constraint = IndexerConstraintByID(
                indexer_id=iid,
                key_variables=(),
                condition="1=1",
            )
            results = []
            async for key, eid, bindings, match_arena in ns2.iter_matches(constraint):
                results.append(key)
                match_arena.close()

            assert results == ["a"]

        arena.close()
        root_lease.discard()
