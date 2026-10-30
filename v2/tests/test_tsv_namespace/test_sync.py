"""Tests for sync.py - TSV <-> SQLite synchronization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.tsv_namespace.config import TsvNamespaceConfig
from fxtr2.data.tsv_namespace.dirty import DirtyMarkManager, FlushResult
from fxtr2.data.tsv_namespace.sync import (
    FxtrSyncError,
    _compute_pin_prefix_for_tsv,
    _get_pins_for_tsv,
    _get_tsv_path_prefix,
    dump_sqlite_to_tsv,
    ensure_full_sync,
    ensure_tsv_synced,
    flush_tsv_to_disk,
    sync_tsv_to_sqlite,
)
from fxtr2.data.tsv_namespace.tsv_file import read_tsv, write_tsv
from fxtr2.data.repo_storage_config import RepoStoreConfig


def make_config(namespace_root: Path, reserved_prefix: str = "index") -> TsvNamespaceConfig:
    """Create a minimal TsvNamespaceConfig for testing."""
    return TsvNamespaceConfig(
        namespace_root=namespace_root,
        namespace_root_str=str(namespace_root),
        reserved_prefix=reserved_prefix,
        worktree_namespace_id="tsv_ns_worktree_test",
        repo_store=RepoStoreConfig(
            fxtr_dir=namespace_root / ".fxtr",
            sqlite_store_path=namespace_root / "test.sqlite",
            store_namespace_id="tsv_ns_index_test",
        ),
    )


def make_fxtr_id(seed: int) -> FxtrID:
    """Create a deterministic FxtrID for testing."""
    # Create a 32-byte payload from the seed
    payload = bytes([seed % 256] * 32)
    return FxtrID.from_tag_and_payload("d", payload)


def make_mock_session() -> MagicMock:
    """SQLiteFxtrSession stand-in: ``root_lease.clone()`` returns a stable mock lease."""
    session = MagicMock()
    lease = MagicMock()
    session.root_lease = lease
    lease.clone.return_value = lease
    return session


class TestGetTsvPathPrefix:
    """Tests for _get_tsv_path_prefix helper."""

    def test_index_tsv_at_root(self, tmp_path: Path):
        """index.fxtr.tsv at root returns 'index'."""
        tsv_path = f"{tmp_path}/index.fxtr.tsv"
        assert _get_tsv_path_prefix(tsv_path, str(tmp_path)) == "index"

    def test_other_tsv_at_root(self, tmp_path: Path):
        """other.fxtr.tsv at root returns 'other'."""
        tsv_path = f"{tmp_path}/other.fxtr.tsv"
        assert _get_tsv_path_prefix(tsv_path, str(tmp_path)) == "other"

    def test_nested_tsv(self, tmp_path: Path):
        """data/models.fxtr.tsv returns 'data/models'."""
        tsv_path = f"{tmp_path}/data/models.fxtr.tsv"
        assert _get_tsv_path_prefix(tsv_path, str(tmp_path)) == "data/models"

    def test_deeply_nested_tsv(self, tmp_path: Path):
        """a/b/c.fxtr.tsv returns 'a/b/c'."""
        tsv_path = f"{tmp_path}/a/b/c.fxtr.tsv"
        assert _get_tsv_path_prefix(tsv_path, str(tmp_path)) == "a/b/c"


class TestComputePinPrefixForTsv:
    """Tests for _compute_pin_prefix_for_tsv helper."""

    def test_reserved_prefix_returns_none(self, tmp_path: Path):
        """Reserved prefix TSV returns None (needs special handling)."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"
        assert _compute_pin_prefix_for_tsv(tsv_path, config) is None

    def test_non_reserved_returns_prefix_with_colon(self, tmp_path: Path):
        """Non-reserved TSV returns 'path:'."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/data/models.fxtr.tsv"
        assert _compute_pin_prefix_for_tsv(tsv_path, config) == "data/models:"

    def test_root_non_reserved(self, tmp_path: Path):
        """Non-reserved TSV at root returns 'name:'."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/other.fxtr.tsv"
        assert _compute_pin_prefix_for_tsv(tsv_path, config) == "other:"


