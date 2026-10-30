"""Tests for index registration sync between the git-tracked index file and worktree namespace."""

from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.tsv_namespace.index_file import read_index_file, write_index_file
from fxtr2.data.tsv_namespace.index_sync import (
    add_registration_to_file,
    get_live_registrations,
    remove_dead_index_registrations,
    remove_registration_from_file,
    sync_index_registrations,
)
from fxtr2.data.tsv_namespace.sync import FxtrSyncError
from fxtr2.data.sqlite_store import SQLiteFxtrSession
from fxtr2.data.store_indexing.indexers import DerefMarker, PathKey, TypedPathValueIndexer
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType


def _make_indexer(**overrides) -> TypedPathValueIndexer:
    """Create a simple test indexer."""
    defaults = dict(
        initial_lookup=DerefMarker(),
        strict=False,
        path=(PathKey(key="name"),),
        var_types=(),
        value_type=IndexerColumnType.STR,
    )
    defaults.update(overrides)
    return TypedPathValueIndexer(**defaults)


def _store_indexer(indexer: TypedPathValueIndexer, scope: FxtrScope) -> FxtrID:
    """Store an indexer entity and return its FxtrID."""
    return indexer.store(scope=scope).entity_id


def _make_arena(scope: FxtrScope, ids: list[FxtrID]):
    """Create a MemoryArena containing the given entities."""
    return scope._main_arena.copy_to_new_arena(scope.clone_authorization(), ids)


# ---------------------------------------------------------------------------
# get_live_registrations
# ---------------------------------------------------------------------------


class TestGetLiveRegistrations:
    @pytest.mark.anyio
    async def test_empty_namespace(self, tmp_db_path_str: str):
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            result = await get_live_registrations(ns, session)
            assert result == frozenset()

    @pytest.mark.anyio
    async def test_with_registrations(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid])

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.register_indexer(
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
            )

            result = await get_live_registrations(ns, session)
            assert result == frozenset({(iid, "")})

    @pytest.mark.anyio
    async def test_multiple_registrations(self, tmp_db_path_str: str, local_fxtr_scope: FxtrScope):
        idx1 = _make_indexer(path=(PathKey(key="name"),))
        idx2 = _make_indexer(path=(PathKey(key="score"),), value_type=IndexerColumnType.FLOAT)
        iid1 = _store_indexer(idx1, local_fxtr_scope)
        iid2 = _store_indexer(idx2, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid1, iid2])

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.register_indexer(
                iid1,
                source,
                local_fxtr_scope.clone_authorization(),
            )
            await ns.register_indexer(
                iid2,
                source,
                local_fxtr_scope.clone_authorization(),
                key_prefix="data/",
            )

            result = await get_live_registrations(ns, session)
            assert result == frozenset({(iid1, ""), (iid2, "data/")})


# ---------------------------------------------------------------------------
# remove_dead_index_registrations
# ---------------------------------------------------------------------------


