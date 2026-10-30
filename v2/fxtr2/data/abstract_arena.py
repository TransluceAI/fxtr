"""Abstract interfaces for fxtr arenas and storage backends.

An "arena" represents a lease on a set of entities - a promise that certain data will remain
accessible. Arenas are the primary way user code interacts with fxtr storage.

All arena operations are authorized by a ``Lease[B]`` passed to each method. Every arena carries
a ``token`` property identifying its lifetime; the brand ``B`` is a phantom type parameter that
lets the typechecker distinguish between leases of different lifetimes. See
``plans/phase3/stage8b_leased_memory_store/overview.md`` for the lease-backed design.

Calling convention: each lease argument appears immediately after the thing it authorizes.
Single-target methods put the arena's own lease first (after ``self``). Cross-arena methods pair
the source arena with its own lease (``..., source, src_lease, ...``). Leases are consumed on
each call — callers pass ``lease.clone()`` if they need to retain their own copy.

Key abstractions:
- ReadableArena[B]: Can load entities and track what's accessible
- CopyTargetArena[B]: Can also copy entities from other arenas
- ReadWriteArena[B]: Can also store new entities synchronously and lazily attach from foreign arenas
- PersistentPinningBackend: Persistent storage with named pins for retrieval
- DeepFetchable[B]: Store that supports deep-fetching an entity (and its transitive deps) into
  an arena sharing the caller's lifetime brand
"""

from __future__ import annotations

import abc
import dataclasses
import enum
from typing import Collection, override

from fxtr2.data.entity_core import FxtrCbor, FxtrID
from fxtr2.leases.lease_bridges import LocalBridge
from fxtr2.leases.lease_types import Grant, Lease, LifetimeBrand, LifetimeToken


class DepKind(enum.Enum):
    """Kind of dependency relationship between entities."""

    STRONG_REF = "strong_ref"
    WEAK_REF = "weak_ref"
    CTX_NODE = "ctx_node"
    BACKSTEP = "backstep"
    UNWRAPS_TO = "unwraps_to"


@dataclasses.dataclass(frozen=True)
class FxtrEntityFlatData:
    """Flat representation of an entity's data and dependencies.

    For d/n nodes: entity_bytes is the raw CBOR bytes.
    For derived nodes (c/p/u/./!): entity_bytes is None.
    deps is the full dependency list, same format as the removed
    fetch_data_and_deps_from_arena.
    """

    entity_id: FxtrID
    entity_bytes: bytes | None
    deps: list[tuple[FxtrID, DepKind]]


class LeaseGrantMismatchError(RuntimeError):
    """Raised when a lease's grant does not match an arena's expected grant."""


def validate_lease_grant[B: LifetimeBrand](expected: Grant[B], lease: Lease[B]) -> None:
    """Check that ``lease.grant == expected``, raising ``LeaseGrantMismatchError`` otherwise.

    Does not consume the lease or check liveness; callers who also require the lease to still be
    active should call ``lease.ensure_active()`` in addition.
    """
    if lease.grant != expected:
        raise LeaseGrantMismatchError(
            f"Lease grant {lease.grant!r} does not match expected grant {expected!r}"
        )