class TestGetPinsForTsv:
    """Tests for _get_pins_for_tsv helper."""

    @pytest.mark.anyio
    async def test_reserved_prefix_filters_no_colon(self, tmp_path: Path):
        """Reserved prefix TSV gets pins without colons."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"

        # Mock namespace that returns all pins
        mock_ns = AsyncMock()
        mock_ns.list_pins.return_value = {
            "key1": make_fxtr_id(1),
            "key2": make_fxtr_id(2),
            "data/models:encoder": make_fxtr_id(3),
            "other:foo": make_fxtr_id(4),
        }

        mock_session = make_mock_session()
        result = await _get_pins_for_tsv(tsv_path, config, mock_ns, mock_session)

        # Should only get keys without colons
        assert set(result.keys()) == {"key1", "key2"}
        mock_ns.list_pins.assert_called_once_with("")

    @pytest.mark.anyio
    async def test_non_reserved_uses_prefix(self, tmp_path: Path):
        """Non-reserved TSV uses prefix filter."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/data/models.fxtr.tsv"

        mock_ns = AsyncMock()
        mock_ns.list_pins.return_value = {
            "data/models:encoder": make_fxtr_id(1),
            "data/models:decoder": make_fxtr_id(2),
        }

        mock_session = make_mock_session()
        result = await _get_pins_for_tsv(tsv_path, config, mock_ns, mock_session)

        assert set(result.keys()) == {"data/models:encoder", "data/models:decoder"}
        mock_ns.list_pins.assert_called_once_with("data/models:")