class TestRemoveDeadIndexRegistrations:
    @pytest.mark.anyio
    async def test_no_dead_registrations(
        self, tmp_path: Path, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """When file and live state match, nothing is removed."""
        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid])
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.register_indexer(
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
            )

            # File matches live state
            write_index_file(index_file, frozenset({(iid, "")}))

            await remove_dead_index_registrations(index_file, ns, session)

            # Still registered
            live = await get_live_registrations(ns, session)
            assert live == frozenset({(iid, "")})

    @pytest.mark.anyio
    async def test_removes_dead_registration(
        self, tmp_path: Path, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Registration in worktree_ns but not in file gets removed."""
        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid])
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await ns.register_indexer(
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
            )

            # File is empty — registration is "dead"
            # (file doesn't exist = empty set)

            await remove_dead_index_registrations(index_file, ns, session)

            live = await get_live_registrations(ns, session)
            assert live == frozenset()

    @pytest.mark.anyio
    async def test_empty_namespace_empty_file(self, tmp_path: Path, tmp_db_path_str: str):
        """No-op when both are empty."""
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            await remove_dead_index_registrations(index_file, ns, session)

            live = await get_live_registrations(ns, session)
            assert live == frozenset()


# ---------------------------------------------------------------------------
# sync_index_registrations
# ---------------------------------------------------------------------------


class TestSyncIndexRegistrations:
    @pytest.mark.anyio
    async def test_no_changes(
        self, tmp_path: Path, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """When file and live state match, sync is a no-op."""
        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid])
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            worktree_ns = await session.create_or_retrieve_user_namespace("worktree")
            index_ns = await session.create_or_retrieve_user_namespace("index")

            # Register in worktree and pin indexer entity in backing store
            await worktree_ns.register_indexer(
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
            )
            await index_ns.pin_entity_from_arena(
                iid.id_string(),
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
                overwrite=True,
            )

            # Write matching file
            write_index_file(index_file, frozenset({(iid, "")}))

            await sync_index_registrations(index_file, worktree_ns, index_ns, session)

            live = await get_live_registrations(worktree_ns, session)
            assert live == frozenset({(iid, "")})

    @pytest.mark.anyio
    async def test_adds_new_indexer(
        self, tmp_path: Path, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Registration in file but not in worktree_ns gets added."""
        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid])
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            worktree_ns = await session.create_or_retrieve_user_namespace("worktree")
            index_ns = await session.create_or_retrieve_user_namespace("index")

            # Pin indexer entity in backing store (but don't register in worktree)
            await index_ns.pin_entity_from_arena(
                iid.id_string(),
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
                overwrite=True,
            )

            # File has the registration
            write_index_file(index_file, frozenset({(iid, "")}))

            await sync_index_registrations(index_file, worktree_ns, index_ns, session)

            live = await get_live_registrations(worktree_ns, session)
            assert live == frozenset({(iid, "")})

    @pytest.mark.anyio
    async def test_removes_dead_indexer(
        self, tmp_path: Path, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Registration in worktree_ns but not in file gets removed."""
        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid])
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            worktree_ns = await session.create_or_retrieve_user_namespace("worktree")
            index_ns = await session.create_or_retrieve_user_namespace("index")

            await worktree_ns.register_indexer(
                iid,
                source,
                local_fxtr_scope.clone_authorization(),
            )

            # File is empty
            await sync_index_registrations(index_file, worktree_ns, index_ns, session)

            live = await get_live_registrations(worktree_ns, session)
            assert live == frozenset()

    @pytest.mark.anyio
    async def test_adds_and_removes(
        self, tmp_path: Path, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """Sync handles both adds and removes simultaneously."""
        idx1 = _make_indexer(path=(PathKey(key="name"),))
        idx2 = _make_indexer(path=(PathKey(key="score"),), value_type=IndexerColumnType.FLOAT)
        iid1 = _store_indexer(idx1, local_fxtr_scope)
        iid2 = _store_indexer(idx2, local_fxtr_scope)
        source = _make_arena(local_fxtr_scope, [iid1, iid2])
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            worktree_ns = await session.create_or_retrieve_user_namespace("worktree")
            index_ns = await session.create_or_retrieve_user_namespace("index")

            # Register idx1 in worktree (but file only has idx2)
            await worktree_ns.register_indexer(
                iid1,
                source,
                local_fxtr_scope.clone_authorization(),
            )
            await index_ns.pin_entity_from_arena(
                iid2.id_string(),
                iid2,
                source,
                local_fxtr_scope.clone_authorization(),
                overwrite=True,
            )

            write_index_file(index_file, frozenset({(iid2, "")}))

            await sync_index_registrations(index_file, worktree_ns, index_ns, session)

            live = await get_live_registrations(worktree_ns, session)
            # idx1 removed, idx2 added
            assert live == frozenset({(iid2, "")})

    @pytest.mark.anyio
    async def test_missing_indexer_entity_raises(
        self, tmp_path: Path, tmp_db_path_str: str, local_fxtr_scope: FxtrScope
    ):
        """If indexer entity is missing from backing store, raises FxtrSyncError."""
        indexer = _make_indexer()
        iid = _store_indexer(indexer, local_fxtr_scope)
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            worktree_ns = await session.create_or_retrieve_user_namespace("worktree")
            index_ns = await session.create_or_retrieve_user_namespace("index")

            # File references an indexer, but it's NOT in the backing store
            write_index_file(index_file, frozenset({(iid, "")}))

            with pytest.raises(FxtrSyncError, match="missing from backing store"):
                await sync_index_registrations(index_file, worktree_ns, index_ns, session)

    @pytest.mark.anyio
    async def test_both_empty_is_noop(self, tmp_path: Path, tmp_db_path_str: str):
        """Sync with empty file and empty namespace is a no-op."""
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            worktree_ns = await session.create_or_retrieve_user_namespace("worktree")
            index_ns = await session.create_or_retrieve_user_namespace("index")

            await sync_index_registrations(index_file, worktree_ns, index_ns, session)

            live = await get_live_registrations(worktree_ns, session)
            assert live == frozenset()


# ---------------------------------------------------------------------------
# add_registration_to_file / remove_registration_from_file
# ---------------------------------------------------------------------------


class TestFileWriteHelpers:
    @pytest.mark.anyio
    async def test_add_to_empty_file(self, tmp_path: Path):
        fid = FxtrID.from_tag_and_payload("d", b"test1234567890ab")
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        await add_registration_to_file(index_file, fid, "")

        result = read_index_file(index_file)
        assert result == frozenset({(fid, "")})

    @pytest.mark.anyio
    async def test_add_to_existing_file(self, tmp_path: Path):
        fid1 = FxtrID.from_tag_and_payload("d", b"aaaa1234567890ab")
        fid2 = FxtrID.from_tag_and_payload("d", b"bbbb1234567890ab")
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        write_index_file(index_file, frozenset({(fid1, "")}))
        await add_registration_to_file(index_file, fid2, "data/")

        result = read_index_file(index_file)
        assert result == frozenset({(fid1, ""), (fid2, "data/")})

    @pytest.mark.anyio
    async def test_add_idempotent(self, tmp_path: Path):
        fid = FxtrID.from_tag_and_payload("d", b"test1234567890ab")
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        write_index_file(index_file, frozenset({(fid, "")}))
        await add_registration_to_file(index_file, fid, "")

        result = read_index_file(index_file)
        assert result == frozenset({(fid, "")})

    @pytest.mark.anyio
    async def test_remove_from_file(self, tmp_path: Path):
        fid1 = FxtrID.from_tag_and_payload("d", b"aaaa1234567890ab")
        fid2 = FxtrID.from_tag_and_payload("d", b"bbbb1234567890ab")
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        write_index_file(index_file, frozenset({(fid1, ""), (fid2, "data/")}))
        await remove_registration_from_file(index_file, fid1, "")

        result = read_index_file(index_file)
        assert result == frozenset({(fid2, "data/")})

    @pytest.mark.anyio
    async def test_remove_last_deletes_file(self, tmp_path: Path):
        fid = FxtrID.from_tag_and_payload("d", b"test1234567890ab")
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        write_index_file(index_file, frozenset({(fid, "")}))
        await remove_registration_from_file(index_file, fid, "")

        # write_index_file deletes the file when set is empty
        assert not Path(index_file).exists()
        result = read_index_file(index_file)
        assert result == frozenset()

    @pytest.mark.anyio
    async def test_remove_nonexistent_is_noop(self, tmp_path: Path):
        fid1 = FxtrID.from_tag_and_payload("d", b"aaaa1234567890ab")
        fid2 = FxtrID.from_tag_and_payload("d", b"bbbb1234567890ab")
        index_file = str(tmp_path / "fxtr_namespace_indexes.tsv")

        write_index_file(index_file, frozenset({(fid1, "")}))
        await remove_registration_from_file(index_file, fid2, "")

        result = read_index_file(index_file)
        assert result == frozenset({(fid1, "")})