class FlatEntitySource[B: LifetimeBrand](abc.ABC):
    """Minimal interface for reading flat entity data.

    This is the narrow contract needed by entity import/copy operations.
    It provides entity lookup and flat data loading without requiring
    the full provenance navigation that ReadableArena includes.

    ReadableArena extends this, so any full arena also satisfies this
    interface. TransferEntitySource implements only this interface.
    """

    @property
    @abc.abstractmethod
    def token(self) -> LifetimeToken[B]:
        """The lifetime token that gates this source's operations."""
        raise NotImplementedError

    @abc.abstractmethod
    def is_tracked(self, lease: Lease[B], node_id: FxtrID) -> bool:
        """Check if a node is available in this source."""
        raise NotImplementedError

    @abc.abstractmethod
    async def load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData:
        """Load flat entity data (raw bytes + dependency list).

        Has the same loading obligations as ReadableArena.load_flat; see that for details.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def copy_to_new_arena(
        self, lease: Lease[B], node_ids: Collection[FxtrID]
    ) -> FlatEntitySource[B]:
        """Create a new source that keeps the given nodes accessible.

        The returned source shares the same lifetime token (and brand) as this one — this is
        purely a cheap python-side duplication of tracking state, not a new lifetime. No lease
        is returned; callers keep using clones of their existing lease to access the new source.
        """
        raise NotImplementedError

    def close(self):
        """Release resources held by this source."""


class ReadableArena[B: LifetimeBrand](FlatEntitySource[B]):
    """An arena of entities that can be read from.

    Arenas manage the mapping between FxtrIDs and their data. Whenever you receive a FxtrID from
    an arena, the arena promises that it will be able to load the entity data for that ID as long
    as the arena is active, unless you explicitly drop the ID.

    Arenas make it possible for fxtr to track all of the entities it is responsible for, and to
    clean them up when they are no longer needed. Arenas also enable storage backends to be layered
    on top of each other.
    """

    @abc.abstractmethod
    def is_tracked(self, lease: Lease[B], node_id: FxtrID) -> bool:
        """Check if a node is tracked in this arena.

        A node is tracked if it is currently owned by this arena, such that this arena will keep
        it alive and ensure that it is accessible until explicitly abandoned.

        Returns:
            True if the node is tracked in this arena, False otherwise.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def load(self, lease: Lease[B], node_id: FxtrID) -> FxtrCbor:
        """Load an entity from the fxtr graph.

        This method loads an entity from the current arena and returns it as a raw entity. The
        entity must be tracked in the arena before it can be loaded.

        If an entity is successfully loaded, every ID that is referenced by the entity will be
        tracked and accessible in the arena until explicitly abandoned.

        This method is async because it may need to load entity data from disk or from another
        remote source.

        This method does NOT implicitly use provenance parents; this must be done by the caller
        if desired. Any provenance parents will be ignored.

        Args:
            node_id: The canonical identifier for the entity to load.

        Returns:
            The raw entity.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def unwrap_provenance(self, lease: Lease[B], node_id: FxtrID) -> FxtrID | None:
        """Unwrap a provenance node.

        This method unwraps a provenance node and returns the data node ID it is attached to, if
        the provenance node is known to the store.

        Note that this method must be synchronous, but assumes that the ID is already known to the
        store.

        Args:
            node_id: The provenance node to unwrap.

        Returns:
            The node ID it unwraps to, or None if the node is not a provenance node.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def c_node_to_context_data(self, lease: Lease[B], c_node_id: FxtrID) -> FxtrID:
        """Unstructure a context provenance node.

        This method destructs a context provenance node and returns the data node ID it was built
        from.

        Args:
            node_id: The context provenance node to destruct.

        Returns:
            The data node ID it was built from.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def p_node_to_pathroot(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID:
        """Unstructure a path provenance node.

        This method unstructures a path provenance node and returns the pathroot node ID it was built
        from. (This is _not_ the same as the result of unwrapping it.)

        Args:
            node_id: The path provenance node to destruct.

        Returns:
            The data node ID for the pathroot node it was built from.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def step_back_p_ctx(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID | None:
        """Step back along a path provenance context node.

        This method steps back from the ID of the "$fxtr.ctx" entry of a p-node to the ID of the
        contentful p-node itself. Note that this is always a one-to-one mapping because p-nodes
        are identified based on their parents, and the root entry is always contentful rather than
        being a "$fxtr.ctx" entry.

        In principle this is a deterministic operation on the ID, but running it on the arena
        ensures that the relevant node is tracked and can be loaded if needed.

        Args:
            p_node_id: The ID of the "$fxtr.ctx" entry of a p-node to step back from.

        Returns:
            The ID of the p-node that contains this "$fxtr.ctx" entry, or None if this is not the
            "$fxtr.ctx" entry of a p-node.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData:
        """Load flat entity data without deserializing CBOR.

        All returned IDs in the result.deps will be tracked in the arena as a side effect, maintaining
        the invariant that all IDs returned by an arena method are always loadable in that arena.
        Returns raw bytes (for d/n) and the full dependency list.

        Args:
            node_id: The canonical identifier for the entity to load.

        Returns:
            FxtrEntityFlatData containing raw bytes and dependencies.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def abandon(self, lease: Lease[B], node_id: FxtrID):
        """Abandon an entity from the arena.

        This method abandons an entity from the arena. The entity will no longer be tracked and
        will not be retrievable from this arena unless it becomes tracked again (e.g. by acquiring
        a new reference to it when loading another entity).

        It is the caller's responsibility to avoid abandoning entities that are still needed by
        other users of the arena. We suggest only using `abandon` when an arena is being accessed
        by a single object or task, and that owner knows it will not need the data again.

        Args:
            node_id: The entity to abandon.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def copy_to_new_arena(self, lease: Lease[B], node_ids: Collection[FxtrID]) -> ReadableArena[B]:
        """Copy the given nodes to a new arena.

        This method creates a new arena that contains a subset of nodes that are tracked in this
        one. The returned arena shares the same lifetime token (and brand) as this one — this is
        purely a cheap python-side duplication of tracking state, not a new lifetime. Callers
        continue to use clones of their existing lease to access the new arena; no lease is
        returned.

        Args:
            node_ids: The IDs of the nodes to copy. Must be tracked in this arena.

        Returns:
            A new arena tracking the given nodes under the same lifetime token as this arena.
        """
        raise NotImplementedError("Not implemented yet")

    def close(self):
        """Close the arena.

        This method closes the arena. Any entities tracked by this arena will be abandoned and will
        no longer be retrievable via this arena. The backing storage is free to garbage collect any entities that
        are only reachable from this arena and not referenced elsewhere.

        It is an error to call any arena method on a closed arena.
        """
        raise NotImplementedError("Not implemented yet")


class CopyTargetArena[B: LifetimeBrand](ReadableArena[B]):
    """An arena that can be used as a target for copying entities to."""

    @abc.abstractmethod
    async def copy_from_arena[
        B2: LifetimeBrand
    ](self, lease: Lease[B], source: ReadableArena[B2], src_lease: Lease[B2], entity_id: FxtrID,):
        """Copy an entity from another arena into this arena.

        This method recursively copies the entity and all of its transitive dependencies from the
        source arena into this arena. Only transfers entities that are not already present in this
        arena.

        After `copy_from_arena` is called, the provided `entity_id` will be retrievable in this
        arena. Transitive dependencies may _not_ be retrievable in this arena, but will become
        tracked when passing `entity_id` to `load`.

        Args:
            entity_id: The ID of the entity to export.
        """
        raise NotImplementedError("Not implemented yet")


class ReadWriteArena[B: LifetimeBrand](CopyTargetArena[B]):
    """An arena of entities that can be read from and written to.

    Arenas manage the mapping between FxtrIDs and their data. Whenever you receive a FxtrID from
    an arena, the arena promises that it will be able to load the entity data for that ID as long
    as the arena is active, unless you explicitly drop the ID.

    Arenas make it possible for fxtr to track all of the entities it is responsible for, and to
    clean them up when they are no longer needed. Arenas also enable storage backends to be layered
    on top of each other.
    """

    @abc.abstractmethod
    def store(self, lease: Lease[B], entity: FxtrCbor) -> FxtrID:
        """Store an entity in the fxtr graph.

        This method stores an entity into the current context and returns the canonical identifier
        for that entity. When an entity is stored in a given context, fxtr promises to be able to
        retrieve it as long as that context is active. If the given node is a provenance node, it
        also promises to be able to unwrap it.

        When storing an entity, all IDs that are referenced by the entity must already be tracked
        in the arena.

        The `store` method also precomputes and saves all other information needed to track
        forward and backward links between entities, as well as the "unwrap" operation on
        provenance nodes.

        This method is synchronous. As soon as it completes, the entity is considered to be tracked
        and accessible in the arena, and remains so until explicitly abandoned. Note that this may
        not immediately flush the entity to disk or other storage until the arena is closed; it
        may simply be held in memory.

        This method does NOT implicitly use provenance parents; this must be done by the caller
        if desired. Any provenance parents will be ignored.

        Args:
            entity: The entity to store.

        Returns:
            The canonical identifier for the stored entity.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def track_builtin(self, lease: Lease[B], node_id: FxtrID) -> FxtrID:
        """Track a "." or "!" node.

        This method makes sure that the given builtin node is tracked by the arena.

        Args:
            node_id: The ID of the builtin node to track.

        Returns:
            Its input argument, for convenience.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def create_c_node(self, lease: Lease[B], context_data: FxtrCbor) -> FxtrID:
        """Create a context provenance node from its context data

        This method lifts data for a context node into a context provenance node (cXXXXXX). The data
        node must have a "value" field. The returned node will be a "c"-node that unwraps to
        ".".

        Args:
            context_data: The data for the context node to create. Must be data for a d-node and
                have a "value" field that contains the node ID that this context node unwraps to.

        Returns:
            The context provenance node ID, suitable for use as the "$fxtr.ctx" field for new
            entities.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def create_p_node(self, lease: Lease[B], pathroot_data: FxtrCbor) -> FxtrID:
        """Create a path provenance node from its pathroot data.

        This method lifts the data for a pathroot node into a path provenance node (pXXXXXX)
        pointing at that data. The returned node will be a "p"-node that unwraps to the value of
        the "root" field.

        Args:
            pathroot_data: The data for the pathroot node to create. Must be data for a d-node and
                have a "value" field.

        Returns:
            The path provenance node ID.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def create_u_node(self, lease: Lease[B], wrapped_id: FxtrID) -> FxtrID:
        """Create an untracked provenance node from a data node.

        This method lifts a data node (dXXXXXX) into an untracked provenance node (uXXXXXX). The
        result will be a "u"-node that unwraps to the wrapped ID and does not have any additional
        provenance information attached.

        Note that destructing a u node is the same as unwrapping it normally, so there is no
        separate unstructure operation.

        Args:
            wrapped_id: The ID of the data node to wrap.

        Returns:
            The untracked provenance node ID.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def lazily_attach_from_arena[
        B_Y: LifetimeBrand
    ](
        self,
        lease: Lease[B],
        node_ids: FxtrID | Collection[FxtrID],
        source: ReadableArena[B_Y],
        bridge: LocalBridge,
    ) -> None:
        """Lazily attach one or more entities from a source arena.

        Registers ``source`` and ``bridge`` for the supplied IDs so that subsequent loads
        of those nodes can fetch data from ``source``. The bridge — a ``LocalBridge``
        from :mod:`fxtr2.leases.lease_bridges` — produces a fresh ``Lease[B_Y]`` per
        load: ``IndependentLocalBridge`` clones its stored at-rest lease;
        ``DependentLocalBridge`` runs its converter on cloned input leases plus a
        per-call ``base = Lease[B]`` produced by the implementation. Whichever variant
        is used, every at-rest lease in ``bridge`` is taken on first attach for a
        ``(self.token, source.token)`` pair; subsequent attaches for the same pair
        discard the new bridge and reuse the stored record.

        Accepts a single ``FxtrID`` (legacy single-attach) or a ``Collection[FxtrID]``.
        When a collection is supplied, the implementation does *one*
        ``source.copy_to_new_arena`` fork covering every ID, instead of one fork per ID
        — a meaningful saving when ``source`` is e.g. a ``CapIRArena`` whose
        ``copy_to_new_arena`` is a wire round-trip.

        See :mod:`fxtr2.leases.lease_bridges` for the four-variant bridge taxonomy and
        :doc:`lifetime_conversion_mechanics` for guidance on which variant to choose
        for a given ``(self.token, source.token)`` relationship.

        Args:
            lease: A lease for this arena's token.
            node_ids: The entity (or entities) to attach.
            source: The source arena that will be used to load the entities' data lazily.
            bridge: A :class:`LocalBridge` that mints a fresh ``Lease[B_Y]`` per load.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    @override
    def close(self):
        """Close the arena.

        This method closes the arena. Any entities tracked by this arena will be abandoned and will
        no longer be retrievable via this arena, and it will no longer be possible to store new
        entities in this arena. The backing storage is free to garbage collect any entities that
        are only reachable from this arena and not referenced elsewhere.

        It is an error to call any arena method on a closed arena.
        """
        raise NotImplementedError("Not implemented yet")


class PersistentPinningBackend[B: LifetimeBrand](abc.ABC):
    """A persistent storage backend that allows pinning entities by string key.

    This backend allows entities to be published to a string key, making them available
    persistently and accessible outside their source arena.

    Parameterized by a lifetime brand ``B`` (exposed via ``token``). Retrieval methods return
    arenas branded the same way; callers obtain a ``Lease[B]`` for those arenas via a concrete
    helper (for example ``backend.get_lease()``), not by passing a lease into every backend
    method. The backend's owner manages the token externally (typically via an
    ``UnconditionalLocalLifetime`` held by a session / namespace object).

    To simplify implementation of backends, a persistent pinning backend does NOT need to allow
    direct storage of entities via an arena. Instead, most users should create the entities in a
    full ReadWriteArena and then copy them into the persistent store. (This also means that
    persistent backend arenas do not require saving entities to be synchronous, as normal
    ReadWriteArenas do.)
    """

    @property
    @abc.abstractmethod
    def token(self) -> LifetimeToken[B]:
        """The lifetime token that gates this backend's operations."""
        raise NotImplementedError

    @abc.abstractmethod
    async def pin_entity_from_arena[
        B2: LifetimeBrand
    ](
        self,
        key: str,
        entity_id: FxtrID,
        source: ReadableArena[B2],
        src_lease: Lease[B2],
        *,
        overwrite: bool = False,
    ):
        """Pin an entity from an arena into this backend.

        This method pins the given entity, possibly copying it and all of its transitive
        dependencies from the source arena into the storage backend.

        Args:
            key: The name to pin the entity under.
            entity_id: The ID of the entity to pin.
            source: The arena containing the entity.
            src_lease: A lease for ``source``'s token, consumed by the call.
            overwrite: If False (the default), raises an error if a pin with the given key
                already exists. If True, replaces the existing pin with the new entity.

        Raises:
            KeyError: If a pin with the given key already exists and overwrite is False.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def unpin_entity(self, key: str):
        """Unpin a published entity, removing it from the persistent store.

        This method unpins the entity published at the given key. If the entity is no longer
        reachable from any pinned entity, it may be removed from the persistent store.

        Args:
            key: The key to use for the persistent ID.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, ReadableArena[B]]:
        """Get a published entity by key.

        This method returns the ID of the entity published at the given key, along with an arena
        that can be used to load the entity. The returned arena shares this backend's lifetime
        brand; callers obtain a ``Lease[B]`` for it via a concrete helper such as
        ``backend.get_lease()``.

        Args:
            key: The key to use for the persistent ID.

        Returns:
            A tuple of (entity_id, arena). The entity will be tracked in the returned arena.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List pins in the backend matching the given prefix.

        This method returns all pins whose keys start with the given prefix. It does NOT produce
        an arena or guarantee that the pinned entities are loadable - it only returns the mapping
        from pin names to entity IDs.

        Args:
            prefix: Only return pins whose keys start with this prefix. Empty string (default)
                returns all pins.

        Returns:
            A dict mapping pin name to entity ID for all matching pins.
        """
        raise NotImplementedError("Not implemented yet")


class DeepFetchable[B: LifetimeBrand](abc.ABC):
    """Protocol for *external* deep-fetch sources registered via
    ``FxtrScope.register_deep_fetch_source``.

    On success, ``try_lookup`` returns a ``ReadableArena[B]``; callers use it with leases from
    ``backend.get_lease()`` (or the session) as usual.

    The global ``MemoryStore`` is *not* a ``DeepFetchable`` — it can serve many tokens and uses
    ``try_lookup_with_lease(lease, node_id)`` to select the caller's brand.
    """

    @abc.abstractmethod
    async def try_lookup(self, node_id: FxtrID) -> ReadableArena[B] | None:
        """Try to look up an entity in this deep-fetch source.

        If the entity is available from this source, returns an arena tracking it; otherwise
        None. Callers use the returned arena with ``Lease[B]`` from a concrete
        ``get_lease()``-style hook on the same backend that produced the source.
        """
        raise NotImplementedError("Not implemented yet")