class TestSyncTsvToSqlite:
    """Tests for sync_tsv_to_sqlite."""

    @pytest.mark.anyio
    async def test_syncs_entries_from_tsv(self, tmp_path: Path):
        """Sync creates pins from TSV entries."""
        config = make_config(tmp_path)
        tsv_path_p = tmp_path / "data" / "models.fxtr.tsv"
        tsv_path_p.parent.mkdir(parents=True, exist_ok=True)

        id1 = make_fxtr_id(1)
        id2 = make_fxtr_id(2)
        write_tsv(tsv_path_p, {"encoder": id1, "decoder": id2})

        # Mock namespaces
        mock_arena = MagicMock()
        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {}
        mock_index = AsyncMock()

        # retrieve_pinned returns (fxtr_id, arena) - we need to return the right ID for each call
        async def mock_retrieve(id_string):
            fxtr_id = id1 if id_string == id1.id_string() else id2
            return (fxtr_id, mock_arena)

        mock_index.retrieve_pinned.side_effect = mock_retrieve

        mock_session = make_mock_session()
        await sync_tsv_to_sqlite(str(tsv_path_p), config, mock_worktree, mock_index, mock_session)

        # Should have called pin_entity_from_arena for each entry
        assert mock_worktree.pin_entity_from_arena.call_count == 2
        calls = {c.args[0]: c.args[1] for c in mock_worktree.pin_entity_from_arena.call_args_list}
        assert calls["data/models:encoder"] == id1
        assert calls["data/models:decoder"] == id2

    @pytest.mark.anyio
    async def test_removes_pins_not_in_tsv(self, tmp_path: Path):
        """Sync removes pins that aren't in TSV."""
        config = make_config(tmp_path)
        tsv_path_p = tmp_path / "data" / "models.fxtr.tsv"
        tsv_path_p.parent.mkdir(parents=True, exist_ok=True)

        id1 = make_fxtr_id(1)
        write_tsv(tsv_path_p, {"encoder": id1})

        mock_arena = MagicMock()
        mock_worktree = AsyncMock()
        # Current pins include one that's not in TSV
        mock_worktree.list_pins.return_value = {
            "data/models:encoder": id1,
            "data/models:old_key": make_fxtr_id(99),
        }
        mock_index = AsyncMock()
        mock_index.retrieve_pinned.return_value = (id1, mock_arena)

        mock_session = make_mock_session()
        await sync_tsv_to_sqlite(str(tsv_path_p), config, mock_worktree, mock_index, mock_session)

        # Should unpin the old key
        mock_worktree.unpin_entity.assert_called_once()
        assert mock_worktree.unpin_entity.call_args.args[0] == "data/models:old_key"

    @pytest.mark.anyio
    async def test_missing_tsv_removes_all_pins(self, tmp_path: Path):
        """Sync of missing TSV removes all pins with that prefix."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/data/models.fxtr.tsv"
        # Don't create the file

        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {
            "data/models:encoder": make_fxtr_id(1),
            "data/models:decoder": make_fxtr_id(2),
        }
        mock_index = AsyncMock()

        mock_session = make_mock_session()
        await sync_tsv_to_sqlite(tsv_path, config, mock_worktree, mock_index, mock_session)

        # Should unpin all existing keys
        assert mock_worktree.unpin_entity.call_count == 2
        unpinned = {c.args[0] for c in mock_worktree.unpin_entity.call_args_list}
        assert unpinned == {"data/models:encoder", "data/models:decoder"}

    @pytest.mark.anyio
    async def test_raises_on_missing_entity(self, tmp_path: Path):
        """Sync raises FxtrSyncError if entity not in index namespace."""
        config = make_config(tmp_path)
        tsv_path_p = tmp_path / "index.fxtr.tsv"

        id1 = make_fxtr_id(1)
        write_tsv(tsv_path_p, {"mykey": id1})

        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {}
        mock_index = AsyncMock()
        mock_index.retrieve_pinned.side_effect = KeyError("not found")  # Entity not found

        mock_session = make_mock_session()
        with pytest.raises(FxtrSyncError) as exc_info:
            await sync_tsv_to_sqlite(
                str(tsv_path_p), config, mock_worktree, mock_index, mock_session
            )

        assert "Missing entities" in str(exc_info.value)
        assert "mykey" in str(exc_info.value)

    @pytest.mark.anyio
    async def test_reserved_prefix_keys_no_colon(self, tmp_path: Path):
        """Sync of reserved prefix TSV uses keys without colons."""
        config = make_config(tmp_path)
        tsv_path_p = tmp_path / "index.fxtr.tsv"

        id1 = make_fxtr_id(1)
        write_tsv(tsv_path_p, {"mykey": id1})

        mock_arena = MagicMock()
        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {}
        mock_index = AsyncMock()
        mock_index.retrieve_pinned.return_value = (id1, mock_arena)

        mock_session = make_mock_session()
        await sync_tsv_to_sqlite(str(tsv_path_p), config, mock_worktree, mock_index, mock_session)

        # Full key should be "mykey" (no colon)
        mock_worktree.pin_entity_from_arena.assert_called_once()
        call_args = mock_worktree.pin_entity_from_arena.call_args
        assert call_args.args[0] == "mykey"


class TestDumpSqliteToTsv:
    """Tests for dump_sqlite_to_tsv."""

    @pytest.mark.anyio
    async def test_writes_pins_to_tsv(self, tmp_path: Path):
        """Dump writes pins to TSV file."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/data/models.fxtr.tsv"

        id1 = make_fxtr_id(1)
        id2 = make_fxtr_id(2)

        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {
            "data/models:encoder": id1,
            "data/models:decoder": id2,
        }

        # dump_sqlite_to_tsv now takes string paths
        mock_session = make_mock_session()
        await dump_sqlite_to_tsv(tsv_path, tsv_path, config, mock_worktree, mock_session)

        # Read back and verify
        entries = read_tsv(tsv_path)
        assert entries == {"encoder": id1, "decoder": id2}

    @pytest.mark.anyio
    async def test_empty_pins_writes_nothing(self, tmp_path: Path):
        """Dump with no pins writes nothing to dest_path."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"
        dest_path = tmp_path / "temp_output.tsv"

        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {}

        # dump_sqlite_to_tsv now writes nothing if there are no pins
        # (caller handles deletion of original file)
        mock_session = make_mock_session()
        await dump_sqlite_to_tsv(tsv_path, str(dest_path), config, mock_worktree, mock_session)

        # Dest path should not exist (nothing was written)
        assert not dest_path.exists()

    @pytest.mark.anyio
    async def test_reserved_prefix_queries_all_filters_no_colon(self, tmp_path: Path):
        """Dump for reserved prefix TSV queries all and filters."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"

        id1 = make_fxtr_id(1)
        id2 = make_fxtr_id(2)

        mock_worktree = AsyncMock()
        # Return mix of keys with and without colons
        mock_worktree.list_pins.return_value = {
            "key1": id1,
            "key2": id2,
            "other:foo": make_fxtr_id(3),  # Should be filtered out
        }

        mock_session = make_mock_session()
        await dump_sqlite_to_tsv(tsv_path, tsv_path, config, mock_worktree, mock_session)

        # Only keys without colons should be written
        entries = read_tsv(tsv_path)
        assert entries == {"key1": id1, "key2": id2}


