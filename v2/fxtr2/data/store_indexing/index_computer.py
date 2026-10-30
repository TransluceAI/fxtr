"""IndexComputer: bridge between store (FxtrIDs + SQL) and indexer logic (loaded entities + scopes).

The IndexComputer caches loaded indexer objects in a persistent scope so that repeated
pin operations don't re-deserialize the same indexers. It is used as an async context
manager tied to the lifetime of a SQLiteFxtrSession.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from anyio import AsyncContextManagerMixin

from fxtr2.data.abstract_arena import ReadableArena
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.leases.lease_bridges import IndependentLocalBridge
from fxtr2.leases.lease_types import Lease, LifetimeBrand

from .indexers import WellKnownBuiltinIndexer
from .indexing_types import IndexerColumnType, IndexerColumnValue


class IndexComputer(AsyncContextManagerMixin):
    """Helper that runs indexer logic on entities, caching loaded indexers across calls.

    Must be used as an async context manager. While entered, maintains a persistent
    isolated scope that holds deserialized indexer objects. This avoids re-loading
    indexer entities on every pin operation.

    Provides two operations used by the store:

    - ``get_indexer_types``: Load an indexer and return its column schema.
    - ``compute_indexer_entries``: Run indexers on entities and return (key_tuple, value) results.

    The provided arenas are not modified — ``lazily_attach_from_arena`` copies data
    into the IndexComputer's own internal scopes rather than mutating the source arena.
    The caller passes leases for each arena; the IndexComputer threads them to the
    store's safe attach API (default converter — Z == Y — since the leases are taken
    directly from the caller's own session-branded arena).
    """

    def __init__(self) -> None:
        self._scope: FxtrScope | None = None
        self._cached_indexers: dict[FxtrID, WellKnownBuiltinIndexer] = {}

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncIterator[IndexComputer]:
        async with FxtrScope(isolated=True, name="index_computer_persistent") as scope:
            self._scope = scope
            try:
                yield self
            finally:
                self._scope = None
                self._cached_indexers.clear()

    @property
    def cached_indexers(self) -> frozenset[FxtrID]:
        """The set of indexer FxtrIDs that are already loaded and cached."""
        return frozenset(self._cached_indexers.keys())

    async def _ensure_indexers_loaded[
        B_Idx: LifetimeBrand
    ](
        self,
        indexers: Sequence[FxtrID],
        indexer_arena: ReadableArena[B_Idx] | None,
        indexer_lease: Lease[B_Idx] | None,
    ) -> dict[FxtrID, WellKnownBuiltinIndexer]:
        """Ensure all indexers are loaded, returning the full map for the requested IDs.

        New indexers (not in cache) are attached from ``indexer_arena`` into the persistent
        scope and loaded. Already-cached indexers are returned from cache.

        Args:
            indexers: FxtrIDs of the indexers needed.
            indexer_arena: Arena containing any new (uncached) indexers, or None if all
                are already cached.
            indexer_lease: A lease for ``indexer_arena``'s token, consumed by the call.
                Must be provided iff ``indexer_arena`` is. If any new indexers need to be
                loaded, the lease is cloned once per new indexer and used to thread a
                ``lazily_attach_from_arena`` call; otherwise the lease is discarded.

        Returns:
            Dict mapping each requested indexer FxtrID to its loaded object.
        """
        assert self._scope is not None
        result: dict[FxtrID, WellKnownBuiltinIndexer] = {}

        # Take ownership of the indexer lease (if any) up front so we can clone as needed.
        taken_indexer_lease: Lease[B_Idx] | None = None
        if indexer_lease is not None:
            taken_indexer_lease = indexer_lease.take()
        try:
            for iid in indexers:
                if iid in self._cached_indexers:
                    result[iid] = self._cached_indexers[iid]
                else:
                    assert (
                        indexer_arena is not None
                    ), f"Indexer {iid} not cached but no indexer_arena provided"
                    assert (
                        taken_indexer_lease is not None
                    ), f"Indexer {iid} not cached but no indexer_lease provided"
                    # Case 1 attach (Y outlives X): an IndependentLocalBridge
                    # holds a Lease[Y] cloned from ``taken_indexer_lease``.
                    self._scope._main_arena.lazily_attach_from_arena(
                        self._scope._current_auth_lease().clone(),
                        iid,
                        indexer_arena,
                        IndependentLocalBridge(lease=taken_indexer_lease.clone()),
                    )
                    ref = EntityRef(iid, WellKnownBuiltinIndexer)  # type: ignore[arg-type]
                    loaded = await ref.load(scope=self._scope)
                    self._cached_indexers[iid] = loaded
                    result[iid] = loaded
        finally:
            if taken_indexer_lease is not None:
                taken_indexer_lease.discard()

        return result

    async def get_indexer_types[
        B_Idx: LifetimeBrand
    ](
        self,
        indexer: FxtrID,
        arena: ReadableArena[B_Idx] | None = None,
        arena_lease: Lease[B_Idx] | None = None,
    ) -> tuple[Sequence[IndexerColumnType], IndexerColumnType]:
        """Load an indexer entity and return its column schema.

        If the indexer is already cached, the arena is not needed.

        The provided arena is not modified.

        Args:
            indexer: The FxtrID of the indexer entity.
            arena: An arena containing the indexer entity (only needed if not cached).
            arena_lease: A lease for ``arena``'s token, consumed by the call. Required
                iff ``arena`` is provided.

        Returns:
            A tuple of (key_types, value_type).
        """
        loaded_map = await self._ensure_indexers_loaded([indexer], arena, arena_lease)
        loaded = loaded_map[indexer]
        return (loaded.index_key_types(), loaded.index_value_type())

    async def compute_indexer_entries[
        B_Ent: LifetimeBrand, B_Idx: LifetimeBrand
    ](
        self,
        entities: Sequence[FxtrID],
        indexers: Sequence[FxtrID],
        entity_arena: ReadableArena[B_Ent],
        entity_lease: Lease[B_Ent],
        indexer_arena: ReadableArena[B_Idx] | None = None,
        indexer_lease: Lease[B_Idx] | None = None,
    ) -> dict[
        tuple[FxtrID, FxtrID],
        Sequence[tuple[tuple[IndexerColumnValue, ...], IndexerColumnValue]],
    ]:
        """Run every indexer on every entity and return the results.

        The provided arenas are not modified.

        Args:
            entities: FxtrIDs of the entities to index.
            indexers: FxtrIDs of the indexer entities to apply.
            entity_arena: An arena containing the entities and their transitive deps.
            entity_lease: A lease for ``entity_arena``'s token, consumed by the call.
            indexer_arena: An arena containing any new (uncached) indexer entities, or
                None if all indexers are already cached.
            indexer_lease: A lease for ``indexer_arena``'s token, consumed by the call.
                Required iff ``indexer_arena`` is provided.

        Returns:
            A dict mapping ``(indexer_id, entity_id)`` to a sequence of ``(key_tuple, value)``
            pairs. Every (indexer, entity) combination is present, even if the result is empty.
        """
        assert self._scope is not None
        results: dict[
            tuple[FxtrID, FxtrID],
            Sequence[tuple[tuple[IndexerColumnValue, ...], IndexerColumnValue]],
        ] = {}

        # Load/cache all indexers in the persistent scope
        loaded_indexers = await self._ensure_indexers_loaded(indexers, indexer_arena, indexer_lease)

        # Take ownership of the entity lease so we can clone it for each attach below.
        with entity_lease.take() as entity_lease:
            # Use the persistent scope as parent so child scope can access cached indexers
            with self._scope.as_active_scope():
                # Single child scope for the entire compute call
                async with FxtrScope(name="index_computer_run") as child:
                    # Attach all entities from the entity arena. Case 1
                    # bridge (Y outlives the child scope, since the caller's
                    # entity_lease keeps Y alive across this whole call).
                    for eid in entities:
                        child._main_arena.lazily_attach_from_arena(
                            child._current_auth_lease().clone(),
                            eid,
                            entity_arena,
                            IndependentLocalBridge(lease=entity_lease.clone()),
                        )

                    # Run each indexer on each entity
                    for iid, indexer_obj in loaded_indexers.items():
                        for eid in entities:
                            entries = await indexer_obj.unpack_entity(eid, child)
                            results[(iid, eid)] = entries

        return results
