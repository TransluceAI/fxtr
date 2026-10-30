"""Abstract interfaces for fxtr arenas and storage backends.

An "arena" represents a lease on a set of entities - a promise that certain data will remain
accessible. Arenas are the primary way user code interacts with fxtr storage.

Key abstractions:
- ReadableArena: Can load entities and track what's accessible
- CopyTargetArena: Can also copy entities from other arenas
- ReadWriteArena: Can also store new entities synchronously
- PersistentPinningBackend: Persistent storage with named pins for retrieval
- StreamSink: Experimental streaming output interface
- ObservableStore: Store that supports querying state and backreferences
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any, Collection, Literal, override

from fxtr2.data import entity_core
from fxtr2.data.entity_core import FxtrCbor, FxtrID


class ReadableArena(abc.ABC):
    """An arena of entities that can be read from.

    Arenas manage the mapping between FxtrIDs and their data. Whenever you receive a FxtrID from
    an arena, the arena promises that it will be able to load the entity data for that ID as long
    as the arena is active, unless you explicitly drop the ID.

    Arenas make it possible for fxtr to track all of the entities it is responsible for, and to
    clean them up when they are no longer needed. Arenas also enable storage backends to be layered
    on top of each other.
    """

    @abc.abstractmethod
    def is_tracked(self, node_id: FxtrID) -> bool:
        """Check if a node is tracked in this arena.

        A node is tracked if it is currently owned by this arena, such that this arena will keep
        it alive and ensure that it is accessible until explicitly abandoned.

        Returns:
            True if the node is tracked in this arena, False otherwise.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def load(self, node_id: FxtrID) -> FxtrCbor:
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
    def unwrap_provenance(self, node_id: FxtrID) -> FxtrID | None:
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
    def c_node_to_context_data(self, c_node_id: FxtrID) -> FxtrID:
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
    def p_node_to_pathroot(self, p_node_id: FxtrID) -> FxtrID:
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
    def step_back_p_ctx(self, p_node_id: FxtrID) -> FxtrID | None:
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
    def abandon(self, node_id: FxtrID):
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
    def copy_to_new_arena(self, node_ids: Collection[FxtrID]) -> ReadableArena:
        """Copy the given nodes to a new arena.

        This method creates a new arena that contains a subset of nodes that are tracked in this
        one. The new arena will guarantee that the given nodes remain accessible until the new arena
        is closed, even if the original arena is closed or nodes in it are abandoned.

        Args:
            node_ids: The IDs of the nodes to copy. Must be tracked in this arena.

        Returns:
            A new arena that tracks the given nodes.
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


class CopyTargetArena(ReadableArena):
    """An arena that can be used as a target for copying entities to."""

    @abc.abstractmethod
    async def copy_from_arena(self, entity_id: FxtrID, source: ReadableArena):
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


class ReadWriteArena(CopyTargetArena):
    """An arena of entities that can be read from and written to.

    Arenas manage the mapping between FxtrIDs and their data. Whenever you receive a FxtrID from
    an arena, the arena promises that it will be able to load the entity data for that ID as long
    as the arena is active, unless you explicitly drop the ID.

    Arenas make it possible for fxtr to track all of the entities it is responsible for, and to
    clean them up when they are no longer needed. Arenas also enable storage backends to be layered
    on top of each other.
    """

    @abc.abstractmethod
    def store(self, entity: FxtrCbor) -> FxtrID:
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
    def track_builtin(self, node_id: FxtrID) -> FxtrID:
        """Track a "." or "!" node.

        This method makes sure that the given builtin node is tracked by the arena.

        Args:
            node_id: The ID of the builtin node to track.

        Returns:
            Its input argument, for convenience.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    def create_c_node(self, context_data: FxtrCbor) -> FxtrID:
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
    def create_p_node(self, pathroot_data: FxtrCbor) -> FxtrID:
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
    def create_u_node(self, wrapped_id: FxtrID) -> FxtrID:
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


