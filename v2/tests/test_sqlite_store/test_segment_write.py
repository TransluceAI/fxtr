"""Tests for segment file write path via bulk copy operations."""

import hashlib
import os
import uuid

import anyio
import pytest

from fxtr2.data import entity_core
from fxtr2.data.sqlite_store import sqlite_ops
from fxtr2.data.sqlite_store.entity_blob import (
    decode_cbor_bytes,
    decode_fxtr_id_bytes,
    decode_metadata,
)


def _setup_namespace_and_session(conn):
    """Create a minimal namespace and session for testing. Returns (namespace_id, session_id)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    session_id = sqlite_ops.insert_session(conn, uuid.uuid4().bytes, now)
    namespace_id, _ns_uuid = sqlite_ops.get_or_create_user_namespace(conn, "test_ns")
    return namespace_id, session_id


@pytest.mark.anyio
class TestSegmentWrite:
    """Test basic segment file write operations via bulk copy."""

    async def test_bulk_copy_writes_to_segment(self, tmp_path, ground_context):
        """Test that bulk_copy_commit writes entity data to segment files."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    # Set up namespace and session
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    # Build a d-node that depends on ground_context
                    test_data = {
                        "value": "test",
                        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(ground_context),
                    }
                    cbor_bytes = entity_core.fxtr_cbor_dumps(test_data)
                    content_hash = hashlib.sha256(cbor_bytes).digest()
                    entity_id_bytes = b"d" + content_hash

                    # Create save items: ground_context (no deps) and d-node (depends on ground)
                    from fxtr2.data.abstract_arena import DepKind
                    from fxtr2.data.entity_core import FxtrID

                    ground_save = sqlite_ops.SaveItem(
                        fxtr_id=ground_context,
                        public_id_bytes=ground_context._raw_data_id,
                        cbor_bytes=None,
                        deps=[],
                        deps_as_bytes=[],
                    )
                    entity_fxtr_id = FxtrID(raw_data_id=entity_id_bytes)
                    entity_save = sqlite_ops.SaveItem(
                        fxtr_id=entity_fxtr_id,
                        public_id_bytes=entity_id_bytes,
                        cbor_bytes=cbor_bytes,
                        deps=[(ground_context, DepKind.CTX_NODE)],
                        deps_as_bytes=[(ground_context._raw_data_id, "ctx_node")],
                    )

                    # Topological order: ground first, then entity
                    save_queue = [ground_save, entity_save]

                    # Big commit (single transaction)
                    resolved, arena_id = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        entity_id_bytes,
                        {},  # no preexisting
                        save_queue,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    internal_id = resolved[entity_id_bytes]
                    assert arena_id is not None

                    # Verify entity row has segment location
                    def _check_entity(conn):
                        cursor = conn.execute(
                            "SELECT seg_id, seg_offset, seg_length, storage_location "
                            "FROM entity WHERE id = ?",
                            (internal_id,),
                        )
                        return cursor.fetchone()

                    row = await pool_group.read_pool.run_sync_with_connection(_check_entity)
                    assert row is not None
                    seg_id, seg_offset, seg_length, storage_loc = row
                    assert storage_loc == "data2/seg"
                    assert seg_id == 0
                    assert seg_offset is not None
                    assert seg_length is not None

                    # Read blob from segment file and verify
                    blob = pool_group.segment_manager.read_record(seg_id, seg_offset, seg_length)
                    assert blob is not None

                    decoded_fxtr_id = decode_fxtr_id_bytes(blob)
                    assert decoded_fxtr_id == entity_id_bytes

                    decoded_cbor = decode_cbor_bytes(blob)
                    assert decoded_cbor == cbor_bytes

                    deps_list, precomp_map = decode_metadata(blob)
                    assert len([d for d in deps_list if d[1] == "ctx_node"]) == 1

                    assert internal_id in precomp_map
                    assert precomp_map[internal_id][0] == entity_id_bytes
                    # Verify 5th element exists (seg_offset for self)
                    assert len(precomp_map[internal_id]) == 5

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_bulk_copy_derived_node_empty_cbor(self, tmp_path, ground_context):
        """Test that derived nodes have empty CBOR sections in segment blobs."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    # Create entities: ground_context -> d-node -> c-node (derived, no CBOR)
                    from fxtr2.data.abstract_arena import DepKind
                    from fxtr2.data.entity_core import FxtrID

                    ctx_data = {
                        "value": "ctx",
                        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(ground_context),
                    }
                    ctx_cbor = entity_core.fxtr_cbor_dumps(ctx_data)
                    ctx_hash = hashlib.sha256(ctx_cbor).digest()
                    ctx_d_id_bytes = b"d" + ctx_hash
                    c_node_id_bytes = b"c" + ctx_hash

                    ground_save = sqlite_ops.SaveItem(
                        fxtr_id=ground_context,
                        public_id_bytes=ground_context._raw_data_id,
                        cbor_bytes=None,
                        deps=[],
                        deps_as_bytes=[],
                    )
                    ctx_fxtr_id = FxtrID(raw_data_id=ctx_d_id_bytes)
                    d_node_save = sqlite_ops.SaveItem(
                        fxtr_id=ctx_fxtr_id,
                        public_id_bytes=ctx_d_id_bytes,
                        cbor_bytes=ctx_cbor,
                        deps=[(ground_context, DepKind.CTX_NODE)],
                        deps_as_bytes=[(ground_context._raw_data_id, "ctx_node")],
                    )
                    c_fxtr_id = FxtrID(raw_data_id=c_node_id_bytes)
                    c_node_save = sqlite_ops.SaveItem(
                        fxtr_id=c_fxtr_id,
                        public_id_bytes=c_node_id_bytes,
                        cbor_bytes=None,  # Derived node, no CBOR
                        deps=[(ctx_fxtr_id, DepKind.STRONG_REF)],
                        deps_as_bytes=[(ctx_d_id_bytes, "strong_ref")],
                    )

                    save_queue = [ground_save, d_node_save, c_node_save]

                    resolved, arena_id = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        c_node_id_bytes,
                        {},
                        save_queue,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    c_internal_id = resolved[c_node_id_bytes]
                    ctx_internal_id = resolved[ctx_d_id_bytes]

                    # Read c-node blob from segment and verify empty CBOR
                    def _get_seg_loc(conn, iid):
                        cursor = conn.execute(
                            "SELECT seg_id, seg_offset, seg_length FROM entity WHERE id = ?",
                            (iid,),
                        )
                        return cursor.fetchone()

                    c_loc = await pool_group.read_pool.run_sync_with_connection(
                        _get_seg_loc, c_internal_id
                    )
                    blob = pool_group.segment_manager.read_record(*c_loc)

                    decoded_fxtr_id = decode_fxtr_id_bytes(blob)
                    assert decoded_fxtr_id == c_node_id_bytes

                    decoded_cbor = decode_cbor_bytes(blob)
                    assert decoded_cbor is None, "Derived node should have empty CBOR section"

                    deps_list, _precomp_map = decode_metadata(blob)
                    assert (ctx_internal_id, "strong_ref") in deps_list

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_bulk_copy_precomp_same_segment_offsets(self, tmp_path, ground_context):
        """Test that same-batch deps get seg_offset in precomp entries."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    from fxtr2.data.abstract_arena import DepKind
                    from fxtr2.data.entity_core import FxtrID

                    # ground (no deps) -> d-node (depends on ground)
                    d_data = {
                        "value": "d",
                        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(ground_context),
                    }
                    d_cbor = entity_core.fxtr_cbor_dumps(d_data)
                    d_hash = hashlib.sha256(d_cbor).digest()
                    d_id_bytes = b"d" + d_hash

                    ground_save = sqlite_ops.SaveItem(
                        fxtr_id=ground_context,
                        public_id_bytes=ground_context._raw_data_id,
                        cbor_bytes=None,
                        deps=[],
                        deps_as_bytes=[],
                    )
                    d_fxtr_id = FxtrID(raw_data_id=d_id_bytes)
                    d_save = sqlite_ops.SaveItem(
                        fxtr_id=d_fxtr_id,
                        public_id_bytes=d_id_bytes,
                        cbor_bytes=d_cbor,
                        deps=[(ground_context, DepKind.CTX_NODE)],
                        deps_as_bytes=[(ground_context._raw_data_id, "ctx_node")],
                    )

                    save_queue = [ground_save, d_save]

                    resolved, _arena_id = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        d_id_bytes,
                        {},
                        save_queue,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    ground_iid = resolved[ground_context._raw_data_id]
                    d_iid = resolved[d_id_bytes]

                    # Read d-node's blob
                    def _get_seg_loc(conn, iid):
                        cursor = conn.execute(
                            "SELECT seg_id, seg_offset, seg_length FROM entity WHERE id = ?",
                            (iid,),
                        )
                        return cursor.fetchone()

                    d_loc = await pool_group.read_pool.run_sync_with_connection(_get_seg_loc, d_iid)
                    blob = pool_group.segment_manager.read_record(*d_loc)

                    _deps_list, precomp_map = decode_metadata(blob)

                    # ground_context was written before d-node, so its precomp entry
                    # should have a non-null 5th element (seg_offset in same segment)
                    ground_entry = precomp_map[ground_iid]
                    assert len(ground_entry) == 5
                    assert ground_entry[4] is not None, "Same-batch dep should have seg_offset"
                    assert isinstance(ground_entry[4], int)

                    # d-node's own precomp entry will have None for seg_offset in
                    # the persisted blob — the offset isn't known until after encoding.
                    # The in-memory update only benefits later entities in the same batch.
                    d_entry = precomp_map[d_iid]
                    assert len(d_entry) == 5
                    assert (
                        d_entry[4] is None
                    ), "Own entry has None seg_offset in blob (written before offset known)"

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_bulk_copy_write_lock_file_created(self, tmp_path, ground_context):
        """Test that write.lock file is created in segments dir."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    ground_save = sqlite_ops.SaveItem(
                        fxtr_id=ground_context,
                        public_id_bytes=ground_context._raw_data_id,
                        cbor_bytes=None,
                        deps=[],
                        deps_as_bytes=[],
                    )

                    await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        ground_context._raw_data_id,
                        {},
                        [ground_save],
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    # write.lock should have been created
                    lock_path = os.path.join(pool_group.segments_dir, "write.lock")
                    assert os.path.exists(lock_path)

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_bulk_copy_updates_write_cursor(self, tmp_path, ground_context):
        """Test that write_cursor in segment_metadata is updated after commit."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    ground_save = sqlite_ops.SaveItem(
                        fxtr_id=ground_context,
                        public_id_bytes=ground_context._raw_data_id,
                        cbor_bytes=None,
                        deps=[],
                        deps_as_bytes=[],
                    )

                    await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        ground_context._raw_data_id,
                        {},
                        [ground_save],
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    # Check write_cursor is > 0
                    def _check_cursor(conn):
                        cursor = conn.execute(
                            "SELECT write_cursor FROM segment_metadata WHERE segment_id = 0"
                        )
                        return cursor.fetchone()[0]

                    cursor_val = await pool_group.read_pool.run_sync_with_connection(_check_cursor)
                    assert cursor_val > 0, "write_cursor should be updated after write"

                    # Verify segment file size matches
                    seg_path = pool_group.segment_manager.segment_path(0)
                    file_size = os.path.getsize(seg_path)
                    assert file_size == cursor_val

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()


