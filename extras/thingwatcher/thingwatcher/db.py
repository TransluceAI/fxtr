"""SQLite database layer for thingwatcher persistence.

Uses SQLAlchemy Core with async engine (aiosqlite driver) for database operations.
"""

from __future__ import annotations

import uuid
from typing import Any

import cbor2
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    select,
)
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

metadata = MetaData()

ticks = Table(
    "ticks",
    metadata,
    Column("tick", Integer, primary_key=True),
    Column("timestamp", Float, nullable=False),
    Column("level", Text, nullable=False),  # tick level for filtering
    Column("type", Text, nullable=False),  # tick type (e.g. "api_tick", "stream")
    Column("data", LargeBinary),  # CBOR encoded, nullable
    Column("seq", Integer, nullable=False),  # global sequence number for this tick
)

data_chunks = Table(
    "data_chunks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("data", LargeBinary, nullable=False),  # CBOR encoded data for this chunk
    Column("extends", Integer, ForeignKey("data_chunks.id")),  # NULL = start of chain
)

thing_states = Table(
    "thing_states",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("valid_from", Integer, ForeignKey("ticks.tick"), nullable=False),
    Column("valid_until", Integer, ForeignKey("ticks.tick")),  # NULL = still valid
    Column("valid_from_seq", Integer, nullable=False),  # sequence number when created
    Column("valid_to_seq", Integer),  # sequence number when invalidated, NULL = still valid
    Column("thing_key", LargeBinary, nullable=False),  # CBOR canonical
    Column("thing_type", Text, nullable=False),
    Column(
        "data_chunk_id", Integer, ForeignKey("data_chunks.id"), nullable=False
    ),  # pointer to data
    # Note: triggerable is NOT stored in DB - it's computed dynamically from watcher state
    # INVARIANT: thingstateId (thing_states.id) is exposed via GraphQL for client-side change detection.
    # For this to work correctly, a new thing_state record must be created whenever a thing's data_chunk
    # or links change. This ensures clients can detect changes by comparing thingstate IDs.
)

# Indexes defined separately to avoid issues with Table constructor
Index("idx_thing_states_key", thing_states.c.thing_key)
Index("idx_thing_states_valid", thing_states.c.valid_from, thing_states.c.valid_until)

links = Table(
    "links",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("from_thing_key", LargeBinary, nullable=False),  # CBOR canonical
    Column("valid_from", Integer, ForeignKey("ticks.tick"), nullable=False),
    Column("valid_until", Integer, ForeignKey("ticks.tick")),  # NULL = still valid
    Column("valid_from_seq", Integer, nullable=False),  # sequence number when created
    Column("valid_to_seq", Integer),  # sequence number when invalidated, NULL = still valid
    Column("link_type", Text, nullable=False),
    Column("link_key", LargeBinary, nullable=False),  # CBOR canonical
    Column("to_thing_key", LargeBinary, nullable=False),  # CBOR canonical
)

Index("idx_links_from_key", links.c.from_thing_key)
Index("idx_links_to_key", links.c.to_thing_key)
Index("idx_links_valid", links.c.valid_from, links.c.valid_until)

trigger_records = Table(
    "trigger_records",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("thing_key", LargeBinary, nullable=False),  # The triggered thing's key (CBOR canonical)
    Column("thingstate_id", Integer),  # The thingstate that was active at trigger time (nullable)
    Column("trigger_tick", Integer, ForeignKey("ticks.tick"), nullable=False),
    Column("payload", LargeBinary),  # CBOR-encoded payload (nullable)
)

Index("idx_trigger_records_key", trigger_records.c.thing_key)

short_ids = Table(
    "short_ids",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),  # The short ID (1, 2, 3, ...)
    Column("thing_key", LargeBinary, nullable=False, unique=True),  # CBOR canonical key
)

Index("idx_short_ids_key", short_ids.c.thing_key)

roots = Table(
    "roots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("root_name", Text, nullable=False),  # The user-given name for this root
    Column("thing_key", LargeBinary, nullable=False),  # CBOR canonical key of resolved thing
    Column("valid_from", Integer, ForeignKey("ticks.tick"), nullable=False),
    Column("valid_until", Integer, ForeignKey("ticks.tick")),  # NULL = still valid
    Column("valid_from_seq", Integer, nullable=False),  # sequence number when created
    Column("valid_to_seq", Integer),  # sequence number when invalidated, NULL = still valid
)