class TestEnsureTsvSynced:
    """Tests for ensure_tsv_synced."""

    @pytest.mark.anyio
    async def test_uses_dirty_manager_ensure_synced(self, tmp_path: Path):
        """ensure_tsv_synced delegates to dirty_manager.ensure_synced."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"

        dirty_manager = DirtyMarkManager(str(tmp_path))
        mock_worktree = AsyncMock()
        mock_index = AsyncMock()

        # Mock ensure_synced to track callback
        callback_called = []

        async def mock_ensure_synced(path, callback):
            callback_called.append(path)
            # Don't call the callback (simulating skip)
            return False

        dirty_manager.ensure_synced = mock_ensure_synced

        mock_session = make_mock_session()
        result = await ensure_tsv_synced(
            tsv_path, config, dirty_manager, mock_worktree, mock_index, mock_session
        )

        assert result is False
        assert len(callback_called) == 1
        assert callback_called[0] == tsv_path

    @pytest.mark.anyio
    async def test_returns_true_when_sync_performed(self, tmp_path: Path):
        """ensure_tsv_synced returns True when sync is performed."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"

        dirty_manager = DirtyMarkManager(str(tmp_path))

        async def mock_ensure_synced(path, callback):
            await callback()  # Call the sync callback
            return True

        dirty_manager.ensure_synced = mock_ensure_synced

        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {}
        mock_index = AsyncMock()

        mock_session = make_mock_session()
        result = await ensure_tsv_synced(
            tsv_path, config, dirty_manager, mock_worktree, mock_index, mock_session
        )

        assert result is True