def _make_d_node_save_items(ground_context, label: str):
    """Create ground + d-node save items with a unique d-node. Returns (save_queue, d_id_bytes)."""
    from fxtr2.data.abstract_arena import DepKind
    from fxtr2.data.entity_core import FxtrID

    d_data = {
        "value": label,
        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(ground_context),
    }
    d_cbor = entity_core.fxtr_cbor_dumps(d_data)
    d_hash = hashlib.sha256(d_cbor).digest()
    d_id_bytes = b"d" + d_hash

    ground_save = sqlite_ops.SaveItem(
        fxtr_id=ground_context,
        public_id_bytes=ground_context._raw_data_id,
        cbor_bytes=None,
        deps=[],
        deps_as_bytes=[],
    )
    d_fxtr_id = FxtrID(raw_data_id=d_id_bytes)
    d_save = sqlite_ops.SaveItem(
        fxtr_id=d_fxtr_id,
        public_id_bytes=d_id_bytes,
        cbor_bytes=d_cbor,
        deps=[(ground_context, DepKind.CTX_NODE)],
        deps_as_bytes=[(ground_context._raw_data_id, "ctx_node")],
    )
    return [ground_save, d_save], d_id_bytes


@pytest.mark.anyio
class TestSegmentSealing:
    """Test segment rotation and cross-segment reads."""

    async def test_batch_contiguity(self, tmp_path, ground_context):
        """All entities in a single batch land in the same segment."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    save_queue, d_id_bytes = _make_d_node_save_items(ground_context, "contiguity")

                    resolved, _arena_id = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        d_id_bytes,
                        {},
                        save_queue,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    # All entities should have the same seg_id
                    def _check_seg_ids(conn):
                        iids = list(resolved.values())
                        placeholders = ",".join("?" * len(iids))
                        cursor = conn.execute(
                            f"SELECT DISTINCT seg_id FROM entity WHERE id IN ({placeholders})",
                            iids,
                        )
                        return [row[0] for row in cursor.fetchall()]

                    seg_ids = await pool_group.read_pool.run_sync_with_connection(_check_seg_ids)
                    assert (
                        len(seg_ids) == 1
                    ), f"Expected all entities in same segment, got {seg_ids}"
                    assert seg_ids[0] == 0

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_rollover_trigger(self, tmp_path, ground_context):
        """Writing past max_segment_size seals the old segment and creates a new one."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    # Write batch 1 with a tiny max_segment_size to trigger rotation
                    save_queue, d_id_bytes = _make_d_node_save_items(ground_context, "rollover1")

                    resolved, _arena_id = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        d_id_bytes,
                        {},
                        save_queue,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                        200,  # max_segment_size: tiny, triggers rotation
                    )

                    # Check segment_metadata: segment 0 should be sealed, segment 1 active
                    def _check_segments(conn):
                        cursor = conn.execute(
                            "SELECT segment_id, status FROM segment_metadata ORDER BY segment_id"
                        )
                        return cursor.fetchall()

                    segments = await pool_group.read_pool.run_sync_with_connection(_check_segments)
                    assert len(segments) == 2
                    assert segments[0] == (0, "sealed")
                    assert segments[1] == (1, "active")

                    # New segment file should exist
                    seg1_path = pool_group.segment_manager.segment_path(1)
                    assert os.path.exists(seg1_path)

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_cross_segment_reads(self, tmp_path, ground_context):
        """Entities in segment 1 can reference deps in segment 0 (via SQLite lookup)."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    # Batch 1: ground + entity_A -> segment 0 (triggers rotation)
                    save_queue_1, a_id_bytes = _make_d_node_save_items(
                        ground_context, "cross_seg_a"
                    )

                    resolved_1, _ = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        a_id_bytes,
                        {},
                        save_queue_1,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                        200,  # tiny max triggers rotation after batch 1
                    )

                    # Batch 2: entity_B depends on entity_A (preexisting, in segment 0)
                    from fxtr2.data.abstract_arena import DepKind
                    from fxtr2.data.entity_core import FxtrID

                    b_data = {
                        "value": "cross_seg_b",
                        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(ground_context),
                    }
                    b_cbor = entity_core.fxtr_cbor_dumps(b_data)
                    b_hash = hashlib.sha256(b_cbor).digest()
                    b_id_bytes = b"d" + b_hash

                    a_fxtr_id = FxtrID(raw_data_id=a_id_bytes)
                    b_fxtr_id = FxtrID(raw_data_id=b_id_bytes)
                    b_save = sqlite_ops.SaveItem(
                        fxtr_id=b_fxtr_id,
                        public_id_bytes=b_id_bytes,
                        cbor_bytes=b_cbor,
                        deps=[(a_fxtr_id, DepKind.CTX_NODE)],
                        deps_as_bytes=[(a_id_bytes, "ctx_node")],
                    )

                    # entity_A and ground are preexisting for batch 2
                    preexisting = {
                        ground_context._raw_data_id: resolved_1[ground_context._raw_data_id],
                        a_id_bytes: resolved_1[a_id_bytes],
                    }

                    resolved_2, _ = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        b_id_bytes,
                        preexisting,
                        [b_save],
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    b_iid = resolved_2[b_id_bytes]
                    a_iid = resolved_1[a_id_bytes]

                    # Verify entity_A is in segment 0 and entity_B is in segment 1
                    def _get_seg_info(conn, iid):
                        cursor = conn.execute(
                            "SELECT seg_id, seg_offset, seg_length FROM entity WHERE id = ?",
                            (iid,),
                        )
                        return cursor.fetchone()

                    a_loc = await pool_group.read_pool.run_sync_with_connection(
                        _get_seg_info, a_iid
                    )
                    b_loc = await pool_group.read_pool.run_sync_with_connection(
                        _get_seg_info, b_iid
                    )
                    assert a_loc[0] == 0, "entity_A should be in segment 0"
                    assert b_loc[0] == 1, "entity_B should be in segment 1"

                    # Read entity_B's blob and verify precomp for entity_A has seg_offset=None
                    b_blob = pool_group.segment_manager.read_record(b_loc[0], b_loc[1], b_loc[2])
                    _deps_list, precomp_map = decode_metadata(b_blob)
                    a_entry = precomp_map[a_iid]
                    assert len(a_entry) == 5
                    assert (
                        a_entry[4] is None
                    ), "Cross-segment dep should have seg_offset=None (requires SQLite lookup)"

                    # But we can still read both blobs via segment_manager
                    a_blob = pool_group.segment_manager.read_record(a_loc[0], a_loc[1], a_loc[2])
                    assert decode_fxtr_id_bytes(a_blob) == a_id_bytes
                    assert decode_fxtr_id_bytes(b_blob) == b_id_bytes

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_inherited_precomp_offset_cleared_cross_segment(self, tmp_path, ground_context):
        """Inherited precomp seg_offsets must be None when they refer to a different segment.

        Bug scenario: Batch 1 writes ground + A to segment 0. A's precomp has
        ground -> seg_offset=X (valid offset in segment 0). Segment 0 is sealed.
        Batch 2 writes B to segment 1. B depends on A (preexisting), so B's precomp
        inherits A's precomp including ground -> seg_offset=X. But X is an offset in
        segment 0, not segment 1. The read path would naively cache ground's location
        as (seg_id=1, X, None), causing a read from the wrong segment.
        """
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    # Batch 1: ground + entity_A -> segment 0 (triggers rotation)
                    save_queue_1, a_id_bytes = _make_d_node_save_items(
                        ground_context, "inherited_precomp_a"
                    )

                    resolved_1, _ = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        a_id_bytes,
                        {},
                        save_queue_1,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                        200,  # tiny max triggers rotation after batch 1
                    )

                    ground_iid = resolved_1[ground_context._raw_data_id]

                    # Batch 2: entity_B depends on entity_A (preexisting, in segment 0)
                    from fxtr2.data.abstract_arena import DepKind
                    from fxtr2.data.entity_core import FxtrID

                    b_data = {
                        "value": "inherited_precomp_b",
                        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(ground_context),
                    }
                    b_cbor = entity_core.fxtr_cbor_dumps(b_data)
                    b_hash = hashlib.sha256(b_cbor).digest()
                    b_id_bytes = b"d" + b_hash

                    a_fxtr_id = FxtrID(raw_data_id=a_id_bytes)
                    b_fxtr_id = FxtrID(raw_data_id=b_id_bytes)
                    b_save = sqlite_ops.SaveItem(
                        fxtr_id=b_fxtr_id,
                        public_id_bytes=b_id_bytes,
                        cbor_bytes=b_cbor,
                        deps=[(a_fxtr_id, DepKind.CTX_NODE)],
                        deps_as_bytes=[(a_id_bytes, "ctx_node")],
                    )

                    preexisting = {
                        ground_context._raw_data_id: resolved_1[ground_context._raw_data_id],
                        a_id_bytes: resolved_1[a_id_bytes],
                    }

                    resolved_2, _ = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        b_id_bytes,
                        preexisting,
                        [b_save],
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    b_iid = resolved_2[b_id_bytes]

                    # Verify B is in segment 1
                    def _get_seg_info(conn, iid):
                        cursor = conn.execute(
                            "SELECT seg_id, seg_offset, seg_length FROM entity WHERE id = ?",
                            (iid,),
                        )
                        return cursor.fetchone()

                    b_loc = await pool_group.read_pool.run_sync_with_connection(
                        _get_seg_info, b_iid
                    )
                    assert b_loc[0] == 1, "entity_B should be in segment 1"

                    # Read B's blob and check ground's INHERITED precomp entry.
                    # Ground was written to segment 0. Its seg_offset in A's precomp
                    # referred to segment 0. B inherited this entry. Since B is in
                    # segment 1, the inherited seg_offset is stale and must be None.
                    b_blob = pool_group.segment_manager.read_record(b_loc[0], b_loc[1], b_loc[2])
                    _deps_list, precomp_map = decode_metadata(b_blob)

                    ground_entry = precomp_map[ground_iid]
                    assert len(ground_entry) == 5
                    assert ground_entry[4] is None, (
                        f"Inherited precomp seg_offset for ground should be None "
                        f"(ground is in segment 0, B is in segment 1), "
                        f"but got {ground_entry[4]}"
                    )

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()

    async def test_same_segment_precomp_offset_valid_envelope(self, tmp_path, ground_context):
        """Same-batch precomp offsets point to valid record envelopes."""
        db_path = str(tmp_path / "test.db")
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)

        try:
            async with anyio.create_task_group() as tg:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
                read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

                try:
                    namespace_id, session_id = await pool_group.write_pool.run_sync_with_connection(
                        _setup_namespace_and_session
                    )

                    save_queue, d_id_bytes = _make_d_node_save_items(
                        ground_context, "envelope_check"
                    )

                    resolved, _ = await pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.bulk_copy_commit,
                        namespace_id,
                        uuid.uuid4().bytes,
                        session_id,
                        d_id_bytes,
                        {},
                        save_queue,
                        pool_group.segment_manager,
                        pool_group.segments_dir,
                    )

                    ground_iid = resolved[ground_context._raw_data_id]
                    d_iid = resolved[d_id_bytes]

                    # Read d-node's blob and get ground's precomp seg_offset
                    def _get_seg_loc(conn, iid):
                        cursor = conn.execute(
                            "SELECT seg_id, seg_offset, seg_length FROM entity WHERE id = ?",
                            (iid,),
                        )
                        return cursor.fetchone()

                    d_loc = await pool_group.read_pool.run_sync_with_connection(_get_seg_loc, d_iid)
                    d_blob = pool_group.segment_manager.read_record(*d_loc)
                    _deps_list, precomp_map = decode_metadata(d_blob)

                    ground_entry = precomp_map[ground_iid]
                    precomp_offset = ground_entry[4]
                    assert precomp_offset is not None

                    # Use read_record_at to read from that offset — should recover ground's blob
                    ground_blob = pool_group.segment_manager.read_record_at(0, precomp_offset)
                    assert decode_fxtr_id_bytes(ground_blob) == ground_context._raw_data_id

                finally:
                    write_cancel_scope.cancel()
                    read_cancel_scope.cancel()

        finally:
            await pool_group.aclose()