class PersistentPinningBackend(abc.ABC):
    """A persistent storage backend that allows pinning entities by string key.

    This backend allows entities to be published to a string key, making them available
    persistently and accessible outside their source arena.

    To simplify implementation of backends, a persistent pinning backend does NOT need to allow
    direct storage of entities via an arena. Instead, most users should create the entities in a
    full ReadWriteArena and then copy them into the persistent store. (This also means that
    persistent backend arenas do not require saving entities to be synchronous, as normal
    ReadWriteArenas do.)
    """

    @abc.abstractmethod
    async def pin_entity_from_arena(
        self, key: str, entity_id: FxtrID, source: ReadableArena, *, overwrite: bool = False
    ):
        """Pin an entity from an arena into this backend.

        This method pins the given entity, possibly copying it and all of its transitive
        dependencies from the source arena into the storage backend.

        Args:
            key: The name to pin the entity under.
            entity_id: The ID of the entity to pin.
            source: The arena containing the entity.
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
    async def retrieve_pinned(self, key: str) -> tuple[entity_core.FxtrID, ReadableArena]:
        """Get a published entity by key.

        This method returns the ID entity published at the given key, along with an arena that can
        be used to load the entity.

        Args:
            key: The key to use for the persistent ID.

        Returns:
            The entity ID of the pinned entity, and an arena that can be used to load the entity.
            The entity will be tracked in the returned arena.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def list_pins(self, prefix: str = "") -> dict[str, entity_core.FxtrID]:
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


class StreamSink(abc.ABC):
    """Internal representation of the output side of a stream.

    Stream sinks can be implemented by various backends to support receiving streamed items from
    the user. They take an explicit input arena that they should copy values from when they are
    emitted.
    """

    @abc.abstractmethod
    def emit(self, *items: entity_core.FxtrCbor, from_arena: ReadableArena | None = None) -> None:
        """Emit items to the stream.

        Args:
            items: The items to emit.
            from_arena: The arena to copy values from. Required if the items contain entities.
        """
        raise NotImplementedError("Subclasses should override")

    @abc.abstractmethod
    def finish_with_result(self, result: entity_core.FxtrID, from_arena: ReadableArena) -> None:
        """Close the stream with a final result.

        After a stream is closed, it is no longer valid to emit any more items to the stream.
        Additionally, all substreams of this stream must be finished first before this stream can
        be finished.

        Args:
            result: The ID of the final result to use as the result of the stream, replacing all previous
                streamed outputs. Usually an entity or set of entities that combine the results
                that were previously streamed.
            from_arena: The arena where the result is stored.
        """
        raise NotImplementedError("Subclasses should override")

    @abc.abstractmethod
    def make_substream_sink(
        self, stream_key: entity_core.FxtrID | None = None, from_arena: ReadableArena | None = None
    ) -> StreamSink:
        """Create a substream sink inside this stream sink.

        Streams can contain other streams in addition to regular CBOR items. These streams are
        treated as children of the parent stream, and when the child stream finishes, it is treated
        similarly to emitting the final result of the child stream.

        Substreams must be finished before the parent stream can be finished.

        Args:
            stream_key: An optional ID to associate this substream with.
            from_arena: The arena to copy values from. Required if the stream key contains entities.

        Returns:
            A new substream.
        """
        raise NotImplementedError("Subclasses should override")

    @abc.abstractmethod
    def is_finished(self) -> bool:
        """Check if the stream is finished."""
        raise NotImplementedError("Subclasses should override")


@dataclasses.dataclass(frozen=True)
class EntityBackref:
    """Another entity that refers to this entity."""

    referrer: FxtrID
    kind: Literal["strong_ref", "weak_ref", "unwrap", "context_data"]


@dataclasses.dataclass(frozen=True)
class OtherBackref:
    """A non-entity source that refers to this entity."""

    kind: str
    data: Any


class ObservableStore(abc.ABC):
    """A store whose state can be queried."""

    @abc.abstractmethod
    async def try_lookup(self, node_id: FxtrID) -> ReadableArena | None:
        """Try to look up an entity in the store.

        If this entity is in the store, this method returns an arena that can be used to load the
        entity. If not, this method returns None.

        Args:
            node_id: The ID of the entity to look up.

        Returns:
            An arena that can be used to load the entity, or None if the entity is not in the store.
        """
        raise NotImplementedError("Not implemented yet")

    @abc.abstractmethod
    async def backrefs(self, node_id: FxtrID) -> list[EntityBackref | OtherBackref]:
        """Get backrefs to a particular entity.

        Returns a list of other items known to the store that refer to, depend on, or contain the
        given entity.

        Args:
            node_id: The ID of the entity to get backrefs for.

        Returns:
            A list of other items known to the store that refer to, depend on, or contain the
            given entity.
        """
        raise NotImplementedError("Not implemented yet")