class TestEnsureFullSync:
    """Tests for ensure_full_sync."""

    @pytest.mark.anyio
    async def test_syncs_tsvs_from_disk_scan(self, tmp_path: Path):
        """Full sync processes TSVs found on disk."""
        config = make_config(tmp_path)

        # Create a TSV on disk
        tsv_path_p = tmp_path / "data" / "models.fxtr.tsv"
        tsv_path_p.parent.mkdir(parents=True, exist_ok=True)
        write_tsv(tsv_path_p, {"encoder": make_fxtr_id(1)})

        dirty_manager = DirtyMarkManager(str(tmp_path))

        synced_paths = []

        async def mock_ensure_synced(path, callback):
            synced_paths.append(path)
            return True

        dirty_manager.ensure_synced = mock_ensure_synced

        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {}
        mock_index = AsyncMock()

        mock_session = make_mock_session()
        await ensure_full_sync(config, dirty_manager, mock_worktree, mock_index, mock_session)

        # Should have synced the TSV found on disk (discover resolves paths)
        assert str(tsv_path_p.resolve()) in synced_paths

    @pytest.mark.anyio
    async def test_syncs_tsvs_implied_by_pins(self, tmp_path: Path):
        """Full sync processes TSVs implied by existing pins."""
        config = make_config(tmp_path)

        dirty_manager = DirtyMarkManager(str(tmp_path))

        synced_paths = []

        async def mock_ensure_synced(path, callback):
            synced_paths.append(path)
            return True

        dirty_manager.ensure_synced = mock_ensure_synced

        mock_worktree = AsyncMock()
        # Existing pin implies a TSV that may not exist on disk
        mock_worktree.list_pins.return_value = {
            "data/models:encoder": make_fxtr_id(1),
        }
        mock_index = AsyncMock()

        mock_session = make_mock_session()
        await ensure_full_sync(config, dirty_manager, mock_worktree, mock_index, mock_session)

        # Should have tried to sync the implied TSV
        expected_path = f"{tmp_path}/data/models.fxtr.tsv"
        assert expected_path in synced_paths

    @pytest.mark.anyio
    async def test_orphan_cleanup_when_tsv_deleted(self, tmp_path: Path):
        """Full sync removes orphan pins when TSV is deleted."""
        config = make_config(tmp_path)

        dirty_manager = DirtyMarkManager(str(tmp_path))

        # Track sync callbacks
        sync_callbacks_called = []

        async def mock_ensure_synced(path, callback):
            sync_callbacks_called.append((path, callback))
            await callback()  # Actually call the sync
            return True

        dirty_manager.ensure_synced = mock_ensure_synced

        mock_worktree = AsyncMock()
        # Pin exists for a TSV that doesn't exist on disk
        mock_worktree.list_pins.return_value = {
            "data/models:encoder": make_fxtr_id(1),
        }
        mock_index = AsyncMock()

        mock_session = make_mock_session()
        await ensure_full_sync(config, dirty_manager, mock_worktree, mock_index, mock_session)

        # Should have called unpin_entity for the orphan
        assert mock_worktree.unpin_entity.call_args.args[0] == "data/models:encoder"


class TestFlushTsvToDisk:
    """Tests for flush_tsv_to_disk."""

    @pytest.mark.anyio
    async def test_uses_dirty_manager_flush(self, tmp_path: Path):
        """flush_tsv_to_disk delegates to dirty_manager.flush."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"

        dirty_manager = DirtyMarkManager(str(tmp_path))

        # Mock flush to track callback
        dump_called = []

        async def mock_flush(path, callback, force=False):
            dump_called.append(path)
            return FlushResult.NO_MARK

        dirty_manager.flush = mock_flush

        mock_worktree = AsyncMock()

        mock_session = make_mock_session()
        result = await flush_tsv_to_disk(
            tsv_path, config, dirty_manager, mock_worktree, mock_session
        )

        assert result == FlushResult.NO_MARK
        assert len(dump_called) == 1
        assert dump_called[0] == tsv_path

    @pytest.mark.anyio
    async def test_dump_callback_writes_tsv(self, tmp_path: Path):
        """flush_tsv_to_disk's dump callback writes to the temp path."""
        config = make_config(tmp_path)
        tsv_path = f"{tmp_path}/index.fxtr.tsv"
        temp_path = f"{tmp_path}/temp_dump.tsv"

        dirty_manager = DirtyMarkManager(str(tmp_path))

        # Track and call the dump callback with temp_path (simulating dirty manager)
        async def mock_flush(path, callback, force=False):
            await callback(temp_path)  # Call the dump with temp path
            # Simulate what dirty manager does: move temp to tsv
            import os

            os.rename(temp_path, tsv_path)
            return FlushResult.COMPLETE

        dirty_manager.flush = mock_flush

        id1 = make_fxtr_id(1)
        mock_worktree = AsyncMock()
        mock_worktree.list_pins.return_value = {"mykey": id1}

        mock_session = make_mock_session()
        result = await flush_tsv_to_disk(
            tsv_path, config, dirty_manager, mock_worktree, mock_session
        )

        assert result == FlushResult.COMPLETE
        # TSV should have been written (via rename from temp)
        entries = read_tsv(tsv_path)
        assert entries == {"mykey": id1}
