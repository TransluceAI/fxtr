"""The entity store on Postgres: a write-behind :class:`~fxtr.core.entities.FxtrEntityStorage`.

The storage contract's ``store_raw_data`` and ``is_known`` are synchronous while the database is
not, so :class:`PostgresEntityStorage` keeps two pieces of local state:

* the **pending** entities, stored since the last :meth:`~PostgresEntityStorage.flush` and held
  in memory as encoded bytes until one is called; and
* the **confirmed** ids, those this instance has seen in the database: everything it has flushed
  or loaded, plus the direct references of every loaded value (which the storage invariant
  guarantees are in the database too).

An id is *known* when it is pending or confirmed. An entity that is in the database but that this
instance has never seen is therefore not known until it is loaded or checked with
:meth:`~PostgresEntityStorage.confirm_known`; this keeps ``is_known`` honest and synchronous.
Loads read pending entities from memory and everything else from the database.

A flush is the durability point. It is usually part of a larger operation's transaction
(persisting an array that references the entities, say), so :meth:`~PostgresEntityStorage.flush`
takes the :class:`~fxtr.runner.postgres.database.Transaction` and does its bookkeeping only once
that transaction commits. Pending entities are lost if the process dies before a flush.

**One instance per writing context.** An instance is cheap: a buffer and a set over a shared
:class:`~fxtr.runner.postgres.database.Database`. Create one for each context that stores
entities and has a natural point at which it commits them: a client session in a notebook, or,
in the runner, one per attempt, flushed in every transaction the attempt opens and dropped when
the attempt's task ends. Each instance then has one committer, and also its own
view of what is known: an attempt's instance knows only what the attempt loaded or stored, which
is the visibility we want. Instances over the same database may freely store the same content;
the database deduplicates it (inserts are on-conflict-do-nothing, in sorted cid order so two
transactions inserting overlapping sets cannot deadlock).

Sharing one instance between tasks that commit independently is still correct, just not the
intended arrangement: a flush snapshots the pending map synchronously, inserts exactly the
snapshot, and on commit confirms and pops exactly the snapshot, so a store landing while a flush
is awaited stays pending for the next one, and two flushes of overlapping snapshots each land or
roll back on their own with idempotent bookkeeping. The cost is coupling through the database: a
transaction inserting an entity another uncommitted transaction has inserted waits for it.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from fxtr.core import codec
from fxtr.core.codec import iter_entity_ids
from fxtr.core.entities import BoundID, Entity, FxtrEntityStorage
from fxtr.core.entity_data import (
    EntityID,
    EntityNotFoundError,
    FxtrDataValue,
    StorableFxtrDataValue,
    get_entity_type_tag,
)
from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.schema import entities
from fxtr.storage.util import EncodedEntity, encode_for_store

_IDS_PER_QUERY = 10_000
"""The most stored ids one query of :meth:`PostgresEntityStorage.load_raw_data_many` reads."""


class PostgresEntityStorage(FxtrEntityStorage):
    """Entity storage over the ``entities`` table of a :class:`Database`.

    Cheap to construct; make one per writing context (see the module docstring).

    Args:
        database: The database holding the ``entities`` table.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._pending: dict[bytes, EncodedEntity] = {}
        self._confirmed: set[bytes] = set()

    @property
    def pending_count(self) -> int:
        """How many stored entities have not been flushed (and committed) yet."""
        return len(self._pending)

    def _is_known_cid(self, cid: bytes) -> bool:
        return cid in self._pending or cid in self._confirmed

    def is_known(self, entity_id: EntityID) -> bool:
        return self._is_known_cid(entity_id.cid)

    def store_raw_data_sync(self, data: StorableFxtrDataValue) -> EntityID:
        """Store a value synchronously; see :meth:`store_raw_data`. Nothing here needs to await:
        the store completes into the pending buffer (see the module docstring)."""
        cid, entity = encode_for_store(data, self._is_known_cid)
        self._pending[cid] = entity
        return EntityID(cid=cid)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return self.store_raw_data_sync(data)

    def store_sync[T: Entity](self, entity: T) -> BoundID[T]:
        """Store an entity synchronously; see
        :meth:`~fxtr.core.entities.FxtrEntityStorage.store`."""
        entity_id = self.store_raw_data_sync(entity._fxtr_serialize())
        return BoundID(entity_id=entity_id, loader=entity._fxtr_loader())

    async def load_raw_data(
        self, entity_id: EntityID, tx: Transaction | None = None
    ) -> FxtrDataValue:
        """Load an entity: from the pending buffer if it is there, else from the database.

        Args:
            entity_id: The id.
            tx: The transaction to read in, so that entities flushed into it (by this or another
                store instance) are visible; ``None`` opens one just for this read.
        """
        cid = entity_id.cid
        if (pending := self._pending.get(cid)) is not None:
            encoded = pending.encoded
        else:
            if tx is None:
                async with self._database.transaction() as own:
                    return await self.load_raw_data(entity_id, own)
            row = (await tx.execute(select(entities.c.data).where(entities.c.cid == cid))).first()
            if row is None:
                raise EntityNotFoundError(f"Entity not found in storage: {entity_id!r}")
            encoded = row[0]
        data = codec.decode(encoded)
        get_entity_type_tag(data)
        # The value and, by the storage invariant, everything it references are in the database
        # (or pending), so ids obtained by loading can be referenced from new values.
        self._confirmed.add(cid)
        self._confirmed.update(ref.cid for ref in iter_entity_ids(data))
        return data

    async def load_raw_data_many(
        self, entity_ids: Sequence[EntityID], tx: Transaction | None = None
    ) -> list[FxtrDataValue]:
        """:meth:`load_raw_data` for many ids at once, in their order, reading the stored ones in
        one query per :data:`_IDS_PER_QUERY` ids (and opening no transaction when none has to be
        read).

        Args:
            entity_ids: The ids. Repeats are allowed.
            tx: The transaction to read in; ``None`` opens one just for this read.

        Raises:
            EntityNotFoundError: If any of them is not in storage.
        """
        found: list[tuple[bytes, str] | None] = []
        for batch in itertools.batched(entity_ids, _IDS_PER_QUERY):
            found.extend(await self.load_raw_bytes_many(batch, tx))
        values: list[FxtrDataValue] = []
        for entity_id, loaded in zip(entity_ids, found, strict=True):
            if loaded is None:
                raise EntityNotFoundError(f"Entity not found in storage: {entity_id!r}")
            data = codec.decode(loaded[0])
            get_entity_type_tag(data)
            self._confirmed.add(entity_id.cid)
            self._confirmed.update(ref.cid for ref in iter_entity_ids(data))
            values.append(data)
        return values

    async def load_raw_bytes(
        self, entity_id: EntityID, tx: Transaction | None = None
    ) -> tuple[bytes, str] | None:
        """The stored DRISL bytes and ``type_tag`` of an entity, verbatim, or ``None`` if it is not
        stored.

        For callers that want the encoded bytes as-is (a raw passthrough to a client that decodes
        them itself), rather than the decoded value :meth:`load_raw_data` returns. Serves the
        pending buffer from memory and everything else from the database; it does no decoding and
        no ``$type`` validation.

        Args:
            entity_id: The id.
            tx: The transaction to read in, so that entities flushed into it are visible; ``None``
                opens one just for this read.
        """
        (result,) = await self.load_raw_bytes_many([entity_id], tx)
        return result

    async def load_raw_bytes_many(
        self, entity_ids: Sequence[EntityID], tx: Transaction | None = None
    ) -> list[tuple[bytes, str] | None]:
        """:meth:`load_raw_bytes` for many ids at once, in their order, reading every stored one
        in a single query (and opening no transaction when none has to be read).

        Args:
            entity_ids: The ids. Repeats are allowed.
            tx: The transaction to read in; ``None`` opens one just for this read.
        """
        results: list[tuple[bytes, str] | None] = [None] * len(entity_ids)
        stored: dict[bytes, list[int]] = {}
        for i, entity_id in enumerate(entity_ids):
            if (pending := self._pending.get(entity_id.cid)) is not None:
                results[i] = pending.encoded, pending.type_tag
            else:
                stored.setdefault(entity_id.cid, []).append(i)
        if not stored:
            return results
        if tx is None:
            async with self._database.transaction() as own:
                return await self.load_raw_bytes_many(entity_ids, own)
        rows = await tx.execute(
            select(entities.c.cid, entities.c.data, entities.c.type_tag).where(
                entities.c.cid.in_(list(stored))
            )
        )
        for row in rows:
            cid = bytes(row.cid)
            self._confirmed.add(cid)
            for i in stored[cid]:
                results[i] = bytes(row.data), row.type_tag
        return results

    async def flush(self, tx: Transaction | None = None) -> None:
        """Write every pending entity as part of a transaction.

        Writes a snapshot of the pending set taken when the call starts; entities stored after
        that stay pending for the next flush. Entities already present (stored by another
        process, say) are left untouched. Once the transaction commits, the snapshot's entities
        count as confirmed and leave the pending set; if it rolls back they stay pending.
        Flushing one instance from concurrent transactions is safe but not the intended
        arrangement (see the module docstring).

        Args:
            tx: The transaction to write in; ``None`` opens one just for this flush.
        """
        if tx is None:
            async with self._database.transaction() as own:
                await self.flush(own)
            return
        snapshot = dict(self._pending)
        if not snapshot:
            return
        # Sorted by cid so two transactions inserting overlapping sets cannot deadlock.
        rows = [
            {"cid": cid, "type_tag": snapshot[cid].type_tag, "data": snapshot[cid].encoded}
            for cid in sorted(snapshot)
        ]
        await tx.execute(insert(entities).on_conflict_do_nothing(), rows)

        def committed() -> None:
            self._confirmed.update(snapshot)
            for cid in snapshot:
                self._pending.pop(cid, None)

        tx.on_commit(committed)

    def assume_known(self, entity_ids: Iterable[EntityID]) -> None:
        """Mark ids as confirmed without consulting the database.

        For ids the caller has read from committed database state that the system guarantees
        references only existing entities: the rows of a project array, say, whose references
        were verified when they were written and whose entities are never deleted. This is what
        :meth:`load_raw_data` does for a loaded value's references; it is not for ids from
        anywhere else, which :meth:`confirm_known` checks.

        Args:
            entity_ids: The ids.
        """
        self._confirmed.update(entity_id.cid for entity_id in entity_ids)

    async def confirm_known(
        self, entity_ids: Iterable[EntityID], tx: Transaction | None = None
    ) -> frozenset[EntityID]:
        """Check which of the given ids are known, consulting the database for those this
        instance has not seen. Ids found there become known once the transaction commits.

        Args:
            entity_ids: The ids to check.
            tx: The transaction to query in, so that entities flushed into it earlier count;
                ``None`` opens one just for this check.

        Returns:
            The subset of ``entity_ids`` that are known after the check.
        """
        if tx is None:
            async with self._database.transaction() as own:
                return await self.confirm_known(entity_ids, own)
        ids = frozenset(entity_ids)
        unseen = [e.cid for e in ids if not self._is_known_cid(e.cid)]
        found: set[bytes] = set()
        for start in range(0, len(unseen), 10_000):
            result = await tx.execute(
                select(entities.c.cid).where(entities.c.cid.in_(unseen[start : start + 10_000]))
            )
            found.update(bytes(cid) for cid in result.scalars())
        if found:
            tx.on_commit(lambda: self._confirmed.update(found))
        return frozenset(e for e in ids if self._is_known_cid(e.cid) or e.cid in found)