Index("idx_roots_name", roots.c.root_name)
Index("idx_roots_valid", roots.c.valid_from, roots.c.valid_until)


class Database:
    """Async SQLite database wrapper using SQLAlchemy Core."""

    def __init__(self, db_path: str | None) -> None:
        """Initialize the database.

        Args:
            db_path: Path to SQLite file, or None for in-memory database.
        """
        self._db_path = db_path
        self._engine: AsyncEngine | None = None

    async def connect(self) -> None:
        """Open database connection and create schema if needed."""
        if self._db_path is None:
            # In-memory with shared cache (allows multiple connections to same DB)
            uri = f"sqlite+aiosqlite:///file:memdb_{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
        else:
            uri = f"sqlite+aiosqlite:///{self._db_path}"

        self._engine = create_async_engine(uri)

        # Create tables
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def close(self) -> None:
        """Close database connection."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None

    def begin(self):
        """Return an async connection context manager for a transaction.

        Usage:
            async with db.begin() as conn:
                # all operations here are in one transaction
                await db.write_tick(conn, ...)
        """
        return self._engine.begin()

    # =========================================================================
    # Write Methods (all take a connection for transaction support)
    # =========================================================================

    async def write_tick(
        self,
        conn: AsyncConnection,
        tick: int,
        timestamp: float,
        level: str,
        tick_type: str,
        data: Any,
        seq: int,
    ) -> None:
        """Write a tick record within a transaction."""
        data_blob = cbor2.dumps(data) if data is not None else None
        await conn.execute(
            ticks.insert().values(
                tick=tick,
                timestamp=timestamp,
                level=level,
                type=tick_type,
                data=data_blob,
                seq=seq,
            )
        )

    async def update_tick_seq(
        self,
        conn: AsyncConnection,
        tick: int,
        new_seq: int,
        new_timestamp: float,
    ) -> None:
        """Update the seq and timestamp of an existing tick (for streaming updates)."""
        await conn.execute(
            ticks.update().where(ticks.c.tick == tick).values(seq=new_seq, timestamp=new_timestamp)
        )

    async def get_tick(self, conn: AsyncConnection, tick: int) -> Row | None:
        """Get a specific tick by number."""
        result = await conn.execute(select(ticks).where(ticks.c.tick == tick))
        return result.fetchone()

    async def write_data_chunk(
        self,
        conn: AsyncConnection,
        data: Any,
        extends: int | None = None,
    ) -> int:
        """Write a data chunk and return its id."""
        result = await conn.execute(
            data_chunks.insert().values(
                data=cbor2.dumps(data),
                extends=extends,
            )
        )
        assert result.inserted_primary_key is not None
        return result.inserted_primary_key[0]

    async def get_data_chunk_by_id(self, conn: AsyncConnection, chunk_id: int) -> Row | None:
        """Get a data chunk by its ID."""
        result = await conn.execute(select(data_chunks).where(data_chunks.c.id == chunk_id))
        return result.fetchone()

    async def walk_chunk_chain(
        self, conn: AsyncConnection, head_chunk_id: int, steps: int
    ) -> int | None:
        """Walk back N steps in the extends chain.

        Args:
            conn: Database connection
            head_chunk_id: The current head chunk ID
            steps: Number of steps to walk back (must be >= 0)

        Returns:
            Chunk ID after walking back N steps. Returns None if steps equals
            the chain length (walked past the start).

        Raises:
            ValueError: If steps > chain length (can't walk that far).
        """
        if steps < 0:
            raise ValueError("steps must be >= 0")

        if steps == 0:
            return head_chunk_id

        chunk_id: int = head_chunk_id
        for i in range(steps):
            chunk = await self.get_data_chunk_by_id(conn, chunk_id)
            if chunk is None:
                raise ValueError(f"Chunk {chunk_id} not found in database")

            extends_id = chunk.extends
            if extends_id is None:
                # We've reached the start of the chain
                if i == steps - 1:
                    # This was the last step - we walked exactly to the start
                    return None
                else:
                    # Can't walk any further
                    raise ValueError(f"Cannot walk {steps} steps: chain only has {i + 1} chunks")
            chunk_id = extends_id

        return chunk_id

    async def write_thing_state(
        self,
        conn: AsyncConnection,
        valid_from: int,
        valid_from_seq: int,
        thing_key: Any,
        thing_type: str,
        data_chunk_id: int,
    ) -> int:
        """Write a thing state and return its id."""
        result = await conn.execute(
            thing_states.insert().values(
                valid_from=valid_from,
                valid_from_seq=valid_from_seq,
                thing_key=cbor2.dumps(thing_key, canonical=True),
                thing_type=thing_type,
                data_chunk_id=data_chunk_id,
            )
        )
        # Use inserted_primary_key for reliable cross-backend behavior
        assert result.inserted_primary_key is not None
        return result.inserted_primary_key[0]

    async def invalidate_thing_state(
        self, conn: AsyncConnection, state_id: int, valid_until: int, valid_to_seq: int
    ) -> None:
        """Set valid_until and valid_to_seq on a thing state."""
        await conn.execute(
            thing_states.update()
            .where(thing_states.c.id == state_id)
            .values(valid_until=valid_until, valid_to_seq=valid_to_seq)
        )

    async def write_link(
        self,
        conn: AsyncConnection,
        valid_from: int,
        valid_from_seq: int,
        from_thing_key: Any,
        link_type: str,
        link_key: Any,
        to_thing_key: Any,
    ) -> int:
        """Write a link and return its id."""
        result = await conn.execute(
            links.insert().values(
                from_thing_key=cbor2.dumps(from_thing_key, canonical=True),
                valid_from=valid_from,
                valid_from_seq=valid_from_seq,
                link_type=link_type,
                link_key=cbor2.dumps(link_key, canonical=True),
                to_thing_key=cbor2.dumps(to_thing_key, canonical=True),
            )
        )
        # Use inserted_primary_key for reliable cross-backend behavior
        assert result.inserted_primary_key is not None
        return result.inserted_primary_key[0]

    async def invalidate_link(
        self, conn: AsyncConnection, link_id: int, valid_until: int, valid_to_seq: int
    ) -> None:
        """Set valid_until and valid_to_seq on a link."""
        await conn.execute(
            links.update()
            .where(links.c.id == link_id)
            .values(valid_until=valid_until, valid_to_seq=valid_to_seq)
        )

    # =========================================================================
    # Read Methods (for GraphQL resolvers)
    # =========================================================================

    async def get_latest_tick(self, conn: AsyncConnection) -> int | None:
        """Get the highest tick number, or None if no ticks."""
        result = await conn.execute(select(ticks.c.tick).order_by(ticks.c.tick.desc()).limit(1))
        row = result.fetchone()
        return row.tick if row else None

    async def get_ticks(
        self,
        conn: AsyncConnection,
        from_tick: int | None = None,
        to_tick: int | None = None,
        levels: list[str] | None = None,
    ) -> list[Row]:
        """Get ticks in range, optionally filtered by level."""
        query = select(ticks).order_by(ticks.c.tick)
        if from_tick is not None:
            query = query.where(ticks.c.tick >= from_tick)
        if to_tick is not None:
            query = query.where(ticks.c.tick <= to_tick)
        if levels:
            query = query.where(ticks.c.level.in_(levels))
        result = await conn.execute(query)
        return result.fetchall()

    async def get_things_at_tick(self, conn: AsyncConnection, tick: int) -> list[Row]:
        """Get all thing_states valid at the given tick."""
        query = select(thing_states).where(
            thing_states.c.valid_from <= tick,
            (thing_states.c.valid_until.is_(None)) | (thing_states.c.valid_until > tick),
        )
        result = await conn.execute(query)
        return result.fetchall()

    async def get_links_at_tick(self, conn: AsyncConnection, tick: int) -> list[Row]:
        """Get all links valid at the given tick."""
        query = select(links).where(
            links.c.valid_from <= tick,
            (links.c.valid_until.is_(None)) | (links.c.valid_until > tick),
        )
        result = await conn.execute(query)
        return result.fetchall()

    async def get_thing_data(
        self, conn: AsyncConnection, tick: int, key_bytes: bytes
    ) -> Row | None:
        """Get the thing_state for a specific key at a tick."""
        query = select(thing_states).where(
            thing_states.c.thing_key == key_bytes,
            thing_states.c.valid_from <= tick,
            (thing_states.c.valid_until.is_(None)) | (thing_states.c.valid_until > tick),
        )
        result = await conn.execute(query)
        return result.fetchone()

    async def get_thing_state_by_id(self, conn: AsyncConnection, state_id: int) -> Row | None:
        """Get a thing_state by its ID (for following data_extends chain)."""
        result = await conn.execute(select(thing_states).where(thing_states.c.id == state_id))
        return result.fetchone()

    async def get_thing_history(
        self,
        conn: AsyncConnection,
        key_bytes: bytes,
        from_tick: int | None = None,
        to_tick: int | None = None,
    ) -> list[Row]:
        """Get all states for a thing key that were valid at any point in the range.

        A state is included if it overlaps with [from_tick, to_tick]:
        - valid_from <= to_tick (started before or at the end of range)
        - valid_until is None or valid_until > from_tick (still valid after start of range)
        """
        query = (
            select(thing_states)
            .where(thing_states.c.thing_key == key_bytes)
            .order_by(thing_states.c.valid_from)
        )
        if from_tick is not None:
            # State must still be valid after from_tick
            query = query.where(
                (thing_states.c.valid_until.is_(None)) | (thing_states.c.valid_until > from_tick)
            )
        if to_tick is not None:
            # State must have started by to_tick
            query = query.where(thing_states.c.valid_from <= to_tick)
        result = await conn.execute(query)
        return result.fetchall()

    async def get_links_for_thing(
        self, conn: AsyncConnection, tick: int, from_key_bytes: bytes
    ) -> list[Row]:
        """Get all links from a specific thing at a tick."""
        query = select(links).where(
            links.c.from_thing_key == from_key_bytes,
            links.c.valid_from <= tick,
            (links.c.valid_until.is_(None)) | (links.c.valid_until > tick),
        )
        result = await conn.execute(query)
        return result.fetchall()

    async def get_backlinks_for_thing(
        self, conn: AsyncConnection, tick: int, to_key_bytes: bytes
    ) -> list[Row]:
        """Get all links pointing TO a specific thing at a tick."""
        query = select(links).where(
            links.c.to_thing_key == to_key_bytes,
            links.c.valid_from <= tick,
            (links.c.valid_until.is_(None)) | (links.c.valid_until > tick),
        )
        result = await conn.execute(query)
        return result.fetchall()

    # =========================================================================
    # Trigger Record Methods
    # =========================================================================

    async def write_trigger_record(
        self,
        conn: AsyncConnection,
        thing_key_bytes: bytes,
        thingstate_id: int | None,
        trigger_tick: int,
        payload: Any,
    ) -> int:
        """Write a trigger record and return its id."""
        payload_blob = cbor2.dumps(payload) if payload is not None else None
        result = await conn.execute(
            trigger_records.insert().values(
                thing_key=thing_key_bytes,
                thingstate_id=thingstate_id,
                trigger_tick=trigger_tick,
                payload=payload_blob,
            )
        )
        return result.lastrowid

    async def get_trigger_records(
        self,
        conn: AsyncConnection,
        key_bytes: bytes | None = None,
        from_tick: int | None = None,
        to_tick: int | None = None,
    ) -> list[Row]:
        """Get trigger records, optionally filtered."""
        query = select(trigger_records).order_by(trigger_records.c.trigger_tick)
        if key_bytes is not None:
            query = query.where(trigger_records.c.thing_key == key_bytes)
        if from_tick is not None:
            query = query.where(trigger_records.c.trigger_tick >= from_tick)
        if to_tick is not None:
            query = query.where(trigger_records.c.trigger_tick <= to_tick)
        result = await conn.execute(query)
        return result.fetchall()

    async def get_trigger_record_by_id(self, conn: AsyncConnection, record_id: int) -> Row | None:
        """Get a single trigger record by ID."""
        result = await conn.execute(
            select(trigger_records).where(trigger_records.c.id == record_id)
        )
        return result.fetchone()

    # =========================================================================
    # Short ID Methods
    # =========================================================================

    async def get_or_create_short_id(self, conn: AsyncConnection, key_bytes: bytes) -> int:
        """Get existing shortId or create a new one for the given key."""
        # Try to find existing
        result = await conn.execute(
            select(short_ids.c.id).where(short_ids.c.thing_key == key_bytes)
        )
        row = result.fetchone()
        if row:
            return row[0]

        # Insert new
        result = await conn.execute(short_ids.insert().values(thing_key=key_bytes))
        # Use inserted_primary_key for reliable cross-backend behavior
        assert result.inserted_primary_key is not None
        return result.inserted_primary_key[0]

    async def resolve_short_id(self, conn: AsyncConnection, short_id: int) -> bytes | None:
        """Look up key_bytes from shortId, or None if not found."""
        result = await conn.execute(select(short_ids.c.thing_key).where(short_ids.c.id == short_id))
        row = result.fetchone()
        return row[0] if row else None

    async def get_short_id(self, conn: AsyncConnection, key_bytes: bytes) -> int | None:
        """Look up shortId from key_bytes, or None if not assigned."""
        result = await conn.execute(
            select(short_ids.c.id).where(short_ids.c.thing_key == key_bytes)
        )
        row = result.fetchone()
        return row[0] if row else None

    # =========================================================================
    # Root Methods
    # =========================================================================

    async def write_root(
        self,
        conn: AsyncConnection,
        root_name: str,
        thing_key_bytes: bytes,
        valid_from: int,
        valid_from_seq: int,
    ) -> int:
        """Write a root binding and return its id."""
        result = await conn.execute(
            roots.insert().values(
                root_name=root_name,
                thing_key=thing_key_bytes,
                valid_from=valid_from,
                valid_from_seq=valid_from_seq,
            )
        )
        # Use inserted_primary_key for reliable cross-backend behavior
        assert result.inserted_primary_key is not None
        return result.inserted_primary_key[0]

    async def invalidate_root(
        self, conn: AsyncConnection, root_id: int, valid_until: int, valid_to_seq: int
    ) -> None:
        """Set valid_until and valid_to_seq on a root binding."""
        await conn.execute(
            roots.update()
            .where(roots.c.id == root_id)
            .values(valid_until=valid_until, valid_to_seq=valid_to_seq)
        )

    async def get_roots_at_tick(self, conn: AsyncConnection, tick: int) -> list[Row]:
        """Get all roots valid at the given tick."""
        query = select(roots).where(
            roots.c.valid_from <= tick,
            (roots.c.valid_until.is_(None)) | (roots.c.valid_until > tick),
        )
        result = await conn.execute(query)
        return result.fetchall()

    async def get_root_at_tick(
        self, conn: AsyncConnection, root_name: str, tick: int
    ) -> Row | None:
        """Get a specific root by name at a tick."""
        query = select(roots).where(
            roots.c.root_name == root_name,
            roots.c.valid_from <= tick,
            (roots.c.valid_until.is_(None)) | (roots.c.valid_until > tick),
        )
        result = await conn.execute(query)
        return result.fetchone()

    async def get_root_history(
        self,
        conn: AsyncConnection,
        root_name: str,
        from_tick: int | None = None,
        to_tick: int | None = None,
    ) -> list[Row]:
        """Get all bindings for a root name over time.

        A binding is included if it overlaps with [from_tick, to_tick]:
        - valid_from <= to_tick (started before or at the end of range)
        - valid_until is None or valid_until > from_tick (still valid after start of range)
        """
        query = select(roots).where(roots.c.root_name == root_name).order_by(roots.c.valid_from)
        if from_tick is not None:
            # Binding must still be valid after from_tick
            query = query.where((roots.c.valid_until.is_(None)) | (roots.c.valid_until > from_tick))
        if to_tick is not None:
            # Binding must have started by to_tick
            query = query.where(roots.c.valid_from <= to_tick)
        result = await conn.execute(query)
        return result.fetchall()

    # =========================================================================
    # Bulk Invalidation (for hard recapture)
    # =========================================================================

    async def invalidate_all_live(
        self, conn: AsyncConnection, valid_until: int, valid_to_seq: int
    ) -> None:
        """Invalidate all live records in thing_states, links, and roots.

        This is used during hard recapture to reset all state before taking
        a fresh snapshot. Sets valid_until and valid_to_seq on all records
        where valid_until IS NULL.

        Args:
            conn: Database connection (within a transaction).
            valid_until: The tick number to set as valid_until.
            valid_to_seq: The sequence number to set as valid_to_seq.
        """
        # Invalidate all live thing_states
        await conn.execute(
            thing_states.update()
            .where(thing_states.c.valid_until.is_(None))
            .values(valid_until=valid_until, valid_to_seq=valid_to_seq)
        )

        # Invalidate all live links
        await conn.execute(
            links.update()
            .where(links.c.valid_until.is_(None))
            .values(valid_until=valid_until, valid_to_seq=valid_to_seq)
        )

        # Invalidate all live roots
        await conn.execute(
            roots.update()
            .where(roots.c.valid_until.is_(None))
            .values(valid_until=valid_until, valid_to_seq=valid_to_seq)
        )
