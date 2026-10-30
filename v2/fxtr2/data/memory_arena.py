"""In-memory implementation of fxtr arenas.

This module provides the primary in-memory storage backend for fxtr:

- MemoryStore: The backing store that holds actual entity data and manages node state.
  Multiple arenas can share a single store, efficiently reusing cached data.

- MemoryArena: A "view" into a MemoryStore with a tracked liveset of accessible entities.
  Implements the full ReadWriteArena interface.

Node state types (internal to MemoryStore):
- StoredNode: A d/n node with actual CBOR bytes stored
- LazyStoredNode: A d/n node that can be reconstructed from a wrapping provenance node
- DerivedNode: A contentless node (., !, c, p, u) computed on demand from dependencies
- InheritedNode: A placeholder for data inherited from another arena (lazy loading)

The store uses reference counting for garbage collection. Cycles are impossible because
entities are content-addressed and immutable - an entity can only reference entities
that existed before it was created.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import logging
import os
import pathlib
import tempfile
import traceback
import typing
import weakref
from typing import Any, Callable, Collection, Concatenate, override

import anyio
import cbor2
import lmdb

from fxtr2.data import entity_core
from fxtr2.data.abstract_arena import (
    EntityBackref,
    ObservableStore,
    OtherBackref,
    ReadableArena,
    ReadWriteArena,
    StreamSink,
)
from fxtr2.data.arena_util import (
    DepKind,
    fetch_data_and_deps_from_arena,
    visit_and_project_ids,
    visit_and_project_ids_in_canonical_cbor_order,
    visit_ids,
)
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ArenaKey:
    """A key for an arena of entities."""

    ix: int


@dataclasses.dataclass
class StoredNode:
    """Tracking state for a "d" or "n" node that is stored in the fxtr graph.

    Attributes:
        my_id: The ID of the node.
        requires: List of node IDs that must be loadable for this node to be loadable. For a
            StoredNode, the set of requirements are exactly the set of FxtrID that appear in the
            stored CBOR form (weak or strong).
        strong_linked_refs: Set of child IDs that this node has a strong link to.
        unwraps_to: The node ID that this node unwraps to, if it is a "n" node. Should be tracked
            by the store.
        required_by: List of node IDs that require this one to exist to be loadable and are tracked
            in the store.
        wrapped_by: List of node IDs that wrap this one and are tracked in the store.
    """

    my_id: FxtrID
    requires: frozenset[FxtrID]
    strong_linked_refs: frozenset[FxtrID]
    unwraps_to: FxtrID | None
    required_by: set[FxtrID | ArenaKey] = dataclasses.field(default_factory=set)
    wrapped_by: set[FxtrID] = dataclasses.field(default_factory=set)

    def into_lazy(self) -> LazyStoredNode:
        """Convert this node to a lazy unwrap node.

        Does NOT free the data from the backing store; this should be done separately.
        """
        return LazyStoredNode(
            my_id=self.my_id,
            requires=self.requires,
            strong_linked_refs=self.strong_linked_refs,
            required_by=self.required_by,
            unwraps_to=self.unwraps_to,
            wrapped_by=self.wrapped_by,
        )


@dataclasses.dataclass
class LazyStoredNode:
    """Tracking state for a "d" or "n" node that is the result of unwrapping another node.

    Used to reduce storage by avoiding duplicating unnecessary data. Any of the nodes in the
    `wrapped_by` set can be used to reconstruct this node. However, before all of the nodes in the
    `wrapped_by` set are removed, this node must be converted to a `StoredNode`.

    Attributes:
        my_id: The ID of the node.
        requires: List of node IDs that must be loadable for this node to be loadable. Does NOT
            include the nodes in the `wrapped_by` set; while we do technically load those nodes
            to reconstruct this one, logically this node should not keep those nodes alive.
        strong_linked_refs: Set of child IDs that this node has a strong link to.
        required_by: List of node IDs that require this one to exist to be loadable and are tracked
            in the store.
        unwraps_to: The node ID that this node unwraps to, if it exists. Should be tracked
            by the store, or a builtin ("." or "!").
        wrapped_by: List of node IDs that wrap this one and are tracked in the store.
    """

    my_id: FxtrID
    requires: frozenset[FxtrID]
    strong_linked_refs: frozenset[FxtrID]
    unwraps_to: FxtrID | None
    required_by: set[FxtrID | ArenaKey] = dataclasses.field(default_factory=set)
    wrapped_by: set[FxtrID] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class DerivedNode:
    """Tracking state for a ".", "!", "p", "c", or "u" node has no own data.

    These nodes may depend on other nodes that are needed to reconstruct their data.

    Attributes:
        my_id: The ID of the node.
        ctx_node: The ID of the context node that this node depends on, if any. Set for "c" and "p"
            nodes.
        backstep_node: The ID of the node that this node was backstepped from, if any. Set for "p"
            nodes that are the "$fxtr.ctx" entry of a p-node.
        requires: List of node IDs that must be loadable for this node to be loadable.
        required_by: List of node IDs that require this one to exist to be loadable and are tracked
            in the store.
        unwraps_to: The node ID that this node unwraps to, if it exists. Should be tracked
            by the store, or a builtin ("." or "!").
    """

    my_id: FxtrID
    requires: frozenset[FxtrID]
    ctx_node: FxtrID | None = None
    backstep_node: FxtrID | None = None
    required_by: set[FxtrID | ArenaKey] = dataclasses.field(default_factory=set)
    unwraps_to: FxtrID | None = None
    wrapped_by: set[FxtrID] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class InheritedNode:
    """Tracking state for a node that is inherited from another arena.

    The existence of an InheritedNode in the store implies that the parent arena is responsible for
    keeping the referenced node alive, but we have not actually copied it into the
    current memory arena yet.

    An inherited node always maintains a reference to the node it unwraps to, in this memory store,
    so that we can freely manipulate the unwraps information in a consistent way across nodes. This
    means that the `unwraps_to` node must be in the _entity_states of this store; it may, however,
    be an InheritedNode itself.

    We maintain the following invariant: for a given parent arena, a node is tracked in that arena
    if and only if there is an InheritedNode for it in this store.

    Attributes:
        my_id: The ID of the node.
        inheritance_parent_arena: The arena that is responsible for loading this node.
        unwraps_to: The node ID that this node unwraps to, if it exists. Should be tracked
            by the store.
        required_by: List of node IDs that require this one to exist to be loadable and are tracked
            in the store.
        wrapped_by: List of node IDs that wrap this one and are tracked in the store.
    """

    my_id: FxtrID
    inheritance_parent_arena: ReadableArena
    unwraps_to: FxtrID | None = None
    required_by: set[FxtrID | ArenaKey] = dataclasses.field(default_factory=set)
    wrapped_by: set[FxtrID] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class MemoryArenaState:
    """Tracking state for an arena."""

    my_key: ArenaKey
    liveset: set[FxtrID]


class RepeatedEvent:
    """A rolling event that can be used to track dirty state."""

    def __init__(self):
        self._event = anyio.Event()

    def trigger(self):
        self._event.set()
        self._event = anyio.Event()

    async def wait(self):
        await self._event.wait()


def _triggers_debug_dirty[
    S: MemoryStore, **P, R
](fn: Callable[Concatenate[S, P], R],) -> Callable[Concatenate[S, P], R]:

    @functools.wraps(fn)
    def wrapper(self: S, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._debug_dirty.trigger()

    return wrapper


class InMemoryBytesBackingStore:
    """Simple in-memory store of entity data."""

    def __init__(self):
        self._data: dict[bytes, bytes] = {}
        self._counts: dict[bytes, int] = {}

    def save_and_increment(self, key: bytes, value: bytes):
        self._data[key] = value
        self._counts[key] = self._counts.get(key, 0) + 1

    def get(self, key: bytes) -> memoryview:
        return memoryview(self._data[key])

    def free(self, key: bytes):
        self._counts[key] -= 1
        if self._counts[key] == 0:
            del self._data[key]
            del self._counts[key]


class LMDBBytesBackingStore:
    """LMDB-backed memory-mapped store of entity data."""

    def __init__(self, path: pathlib.Path | None = None):
        """Initialize the LMDBBytesBackingStore.

        Args:
            path: The path to the LMDB database. If None, a temporary store will be created in a
                temporary directory. IMPORTANT: NEVER create multiple LMDBBytesBackingStore objects
                pointing to the same path, always re-use the same LMDBBytesBackingStore object.
        """
        if path is None:
            # Create a temporary store in a temporary directory.
            self._path = pathlib.Path(tempfile.mktemp())
            self._is_temporary = True
        else:
            self._path = path
            self._is_temporary = False

        env = lmdb.Environment(
            str(self._path),
            map_size=1024 * 1024 * 1024 * 1024,  # 1TB
            writemap=True,
            map_async=True,
            max_dbs=2,
        )
        self._env: lmdb.Environment | None = env
        self._storage_db = env.open_db(b"storage", create=True)
        self._count_db = env.open_db(b"count", create=True)

        self._finalizer = weakref.finalize(
            self,
            LMDBBytesBackingStore._finalize_store,
            env,
            self._path if self._is_temporary else None,
        )

    def close(self):
        if self._env is not None:
            self._env.close()
            self._env = None
            if self._is_temporary and self._path.exists():
                self._path.unlink()

            self._finalizer.detach()

    @staticmethod
    def _finalize_store(env: lmdb.Environment, cleanup_path: pathlib.Path | None):
        env.close()
        if cleanup_path is not None and cleanup_path.exists():
            cleanup_path.unlink()

    def save_and_increment(self, key: bytes, value: bytes):
        if self._env is None:
            raise ValueError("LMDBBytesBackingStore is closed")
        with self._env.begin(write=True) as txn:
            if txn.put(key, value, db=self._storage_db):
                logger.debug(f"Saved %s to LMDB store at %s", key, self._path)
            ct = int.from_bytes(memoryview(txn.get(key, default=b"\x00", db=self._count_db)))  # type: ignore
            txn.put(key, int.to_bytes(ct + 1, length=8), db=self._count_db)

    def get(self, key: bytes) -> memoryview:
        if self._env is None:
            raise ValueError("LMDBBytesBackingStore is closed")
        with self._env.begin(write=False) as txn:
            return memoryview(txn.get(key, db=self._storage_db))  # type: ignore

    def free(self, key: bytes):
        if self._env is None:
            raise ValueError("LMDBBytesBackingStore is closed")
        with self._env.begin(write=True) as txn:
            ct = int.from_bytes(memoryview(txn.get(key, default=b"\x00", db=self._count_db)))  # type: ignore
            if ct == 0:
                txn.delete(key, db=self._storage_db)
                txn.delete(key, db=self._count_db)
                logger.debug(f"Freed %s from LMDB store at %s", key, self._path)
            else:
                txn.put(key, int.to_bytes(ct - 1, length=8), db=self._count_db)


DEBUG_DROPPED_ARENAS = os.environ.get("FXTR_DEBUG_DROPPED_ARENAS", "0") == "1"


class MemoryStore(ObservableStore):
    """Backing store shared by in-memory arenas.

    Memory arenas track their entity states in a memory store object, which holds the actual entity
    data. The memory store can optionally attach to a parent arena, which it can use to import
    data.

    We maintain the invariant that any node tracked by any arena of this store has a corresponding
    entry in _entity_states, although it may be an InheritedNode if the node has not yet been
    loaded from storage. Additionally, if node A requires node B or unwraps to node B, and node A
    has an entry in _entity_states, then node B must also have an entry in _entity_states.
    (Inherited nodes are not considered to require any other nodes, but they may unwrap to them.)

    External arenas can be attached to the memory store by calling `bare_lazily_attach_from_arena`
    on one of its arenas. It is the caller's responsibility to ensure that any arenas attached to
    the memory store in this manner remain open and accessible for the lifetime of the memory store,
    because references are deduplicated but data is only loaded from the backing store lazily.
    """

    def __init__(
        self,
        *,
        force_async_sleep: bool = False,
        backing_store: LMDBBytesBackingStore | InMemoryBytesBackingStore | None = None,
    ):
        self._force_async_sleep = force_async_sleep
        self._backing_store = backing_store or InMemoryBytesBackingStore()

        # Entity states
        self._entity_states: dict[
            FxtrID, StoredNode | LazyStoredNode | DerivedNode | InheritedNode
        ] = {}

        # Management of the arena state and subarenas
        self._arena_counter: int = 0
        self._arenas: dict[ArenaKey, MemoryArenaState] = {}

        # Tracking of the inherited nodes we have
        self._inherited_nodes: list[FxtrID] = []

        # Dead nodes: track nodes that no longer have anyone else that requires them.
        # We don't immediately remove the, for debugging purposes. Note that dead nodes can still
        # keep other nodes in the store.
        self._dead_pre_cleanup: set[FxtrID] = set()

        # Fresh nodes: track nodes that we just created but haven't added any dependencies yet.
        # These should be distinguished from dead nodes. Usually every node with no requirements is
        # either fresh or dead
        self._fresh_nodes: set[FxtrID] = set()

        # Dirty flag for debug tracking
        self._debug_dirty = RepeatedEvent()

        # Environment-variable-gated switch for debugging dropped arenas
        self._debug_dropped_arena_tbs: dict[ArenaKey, traceback.StackSummary] = {}

    def close(self, close_arenas: bool = False):
        """Clear all state from the memory arena, assuming all arenas are closed.

        The main use case is to quickly collect garbage state that is no longer needed when a scope
        exits, and release all claims on parent arenas.

        Args:
            close_arenas: Whether to close all arenas. If False, the caller must ensure that all
                arenas are closed before calling this method.

        Raises:
            ValueError: If close_arenas is False and there are open arenas.
        """
        if self._arenas:
            if close_arenas:
                for arena_key in list(self._arenas.keys()):
                    self.close_arena(arena_key)
            else:
                raise ValueError("Cannot clear state with open arenas unless close_arenas is True")

        # Clear all state.
        all_nodes = list(self._entity_states.values())
        self._entity_states.clear()
        self._inherited_nodes.clear()
        self._dead_pre_cleanup.clear()
        self._fresh_nodes.clear()

        # Free backing store data for stored nodes.
        inheritance_parents: dict[int, ReadableArena] = {}
        for node in all_nodes:
            if isinstance(node, StoredNode):
                self._backing_store.free(node.my_id._raw_data_id)
                logger.debug(f"Clear all: Node {node.my_id} freed from backing store")
            elif isinstance(node, InheritedNode):
                ipa = node.inheritance_parent_arena
                if id(ipa) not in inheritance_parents:
                    inheritance_parents[id(ipa)] = ipa

        for ipa in inheritance_parents.values():
            ipa.close()
            logger.debug(f"Clear all: Parent arena {ipa} closed")

        self._debug_dirty.trigger()

    def _add_requirement(self, node_id: FxtrID, *, required_by: FxtrID | ArenaKey):
        """Mark a node as required by another node."""
        assert node_id in self._entity_states
        node = self._entity_states[node_id]
        if node_id in self._fresh_nodes:
            self._fresh_nodes.discard(node_id)
        elif len(node.required_by) == 0:
            logger.debug(f"Node {node_id} being resurrected by {required_by}!")
            if node_id not in self._dead_pre_cleanup:
                logger.debug(f"Node {node_id} had no required_by but was not dead or fresh?")
            self._dead_pre_cleanup.discard(node_id)
        node.required_by.add(required_by)

    def _collect_one_garbage(self) -> bool:
        """Collect up to a single garbage node. Returns True if there is more garbage to collect."""
        if not self._dead_pre_cleanup:
            return False
        # Remove the node.
        node_id = self._dead_pre_cleanup.pop()
        node = self._entity_states.pop(node_id)
        # Make sure nobody depends on it.
        if node.required_by:
            logging.error(
                f"Node {node_id} was in dead set but is still required by {node.required_by}!"
            )
            # Store it again.
            self._entity_states[node_id] = node
            return bool(self._dead_pre_cleanup)

        # Release it from its parent arena if it is an InheritedNode.
        if isinstance(node, InheritedNode):
            self._inherited_nodes.remove(node_id)
            node.inheritance_parent_arena.abandon(node_id)
            logger.debug(f"Node {node_id} abandoned from parent arena!")

        else:
            # Reduce dependency count of its dependencies.
            for reqmt in node.requires:
                required_node = self._entity_states[reqmt]
                required_node.required_by.discard(node_id)
                if not node.required_by and not node.wrapped_by:
                    logger.debug(
                        f"Node {reqmt} now dead while discarding garbage node {node_id} (that"
                        " required it)!"
                    )
                    self._dead_pre_cleanup.add(reqmt)

            # Release it from the backing store if it is a StoredNode.
            if isinstance(node, StoredNode):
                self._backing_store.free(node.my_id._raw_data_id)

        # Reduce dependency count of its unwrapped node.
        if node.unwraps_to:
            unwrapped_node = self._entity_states[node.unwraps_to]
            unwrapped_node.required_by.discard(node_id)
            if not unwrapped_node.required_by and not unwrapped_node.wrapped_by:
                logger.debug(
                    f"Node {node.unwraps_to} now dead while discarding garbage node {node_id} (that"
                    " unwrapped to it)!"
                )
                self._dead_pre_cleanup.add(node.unwraps_to)

        logger.debug(f"Node {node_id} cleared from storage!")
        return bool(self._dead_pre_cleanup)

    def _maybe_mark_dead(self, node_id: FxtrID):
        """Mark a node as dead if it has no required_by and no wrapped_by."""
        node = self._entity_states[node_id]
        if not node.required_by and not node.wrapped_by:
            logger.debug(f"Node {node_id} now dead!")
            self._dead_pre_cleanup.add(node_id)

    @_triggers_debug_dirty
    def bare_lazily_attach_from_arena(self, node_id: FxtrID, source: ReadableArena):
        """Lazily attach a node from another arena, without yet tracking it in our own arena."""
        if not source.is_tracked(node_id):
            raise ValueError(f"Node {node_id} is not tracked in the source arena!")
        if node_id in self._entity_states:
            # Already have it; no need to copy.
            return

        owned_source_arena = source.copy_to_new_arena([node_id])
        self._adopt_inheritance_arena_claim(node_id, owned_source_arena)

    def _adopt_inheritance_arena_claim(
        self, node_id: FxtrID, inheritance_parent_arena: ReadableArena
    ) -> StoredNode | LazyStoredNode | DerivedNode | InheritedNode:
        """Make and store a node ID returned by a parent arena, if not already in the store.

        The memory store maintains an invariant that the set of InheritedNode instances in the store
        exactly matches the set of nodes that are claimed by each parent arena. If we just made a
        claim with an inheritance parent arena, we can use this to either introduce a new
        InheritedNode, or to release the claim if we already have a version of the node locally.

        Note that if we have _multiple_ possible InheritedNode instances for the same node ID, one
        will be chosen arbitrarily.

        Args
            node_id: The node ID to adopt. Must be a node that is claimed by the parent arena.
            inheritance_parent_arena: The arena that is responsible for loading this node.

        Returns:
            The node that is in the store after fixing the invariant. Will either be a new
            InheritedNode,
        """
        assert inheritance_parent_arena.is_tracked(node_id)

        existing_node = self._entity_states.get(node_id)
        if existing_node is None:
            # Need to make a new InheritedNode.
            if (unwraps_to := inheritance_parent_arena.unwrap_provenance(node_id)) is not None:
                self._adopt_inheritance_arena_claim(unwraps_to, inheritance_parent_arena)
            new_node = InheritedNode(
                my_id=node_id,
                inheritance_parent_arena=inheritance_parent_arena,
                unwraps_to=unwraps_to,
                required_by=set(),
                wrapped_by=set(),
            )
            self._inherited_nodes.append(node_id)
            self._entity_states[node_id] = new_node
            self._fresh_nodes.add(node_id)
            logger.debug(
                f"Node {node_id} newly adopted from a parent arena {inheritance_parent_arena}!"
            )
            return new_node
        elif isinstance(existing_node, InheritedNode):
            if existing_node.inheritance_parent_arena == inheritance_parent_arena:
                # We already have an InheritedNode for this node with the same parent arena, so
                # we are already tracking this claim. Nothing to do.
                return existing_node
            else:
                # We have an InheritedNode for this node, in a different parent arena. For
                # simplicity, we preserve the existing InheritedNode and abandon the new claim.
                # (There is some argument to instead bind the new claim. However, if this node had
                # already been loaded, the new claim would be dropped anyway, and the children of
                # the node would still be inherited from the old arena. So preserving the old arena
                # in this case as well has more consistent behavior across loads.)
                inheritance_parent_arena.abandon(node_id)
                logger.debug(
                    f"Claim on node {node_id} released from parent arena"
                    f" {inheritance_parent_arena} (already inherited from"
                    f" {existing_node.inheritance_parent_arena})"
                )
                return existing_node
        else:
            # We already have a StoredNode or LazyStoredNode for this node. Release the claim.
            inheritance_parent_arena.abandon(node_id)
            logger.debug(
                f"Claim on node {node_id} released from parent arena"
                f" {inheritance_parent_arena} (already have this node)"
            )
            return existing_node

    def _adopt_inheritance_arena_claim_or_none(
        self, node_id: FxtrID | None, inheritance_parent_arena: ReadableArena
    ) -> StoredNode | LazyStoredNode | DerivedNode | InheritedNode | None:
        """Like _adopt_inheritance_arena_claim, but returns None if the node ID is None."""
        if node_id is None:
            return None
        return self._adopt_inheritance_arena_claim(node_id, inheritance_parent_arena)

    @_triggers_debug_dirty
    def new_empty_arena(self) -> MemoryArena:
        """Create a new, empty arena."""
        key = ArenaKey(ix=self._arena_counter)
        self._arena_counter += 1
        self._arenas[key] = MemoryArenaState(my_key=key, liveset=set())
        logger.debug(f"New arena {key} created!")
        arena = MemoryArena(store=self, key=key)
        arena.track_builtin(FxtrID.from_tag_and_payload(".", b""))
        if DEBUG_DROPPED_ARENAS:
            self._debug_dropped_arena_tbs[key] = traceback.extract_stack()
        weakref.finalize(arena, self._finalize_arena_object, key)
        return arena

    def _finalize_arena_object(self, key: ArenaKey):
        if key in self._arenas:
            logger.info(
                f"Arena {key} was finalized while open! Cleaning it up."
                f" ({len(self._arenas[key].liveset)} nodes live)"
            )
            if DEBUG_DROPPED_ARENAS:
                if key in self._debug_dropped_arena_tbs:
                    try:
                        tb = self._debug_dropped_arena_tbs[key]
                        logger.warning(
                            f"Arena {key} was finalized while open! This arena was created at:\n{''.join(tb.format())}"
                        )
                    except BaseException as e:
                        logger.warning(
                            f"Error formatting traceback for arena {key} finalization",
                            exc_info=True,
                        )
                else:
                    logger.warning(
                        f"Arena {key} was finalized while open, but no traceback was recorded?"
                    )
            self.close_arena(key)

    @_triggers_debug_dirty
    def track_in_arena(self, node_id: FxtrID, arena_key: ArenaKey):
        """Track a node in an arena."""
        arena_state = self._arenas[arena_key]
        if node_id not in arena_state.liveset:
            self._add_requirement(node_id, required_by=arena_key)
            arena_state.liveset.add(node_id)
            logger.debug(f"Node {node_id} now in arena {arena_key}!")

    @_triggers_debug_dirty
    def untrack_from_arena(self, node_id: FxtrID, arena_key: ArenaKey):
        """Remove a node from an arena."""
        arena_state = self._arenas[arena_key]
        assert node_id in arena_state.liveset
        arena_state.liveset.discard(node_id)
        node = self._entity_states[node_id]
        node.required_by.discard(arena_key)
        if not node.required_by and not node.wrapped_by:
            logger.debug(f"Node {node_id} now dead after being untracked from arena {arena_key}!")
            self._dead_pre_cleanup.add(node_id)

    @_triggers_debug_dirty
    def close_arena(self, arena_key: ArenaKey):
        """Close an arena.

        This method marks an arena as closed, releasing all nodes required by the arena and
        preventing future access.
        """
        logger.debug(f"Closing arena {arena_key}!")
        arena = self._arenas.pop(arena_key)
        for node_id in arena.liveset:
            node = self._entity_states[node_id]
            node.required_by.discard(arena_key)
            if not node.required_by and not node.wrapped_by:
                logger.debug(f"Node {node_id} now dead after closing arena {arena_key}!")
                self._dead_pre_cleanup.add(node_id)
        arena.liveset.clear()
        self._debug_dropped_arena_tbs.pop(arena_key, None)
        logger.debug(f"Arena {arena_key} closed.")

    def _already_has_data_for(self, node_id: FxtrID) -> bool:
        """Check if a node is known to be in the store and not an inherited node."""
        return node_id in self._entity_states and not isinstance(
            self._entity_states[node_id], InheritedNode
        )

    def _save_or_upgrade_node(self, new_node: StoredNode | LazyStoredNode | DerivedNode) -> None:
        """Save a node or upgrade it from an inherited node to a real node."""
        if new_node.my_id in self._entity_states:
            # The node must be an inherited node that we are replacing.
            replaced_node = self._entity_states[new_node.my_id]
            assert isinstance(replaced_node, InheritedNode)
            assert replaced_node.my_id == new_node.my_id

            # Need to track new requirements, since inherited nodes didn't track them.
            for required_id in new_node.requires:
                self._add_requirement(required_id, required_by=new_node.my_id)

            # Make sure the "lower" unwrapped node knows about this node.
            assert replaced_node.unwraps_to == new_node.unwraps_to
            if new_node.unwraps_to is not None:
                unwrapped_node = self._entity_states[new_node.unwraps_to]
                assert new_node.my_id in unwrapped_node.wrapped_by

            # Preserve mutable tracking state.
            for reqd_id in replaced_node.required_by:
                new_node.required_by.add(reqd_id)
            for wrap_id in replaced_node.wrapped_by:
                new_node.wrapped_by.add(wrap_id)

            # Upgrade the node.
            self._entity_states[new_node.my_id] = new_node
            self._inherited_nodes.remove(replaced_node.my_id)
            replaced_node.inheritance_parent_arena.abandon(replaced_node.my_id)
            logger.debug(
                f"Node {new_node.my_id} upgraded to a real node! Abandoned claim in parent arena"
                f" {replaced_node.inheritance_parent_arena}."
            )
        else:
            # The node is not in the store yet.
            # We need to first ensure the invariants that are required
            # when adding the node are met, and then finally store the node.

            # Make sure referenced nodes know about this node; it should keep them alive.
            for required_id in new_node.requires:
                self._add_requirement(required_id, required_by=new_node.my_id)

            # Make sure the "lower" unwrapped node knows about this node; this one should keep the
            # lower node alive.
            if new_node.unwraps_to is not None:
                unwrapped_node = self._entity_states[new_node.unwraps_to]
                unwrapped_node.wrapped_by.add(new_node.my_id)

            # Finally, store the node. (We store it at the end so that, whenever a node is stored,
            # the invariants already hold. There is a small chance that an error occurs during the
            # invariant setup; in this case some nodes may live too long but we can detect this
            # with a garbage collection sweep.)
            self._entity_states[new_node.my_id] = new_node
            self._fresh_nodes.add(new_node.my_id)
            logger.debug(f"Node {new_node.my_id} newly added to store!")

    @_triggers_debug_dirty
    def bare_store(self, entity: FxtrCbor) -> FxtrID:
        """Store an entity in the store without tracking it in any arena (yet).

        Before calling this method, the caller (usually an arena) must have ensured that all
        children of the entity are already tracked in the store as a whole.
        """
        # First walk the children to make sure that this entity is storeable.
        assert isinstance(entity, dict)
        assert "$fxtr.ctx" in entity
        ctx_id = entity["$fxtr.ctx"]
        assert isinstance(ctx_id, entity_core.RawFxtrEntityMarkerID)

        ctx_tag, ctx_payload = ctx_id.inner_id.split_tag_and_payload()
        match ctx_tag:
            case ".":
                # If context is "." -> This is a ground "d" node.
                entity_bytes = entity_core.fxtr_cbor_dumps_with_marker_check(entity)
                new_id = entity_core.FxtrID.from_tag_and_payload(
                    "d", hashlib.sha256(entity_bytes).digest()
                )

                # If the entity is already known, return it immediately.
                if self._already_has_data_for(new_id):
                    logger.debug(f"Node {new_id} is already in the store")
                    return new_id

                # Otherwise, we need to store the entity and compute the things it requires.
                requires = set()
                strong_links = set()

                def _visitor(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ):
                    assert child_typed_id.inner_id in self._entity_states
                    requires.add(child_typed_id.inner_id)
                    if isinstance(child_typed_id, RawStrongFxtrID):
                        strong_links.add(child_typed_id.inner_id)

                visit_ids(entity, _visitor)

                self._backing_store.save_and_increment(new_id._raw_data_id, entity_bytes)
                new_node = StoredNode(
                    my_id=new_id,
                    requires=frozenset(requires),
                    strong_linked_refs=frozenset(strong_links),
                    unwraps_to=None,
                )
            case "c":
                # If context is "c" -> This is a stored provenance "n" node.
                entity_bytes = entity_core.fxtr_cbor_dumps_with_marker_check(entity)
                new_id = entity_core.FxtrID.from_tag_and_payload(
                    "n", hashlib.sha256(entity_bytes).digest()
                )

                # If the entity is already known, return it immediately.
                if self._already_has_data_for(new_id):
                    logger.debug(f"Node {new_id} is already in the store")
                    return new_id

                # Otherwise, we need to store the entity and compute the things it requires.
                requires = set()
                strong_links = set()

                def _projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    child_id = child_typed_id.inner_id
                    assert child_id in self._entity_states
                    child_state = self._entity_states[child_id]
                    if child_state.unwraps_to is None:
                        raise ValueError(
                            "Illegal entity: this entity declares itself to be an 'n'-type"
                            f" provenance node but has a non-provenance child node: {child_id}. All"
                            " children of an 'n'-node must be provenance nodes."
                        )

                    requires.add(child_id)
                    if isinstance(child_typed_id, RawStrongFxtrID):
                        strong_links.add(child_id)
                    return child_state.unwraps_to

                unwrap_data = visit_and_project_ids(entity, _projector)

                # Recursively store the unwrapped node first. Note that right now we may
                # immediately clear its data as soon as we commit this node, but we need to do all
                # the same work anyway, so this is the easiest way to do it.
                unwrap_id = self.bare_store(unwrap_data)

                # Now build the new node.
                self._backing_store.save_and_increment(new_id._raw_data_id, entity_bytes)
                new_node = StoredNode(
                    my_id=new_id,
                    requires=frozenset(requires),
                    strong_linked_refs=frozenset(strong_links),
                    unwraps_to=unwrap_id,
                )
            case "p":
                # Context is "p" -> This must be itself a provenance "p" node.
                # The only valid way to construct a new "p" node is to either call
                # `create_p_node` to make a root "p" node, or to load a "p" node (which spawns its
                # children). Additionally, those children always keep their parent node alive. This
                # means that storing a "p" node is only allowed if this "p" node is
                # either already in the store, or in the parent arena reachable from this arena.
                payload_data = cbor2.loads(ctx_payload)
                assert isinstance(payload_data, list)
                assert (
                    len(payload_data) >= 2
                ), "any valid p node has a nonempty path to its ctx field"
                external_payload = payload_data[:-1]
                new_id = FxtrID.from_tag_and_payload("p", cbor2.dumps(external_payload))

                ctx_node = self._entity_states[ctx_id.inner_id]

                # One thing one might try to do here would be to load the p node or the child it
                # unwraps to, and make sure that the thing we were passed is what we expect.
                # However, loading is allowed to be async, so we can't easily do that. Additionally,
                # it is possible that this p node is not actually in our store yet, if it was
                # inherited from the parent arena.
                #
                # However, we _do_ know that all IDs that the caller has passed us already have
                # entries in our store. This means there are effectively three cases:
                # 1: This p node is already in our store as a DerivedNode.
                # 2: This p node is in our store as an InheritedNode, but we don't yet have its
                #   data, and it might take an async load to get the data.
                # 3: This p node is not in our store. However, we know this p node is a dependency
                #   of any other p node with a longer path. So, the only way we would not have it
                #   in our store is if every other p node in the provided `entity` data is an
                #   InheritedNode.
                # We can reduce case 3 to case 2 by using the parent arena to walk back along the
                # path, and introduce a new InheritedNode from this. Then, to deal with case 2,
                # we can synchronously _unwrap_ the p node to figure out what data ID it unwraps to.
                # Then, we can store this data node, and then check that the ID we believe this node
                # to unwrap to matches the one we get.

                p_child_counter = 0

                def _projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    # Child IDs of p nodes must be the root p node plus the child index; any other
                    # value is illegal.
                    nonlocal p_child_counter
                    expected_child_id = FxtrID.from_tag_and_payload(
                        "p", cbor2.dumps(external_payload + [p_child_counter])
                    )
                    p_child_counter += 1

                    child_id = child_typed_id.inner_id
                    if child_id != expected_child_id:
                        raise ValueError(
                            "Illegal p-tag entity: this entity declares itself to be a 'p'-type"
                            f" provenance node but child ID {child_id} does not match expected"
                            f" child ID {expected_child_id}."
                        )
                    assert child_id in self._entity_states
                    child_state = self._entity_states[child_id]
                    assert child_state.unwraps_to is not None
                    return child_state.unwraps_to

                unwrap_data = visit_and_project_ids_in_canonical_cbor_order(entity, _projector)

                if new_id in self._entity_states:
                    retrieved_node = self._entity_states[new_id]
                elif isinstance(ctx_node, InheritedNode):
                    # We need to introduce a new InheritedNode from this. We are free to step
                    # back from the ctx node to retrieve the parent.
                    retrieved_node = self._adopt_inheritance_arena_claim_or_none(
                        ctx_node.inheritance_parent_arena.step_back_p_ctx(ctx_node.my_id),
                        ctx_node.inheritance_parent_arena,
                    )
                    assert retrieved_node is not None
                    assert new_id == retrieved_node.my_id
                else:
                    raise RuntimeError(
                        "The entity being stored is a path-provenance node, but it is not alive"
                        " in the fxtr store, and its ctx is not inherited! This"
                        " should not be possible, because children of a path-provenance node"
                        " depend on their parent."
                    )

                # Recursively store the unwrapped node first. If this node is a valid p-node, this
                # child node should be the same as `expected_unwrap_id`, which is already tracked.
                actual_id_of_unwrap_data = self.bare_store(unwrap_data)

                if actual_id_of_unwrap_data != retrieved_node.unwraps_to:
                    self._maybe_mark_dead(actual_id_of_unwrap_data)
                    raise ValueError(
                        "Illegal p-tag entity: provided wrapped data is inconsistent with expected"
                        f" data (expected {retrieved_node.unwraps_to} but got"
                        f" {actual_id_of_unwrap_data})"
                    )

                # We've validated that all children are consistent with the expected structure and
                # that the child data matches the expected data, so this node is already stored
                # correctly.
                if isinstance(retrieved_node, InheritedNode):
                    # We are storing it, and we have the actual data, so we can convert it to a
                    # DerivedNode.
                    context_node = self._adopt_inheritance_arena_claim(
                        retrieved_node.inheritance_parent_arena.p_node_to_pathroot(new_id),
                        retrieved_node.inheritance_parent_arena,
                    )
                    new_node = DerivedNode(
                        my_id=new_id,
                        ctx_node=context_node.my_id,
                        requires=frozenset({context_node.my_id}),
                        unwraps_to=retrieved_node.unwraps_to,
                    )
                else:
                    assert isinstance(retrieved_node, DerivedNode)
                    logger.debug(f"Node {new_id} is already in the store")
                    return new_id

            case "u":

                # Context is "u" -> This must be itself a provenance "u" node.
                # All children must also be "u" nodes, and we should store the data they unwrap
                # to and then build the "u" node from that.
                def _projector(
                    child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                ) -> FxtrID:
                    child_id = child_typed_id.inner_id
                    assert child_id in self._entity_states
                    child_state = self._entity_states[child_id]
                    child_tag, child_payload = child_id.split_tag_and_payload()
                    if child_tag != "u":
                        raise ValueError(
                            "Illegal entity: this entity declares itself to be a 'u'-type"
                            f" provenance node but has a non-'u'-type child node: {child_id}. All"
                            " children of a 'u'-node must be 'u'-type nodes."
                        )
                    unwraps_to = FxtrID(raw_data_id=child_payload)
                    assert child_state.unwraps_to == unwraps_to
                    return unwraps_to

                unwrap_data = visit_and_project_ids(entity, _projector)

                # Recursively store the unwrapped node first.
                unwrap_id = self.bare_store(unwrap_data)
                new_id = entity_core.FxtrID.from_tag_and_payload("u", unwrap_id._raw_data_id)

                # Then construct a wrapper node for the unwrapped node.
                new_node = DerivedNode(
                    my_id=new_id,
                    requires=frozenset({unwrap_id}),
                    unwraps_to=unwrap_id,
                )
            case _:
                raise ValueError(
                    f'Invalid entity: "$fxtr.ctx" has tag {ctx_tag}! Expected ".", "c", "p", or'
                    ' "u".'
                )

        self._save_or_upgrade_node(new_node)

        # Final cleanup.
        if new_node.unwraps_to is not None:
            unwrapped_node = self._entity_states[new_node.unwraps_to]
            if isinstance(new_node, StoredNode) and isinstance(unwrapped_node, StoredNode):
                # We can free the unwrapped node, because we can recompute it from this node on
                # demand.
                self._entity_states[new_node.unwraps_to] = unwrapped_node.into_lazy()
                self._backing_store.free(unwrapped_node.my_id._raw_data_id)
                logger.debug(
                    f"Downgraded data node {new_node.unwraps_to} to lazy (referencing provenance"
                    f" node {new_node.my_id})"
                )

        return new_node.my_id

    def _ensure_own_derived(self, node_id: FxtrID) -> DerivedNode:
        """Ensure we have a DerivedNode for the given node ID, not an InheritedNode."""
        assert node_id in self._entity_states
        node = self._entity_states[node_id]
        match node:
            case DerivedNode():
                return node
            case InheritedNode(inheritance_parent_arena=parent_arena):
                match node_id.get_tag():
                    case "." | "!":
                        # Builtin node! No content.
                        new_node = DerivedNode(my_id=node_id, requires=frozenset())
                    case "c":
                        ctx_node = self._adopt_inheritance_arena_claim(
                            parent_arena.c_node_to_context_data(node_id), parent_arena
                        )
                        new_node = DerivedNode(
                            my_id=node_id,
                            ctx_node=ctx_node.my_id,
                            requires=frozenset({ctx_node.my_id}),
                            unwraps_to=node.unwraps_to,
                        )
                    case "p":
                        pathroot_node = self._adopt_inheritance_arena_claim(
                            parent_arena.p_node_to_pathroot(node_id), parent_arena
                        )
                        backstep_node = self._adopt_inheritance_arena_claim_or_none(
                            parent_arena.step_back_p_ctx(node_id), parent_arena
                        )
                        if backstep_node is None:
                            backstep_node_id = None
                            requires = frozenset({pathroot_node.my_id})
                        else:
                            backstep_node_id = backstep_node.my_id
                            requires = frozenset({pathroot_node.my_id, backstep_node_id})
                        new_node = DerivedNode(
                            my_id=node_id,
                            ctx_node=pathroot_node.my_id,
                            backstep_node=backstep_node_id,
                            requires=requires,
                            unwraps_to=node.unwraps_to,
                        )
                    case "u":
                        assert node.unwraps_to is not None
                        new_node = DerivedNode(
                            my_id=node_id,
                            requires=frozenset({}),
                            unwraps_to=node.unwraps_to,
                        )
                    case _:
                        raise ValueError(f"Unsupported derived node tag: {node_id} -> {type(node)}")
                self._save_or_upgrade_node(new_node)
                return new_node
            case _:
                raise RuntimeError(f"Bad derived node type: {node_id} -> {type(node)}")

    async def _ensure_own(self, node_id: FxtrID) -> StoredNode | LazyStoredNode | DerivedNode:
        """Ensure we have the node with this ID in our own store, pulling from parent if needed.

        This is async because we may need to load node data from the parent arena. If you already
        know the node is a derived node that can be manipulated synchronously, you can use
        `_ensure_own_derived` instead.

        Args:
            node_id: The node ID to ensure we have.

        Returns:
            A node in this store.
        """
        assert node_id in self._entity_states
        node = self._entity_states[node_id]
        if isinstance(node, InheritedNode):
            parent_arena = node.inheritance_parent_arena
            match node_id.get_tag():
                case "d" | "n":
                    # Loading a stored node from an inheritance arena!
                    # First load it from the parent.
                    loaded_data = await parent_arena.load(node_id)
                    # Then make sure this store is aware of all of the new nodes loaded from the
                    # parent.
                    visit_ids(
                        loaded_data,
                        lambda child_id: self._adopt_inheritance_arena_claim(
                            child_id.inner_id, parent_arena
                        ),
                    )
                    # Finally, store it in our own store. This will also upgrade the node to be
                    # a StoredNode and abandon the claim in the parent arena once it is no longer
                    # needed.
                    own_node_id = self.bare_store(loaded_data)
                    assert own_node_id == node_id
                    own_node = self._entity_states[own_node_id]
                    assert isinstance(own_node, (StoredNode, LazyStoredNode))
                    return own_node
                case "." | "!" | "c" | "p" | "u":
                    # Loading a derived node!
                    return self._ensure_own_derived(node_id)
                case tag:
                    raise ValueError(f"Unsupported node tag: {node_id} -> {tag}")
        elif isinstance(node, (StoredNode, LazyStoredNode, DerivedNode)):
            return node
        else:
            raise RuntimeError(f"Bad node type: {node_id} -> {type(node)}")

    @_triggers_debug_dirty
    async def bare_load(self, node_id: FxtrID) -> FxtrCbor:
        """Load an entity from the store without tracking it in any arena (yet)."""
        assert node_id in self._entity_states
        node = await self._ensure_own(node_id)

        if isinstance(node, StoredNode):
            # Rehydrate the entity from storage.
            if self._force_async_sleep:
                await anyio.sleep(0.00001)
            cbor_bytes = self._backing_store.get(node_id._raw_data_id)
            return entity_core.fxtr_cbor_loads(cbor_bytes)

        elif isinstance(node, LazyStoredNode):
            # Rehydrate the entity by accessing a stored provenance node that unwraps to it.
            distance = 0
            load_target = node
            while isinstance(load_target, LazyStoredNode):
                # Walk up until we find a stored node.
                for upper_id in load_target.wrapped_by:
                    upper = self._entity_states.get(upper_id)
                    if isinstance(upper, (StoredNode, LazyStoredNode)):
                        load_target = upper
                        break
                else:
                    raise RuntimeError(
                        f"No owning wrapper found for lazy stored node {load_target.my_id}!"
                    )
                distance += 1
            assert isinstance(load_target, StoredNode)

            def _down_projector(
                child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
            ) -> FxtrID:
                current_id = child_typed_id.inner_id
                for _ in range(distance):
                    # Walk back down the chain to the data node.
                    assert current_id in self._entity_states
                    state = self._entity_states[current_id]
                    assert state.unwraps_to
                    current_id = state.unwraps_to
                # "d" and "n" nodes depend on their children, so the children already exist.
                assert current_id in self._entity_states
                return current_id

            cbor_bytes = self._backing_store.get(load_target.my_id._raw_data_id)
            rehydrated_concrete = entity_core.fxtr_cbor_loads(cbor_bytes)
            rehydrated_projected = visit_and_project_ids(rehydrated_concrete, _down_projector)
            return rehydrated_projected

        elif isinstance(node, DerivedNode):
            match node_id.get_tag():
                case "." | "!" | "c":
                    raise ValueError(f"Cannot load a contentless ID! Got {node_id}")

                case "p":
                    # We are loading a p-node! Loading p-nodes requires lazily creating all of its
                    # children.
                    _, node_payload = node_id.split_tag_and_payload()
                    assert node.unwraps_to is not None
                    assert node.ctx_node is not None
                    payload_data = cbor2.loads(node_payload)
                    assert isinstance(payload_data, list)

                    ctx_node = node.ctx_node

                    # Load the contents of the data node.
                    unwrap_contents = await self.bare_load(node.unwraps_to)

                    # Wrap it as needed. Note that this wrapping step is the _only_ way to construct
                    # a new p-node with a nonempty path.
                    p_child_counter = 0

                    def _projector(
                        child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                    ) -> FxtrID:
                        unwrapped_child_id = child_typed_id.inner_id
                        assert unwrapped_child_id in self._entity_states
                        nonlocal p_child_counter
                        new_p_child_id = FxtrID.from_tag_and_payload(
                            "p", cbor2.dumps(payload_data + [p_child_counter])
                        )
                        p_child_counter += 1
                        if new_p_child_id in self._entity_states:
                            assert (
                                self._entity_states[new_p_child_id].unwraps_to == unwrapped_child_id
                            )
                        elif isinstance(child_typed_id, RawFxtrEntityMarkerID):
                            # Marker node; need to track backref. Backref should point to the
                            # node we are expanding.
                            new_p_child = DerivedNode(
                                my_id=new_p_child_id,
                                ctx_node=ctx_node,
                                backstep_node=node_id,
                                requires=frozenset({ctx_node, node_id}),
                                unwraps_to=unwrapped_child_id,
                            )
                            self._save_or_upgrade_node(new_p_child)
                        else:
                            # Non-marker node; no backref.
                            # Create a new derived node for the child.
                            new_p_child = DerivedNode(
                                my_id=new_p_child_id,
                                ctx_node=ctx_node,
                                backstep_node=None,
                                requires=frozenset({ctx_node}),
                                unwraps_to=unwrapped_child_id,
                            )
                            self._save_or_upgrade_node(new_p_child)
                        return new_p_child_id

                    rehydrated_projected = visit_and_project_ids_in_canonical_cbor_order(
                        unwrap_contents, _projector
                    )
                    return rehydrated_projected
                case "u":
                    # We are loading a u-node! Load it by loading its data node and wrapping.
                    assert node.unwraps_to is not None
                    unwrap_contents = await self.bare_load(node.unwraps_to)

                    def _projector(
                        child_typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
                    ) -> FxtrID:
                        return self.create_u_node(child_typed_id.inner_id)

                    rehydrated_projected = visit_and_project_ids(unwrap_contents, _projector)
                    return rehydrated_projected
                case tag:
                    raise ValueError(
                        f"Internal error: invalid derived node type {tag} for node {node_id}!"
                    )

        else:
            raise RuntimeError(f"Bad node type: {node_id} -> {type(node)}")

    @_triggers_debug_dirty
    def unwrap_provenance(self, node_id: FxtrID) -> FxtrID | None:
        """Unwrap a provenance node."""
        assert node_id in self._entity_states
        node = self._entity_states[node_id]
        return node.unwraps_to

    @_triggers_debug_dirty
    def track_builtin(self, node_id: FxtrID) -> FxtrID:
        """Track a "." or "!" node."""
        assert node_id.get_tag() in (".", "!")
        if not self._already_has_data_for(node_id):
            new_node = DerivedNode(my_id=node_id, requires=frozenset())
            self._save_or_upgrade_node(new_node)
        return node_id

    @_triggers_debug_dirty
    def create_c_node(self, context_data: FxtrCbor) -> FxtrID:
        """Create a context provenance node from its context data."""
        if not isinstance(context_data, dict) or "value" not in context_data:
            raise ValueError("Context data must have a 'value' field")
        value = context_data["value"]
        if not isinstance(value, (RawStrongFxtrID, RawWeakFxtrID)):
            raise ValueError("Context data 'value' field must be a node ID")
        data_id = self.bare_store(context_data)
        data_tag, data_payload = data_id.split_tag_and_payload()
        if data_tag != "d":
            raise ValueError("Context data must be for a d-node")
        assert data_id in self._entity_states
        assert value.inner_id in self._entity_states
        new_id = FxtrID.from_tag_and_payload("c", data_payload)
        if not self._already_has_data_for(new_id):
            new_node = DerivedNode(
                my_id=new_id,
                ctx_node=data_id,
                requires=frozenset({data_id}),
                unwraps_to=value.inner_id,
            )
            self._save_or_upgrade_node(new_node)
        return new_id

    @_triggers_debug_dirty
    def c_node_to_context_data(self, c_node_id: FxtrID) -> FxtrID:
        """Destruct a context provenance node."""
        assert c_node_id.get_tag() == "c"
        node = self._ensure_own_derived(c_node_id)
        ctx_node_id = node.ctx_node
        assert ctx_node_id is not None and ctx_node_id in self._entity_states
        return ctx_node_id

    @_triggers_debug_dirty
    def create_p_node(self, pathroot_data: FxtrCbor) -> FxtrID:
        """Create a path provenance node from its pathroot data."""
        if not isinstance(pathroot_data, dict) or "value" not in pathroot_data:
            raise ValueError("Pathroot data must have a 'value' field")
        value = pathroot_data["value"]
        if not isinstance(value, (RawStrongFxtrID, RawWeakFxtrID)):
            raise ValueError("Pathroot data 'value' field must be a node ID")
        data_id = self.bare_store(pathroot_data)
        data_tag, data_payload = data_id.split_tag_and_payload()
        if data_tag != "d":
            raise ValueError("Pathroot data must be for a d-node")
        assert data_id in self._entity_states
        assert value.inner_id in self._entity_states
        # New node is for the root, so it has no path component.
        new_payload = cbor2.dumps([data_id._raw_data_id])
        new_id = FxtrID.from_tag_and_payload("p", new_payload)
        if not self._already_has_data_for(new_id):
            new_node = DerivedNode(
                my_id=new_id,
                ctx_node=data_id,
                backstep_node=None,
                requires=frozenset({data_id}),
                unwraps_to=value.inner_id,
            )
            self._save_or_upgrade_node(new_node)
        return new_id

    @_triggers_debug_dirty
    def p_node_to_pathroot(self, p_node_id: FxtrID) -> FxtrID:
        """Destruct a provenance node."""
        assert p_node_id.get_tag() == "p"
        node = self._ensure_own_derived(p_node_id)
        pathroot_node_id = node.ctx_node
        assert pathroot_node_id is not None and pathroot_node_id in self._entity_states
        return pathroot_node_id

    @_triggers_debug_dirty
    def step_back_p_ctx(self, p_node_id: FxtrID) -> FxtrID | None:
        """Step back along a path provenance node."""
        assert p_node_id.get_tag() == "p"
        node = self._ensure_own_derived(p_node_id)
        backstep_node_id = node.backstep_node
        if backstep_node_id is None:
            assert backstep_node_id in self._entity_states
        return backstep_node_id

    @_triggers_debug_dirty
    def create_u_node(self, wrapped_id: FxtrID) -> FxtrID:
        """Create a user-defined provenance node from a data node."""
        assert wrapped_id in self._entity_states
        new_id = FxtrID.from_tag_and_payload("u", wrapped_id._raw_data_id)
        if not self._already_has_data_for(new_id):
            # We do not need to require the wrapped node because the unwraps_to dependency always
            # keeps the wrapped node alive.
            new_node = DerivedNode(
                my_id=new_id,
                requires=frozenset({}),
                unwraps_to=wrapped_id,
            )
            self._save_or_upgrade_node(new_node)
        return new_id

    @override
    async def try_lookup(self, node_id: FxtrID) -> ReadableArena | None:
        """Try to look up an entity in the store."""
        if node_id not in self._entity_states:
            return None
        arena = self.new_empty_arena()
        self.track_in_arena(node_id, arena._key)
        return arena

    @override
    async def backrefs(self, node_id: FxtrID) -> list[EntityBackref | OtherBackref]:
        """Get backrefs to a particular entity."""
        node = self._entity_states.get(node_id)
        if node is None:
            return []
        backrefs = []

        for requirer in node.required_by:
            if isinstance(requirer, ArenaKey):
                backrefs.append(OtherBackref(kind="memory_arena", data={"arena_key": requirer.ix}))
            else:
                requirer_node = self._entity_states[requirer]
                match requirer_node:
                    case StoredNode() | LazyStoredNode():
                        if node_id in requirer_node.strong_linked_refs:
                            backrefs.append(EntityBackref(referrer=requirer, kind="strong_ref"))
                        else:
                            backrefs.append(EntityBackref(referrer=requirer, kind="weak_ref"))
                    case DerivedNode():
                        if node_id == requirer_node.ctx_node:
                            # `node_id` is the context node of some derived p or c node, which
                            # refers to it.
                            backrefs.append(EntityBackref(referrer=requirer, kind="context_data"))
                        elif node_id == requirer_node.backstep_node:
                            # Back-step dependencies are already effectively back-references, and
                            # so if `requirer` has a back-step dependency on `node_id`, this means
                            # loading `node_id` will produce `requirer`. So we do not need to track
                            # it as a backref from `node_id`'s perspective; it is a normal dep.
                            pass
                        else:
                            other_data = {
                                "msg": "Unexpected dependency from derived node",
                                "node_id": requirer.id_string(),
                            }
                            backrefs.append(OtherBackref(kind="unexpected", data=other_data))
                    case InheritedNode():
                        # Inherited nodes should not have dependencies.
                        other_data = {
                            "msg": "Unexpected dependency from inherited node",
                            "node_id": requirer.id_string(),
                        }
                        backrefs.append(OtherBackref(kind="unexpected", data=other_data))
                    case _:
                        raise RuntimeError(f"Unexpected node type: {type(requirer_node)}")

        for wrapper in node.wrapped_by:
            backrefs.append(EntityBackref(referrer=wrapper, kind="unwrap"))

        return backrefs

    async def _build_tmp_arena_copying_from_external_arena(
        self, entity_id: FxtrID, source: ReadableArena
    ) -> MemoryArena:
        """Build a new temporary arena that contains an entity copied from an external arena."""
        with contextlib.ExitStack() as stack:
            tmp_source_arena = source.copy_to_new_arena([entity_id])
            stack.callback(tmp_source_arena.close)
            tmp_dest_arena = self.new_empty_arena()
            stack.callback(tmp_dest_arena.close)

            worklist: list[_DiscoverWork | _SaveWork] = [_DiscoverWork(entity_id=entity_id)]
            logger.debug(f"  Copying entity {entity_id.summary()} from {source} to {self}")

            while worklist:
                item = worklist.pop()
                if isinstance(item, _DiscoverWork):
                    if item.entity_id in self._entity_states:
                        # We have this one
                        tmp_dest_arena._track_from_store(item.entity_id)
                        logger.debug(
                            f"  Discover({item.entity_id.summary()}): already in store, adding to"
                            " tmp arena"
                        )
                    else:
                        # We don't have this one, so we need to walk its children
                        cbor_bytes, deps = await fetch_data_and_deps_from_arena(
                            item.entity_id, tmp_source_arena
                        )
                        worklist.append(
                            _SaveWork(entity_id=item.entity_id, cbor=cbor_bytes, deps=deps)
                        )
                        for dep_id, _ in deps:
                            worklist.append(_DiscoverWork(entity_id=dep_id))
                        logger.debug(f"  Discover({item.entity_id.summary()}): not found")
                        logger.debug(
                            "    -> push"
                            f" Discover({[dep_id.summary() for dep_id, _ in deps[::-1]]}),"
                            f" Save({item.entity_id.summary()})"
                        )

                elif isinstance(item, _SaveWork):
                    # We have all the deps, so we can save the entity.
                    cbor_bytes = item.cbor
                    requires: set[FxtrID] = set()
                    strong_linked_refs: set[FxtrID] = set()
                    unwraps_to: FxtrID | None = None
                    ctx_node: FxtrID | None = None
                    backstep_node: FxtrID | None = None
                    for dep, kind in item.deps:
                        match kind:
                            case DepKind.STRONG_REF:
                                strong_linked_refs.add(dep)
                                requires.add(dep)
                            case DepKind.WEAK_REF:
                                requires.add(dep)
                            case DepKind.CTX_NODE:
                                assert ctx_node is None
                                ctx_node = dep
                            case DepKind.BACKSTEP:
                                assert backstep_node is None
                                backstep_node = dep
                            case DepKind.UNWRAPS_TO:
                                assert unwraps_to is None
                                unwraps_to = dep
                            case _:
                                raise ValueError(f"Unexpected dependency kind: {kind}")

                    if cbor_bytes is not None:
                        self._backing_store.save_and_increment(
                            item.entity_id._raw_data_id, cbor_bytes
                        )
                        assert ctx_node is None
                        assert backstep_node is None
                        new_node = StoredNode(
                            my_id=item.entity_id,
                            requires=frozenset(requires),
                            strong_linked_refs=frozenset(strong_linked_refs),
                            unwraps_to=unwraps_to,
                        )
                    else:
                        match item.entity_id.get_tag():
                            case "c" | "p":
                                assert ctx_node is not None
                                assert unwraps_to is not None
                                assert not requires
                                new_node = DerivedNode(
                                    my_id=item.entity_id,
                                    ctx_node=ctx_node,
                                    requires=(
                                        frozenset({ctx_node, backstep_node})
                                        if backstep_node is not None
                                        else frozenset({ctx_node})
                                    ),
                                    unwraps_to=unwraps_to,
                                    backstep_node=backstep_node,
                                )
                            case "u":
                                assert unwraps_to is not None
                                assert not requires
                                assert not ctx_node
                                assert not backstep_node
                                new_node = DerivedNode(
                                    my_id=item.entity_id,
                                    requires=frozenset({}),
                                    unwraps_to=unwraps_to,
                                )
                            case "." | "!":
                                assert not requires
                                assert not ctx_node
                                assert not backstep_node
                                assert not unwraps_to
                                new_node = DerivedNode(my_id=item.entity_id, requires=frozenset({}))
                            case _:
                                raise ValueError(
                                    f"Unexpected entity tag: {item.entity_id.get_tag()}"
                                )
                    self._save_or_upgrade_node(new_node)
                    tmp_dest_arena._track_from_store(item.entity_id)
                    logger.debug(f"  Save({item.entity_id.summary()}): saved to tmp arena")

            return tmp_dest_arena.copy_to_new_arena([entity_id])

    def debug_overview(self) -> Any:
        """Generate a debug overview of the state of the store."""
        raise NotImplementedError("TODO: Not implemented yet")

    def debug_detail(self, node_id: FxtrID) -> Any:
        """Get debug details of a specific node in the store."""
        raise NotImplementedError("TODO: Not implemented yet")

    async def debug_wait_for_dirty(self):
        """Wait for the store to update."""
        await self._debug_dirty.wait()


@dataclasses.dataclass(frozen=True)
class _DiscoverWork:
    entity_id: FxtrID


@dataclasses.dataclass(frozen=True)
class _SaveWork:
    entity_id: FxtrID
    cbor: bytes | None
    deps: list[tuple[FxtrID, DepKind]]


class MemoryArena(ReadWriteArena):
    """An in-memory arena backed by a memory store."""

    def __init__(self, *, store: MemoryStore, key: ArenaKey):
        self._store = store
        self._key = key
        self._closed = False

    # New methods
    def clone(self) -> MemoryArena:
        if self._closed:
            raise ValueError("Cannot clone a closed arena")
        new_arena = self._store.new_empty_arena()
        for node_id in self._store._arenas[self._key].liveset:
            new_arena._track_from_store(node_id)
        return new_arena

    def _liveset(self) -> set[FxtrID]:
        return self._store._arenas[self._key].liveset

    def __repr__(self) -> str:
        if self._closed:
            return f"<MemoryArena store:{id(self._store):x} key={self._key} (closed)>"
        return f"<MemoryArena store:{id(self._store):x} key={self._key}>"

    def unsafe_lazily_attach_from_arena(self, entity_id: FxtrID, source: ReadableArena):
        """Lazily attach an entity from another arena into this arena.

        This method lazily attaches an entity from another arena into this arena, making it possible
        to manipulate as a member of this arena. The attachment is lazy in the sense that, if the
        given entity is not already known to our (in-memory) backing store, we will create a new
        claim derived from the source arena's own backing store, and only load the entity from that
        backing store when it is actually needed by this one.

        If you want to eagerly copy the entity into this arena without maintaining a dependency on
        the source arena, use `copy_from_arena` instead.

        The arena `source` is NOT consumed by this operation; the claim on the entity is transferred
        into a fresh source arena that preserves the claim. However, it is the caller's
        responsibility to ensure that the _backing store_ for the source arena remains open and
        accessible for the lifetime of the entire memory store, not just this memory arena; claims
        from the source arena may be held throughout the lifetime of the memory store. We assume
        that the source arena is law-abiding and represents a persistent claim to the
        entity being attached. If the source arena's backing store cannot actually load the entity
        at the time where it is needed, the corresponding load operation will fail, and this load
        operation could occur much later than the `lazily_attach_from_arena` call.

        Args:
            entity_id: The entity ID to attach. Must be a node that is tracked in the source arena.
            source: The source arena to attach the entity from. Must have the entity tracked at
                the point where this method is called; it can then be closed or abandoned afterward.
                It is the caller's responsibility to close the source arena when no longer needed.
        """
        self._store.bare_lazily_attach_from_arena(entity_id, source)
        self._store.track_in_arena(entity_id, self._key)

    # Inherited methods
    @override
    def close(self):
        self._store.close_arena(self._key)
        self._closed = True

    @override
    def is_tracked(self, node_id: FxtrID) -> bool:
        if self._closed:
            return False
        return node_id in self._store._arenas[self._key].liveset

    def _track_from_store(self, node_id: FxtrID) -> FxtrID:
        self._store.track_in_arena(node_id, self._key)
        return node_id

    def _track_from_store_or_none(self, node_id: FxtrID | None) -> FxtrID | None:
        if node_id is None:
            return None
        self._store.track_in_arena(node_id, self._key)
        return node_id

    def _ensure_all_accessible_for_storing(self, entity: FxtrCbor):
        def _visitor(child_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID):
            if not self.is_tracked(child_id.inner_id):
                raise ValueError(
                    "The entity being stored references another entity that is not tracked in the"
                    f" fxtr arena: {child_id.inner_id}! Entities cannot be stored unless all their"
                    " children are tracked. (Try storing them or copying them from another arena"
                    " first.)"
                )

        visit_ids(entity, _visitor)

    @override
    def store(self, entity: FxtrCbor) -> FxtrID:
        self._ensure_all_accessible_for_storing(entity)
        return self._track_from_store(self._store.bare_store(entity))

    @override
    async def load(self, node_id: FxtrID) -> FxtrCbor:
        if not self.is_tracked(node_id):
            raise ValueError(
                f"Node {node_id} is not tracked in the fxtr arena! Only tracked nodes can be"
                " loaded."
            )
        loaded = await self._store.bare_load(node_id)

        def _visitor(child_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID):
            self._store.track_in_arena(child_id.inner_id, self._key)

        visit_ids(loaded, _visitor)
        return loaded

    @override
    def unwrap_provenance(self, node_id: FxtrID) -> FxtrID | None:
        return self._track_from_store_or_none(self._store.unwrap_provenance(node_id))

    @override
    def track_builtin(self, node_id: FxtrID) -> FxtrID:
        return self._track_from_store(self._store.track_builtin(node_id))

    @override
    def create_c_node(self, context_data: FxtrCbor) -> FxtrID:
        self._ensure_all_accessible_for_storing(context_data)
        return self._track_from_store(self._store.create_c_node(context_data))

    @override
    def c_node_to_context_data(self, c_node_id: FxtrID) -> FxtrID:
        return self._track_from_store(self._store.c_node_to_context_data(c_node_id))

    @override
    def create_p_node(self, pathroot_data: FxtrCbor) -> FxtrID:
        self._ensure_all_accessible_for_storing(pathroot_data)
        return self._track_from_store(self._store.create_p_node(pathroot_data))

    @override
    def p_node_to_pathroot(self, p_node_id: FxtrID) -> FxtrID:
        return self._track_from_store(self._store.p_node_to_pathroot(p_node_id))

    @override
    def step_back_p_ctx(self, p_node_id: FxtrID) -> FxtrID | None:
        return self._track_from_store_or_none(self._store.step_back_p_ctx(p_node_id))

    @override
    def create_u_node(self, wrapped_id: FxtrID) -> FxtrID:
        return self._track_from_store(self._store.create_u_node(wrapped_id))

    @override
    def abandon(self, node_id: FxtrID):
        if not self.is_tracked(node_id):
            raise ValueError(
                f"Node {node_id} is not tracked in the fxtr arena! Only tracked nodes can be"
                " abandoned."
            )
        self._store.untrack_from_arena(node_id, self._key)

    @override
    async def copy_from_arena(self, entity_id: FxtrID, source: ReadableArena):
        if not source.is_tracked(entity_id):
            raise ValueError(
                f"Node {entity_id} is not tracked in the source arena! Only tracked nodes can be"
                " copied from other arenas."
            )
        if isinstance(source, MemoryArena) and source._store is self._store:
            # If the source arena is attached to the same store as this entity, we can simply
            # mark the requested entity as tracked in this arena also.
            self._track_from_store(entity_id)
            return

        # Otherwise, we need to copy the entity data. We do this by copying it into a temporary
        # arena, then binding it to our own.
        tmp_arena = await self._store._build_tmp_arena_copying_from_external_arena(
            entity_id, source
        )
        self._track_from_store(entity_id)
        tmp_arena.close()

    def copy_from_sibling_memory_arena(self, entity_id: FxtrID, source: MemoryArena):
        """Copy an entity from a sibling memory arena into this arena."""
        if source._store is not self._store:
            raise ValueError(
                "copy_from_sibling_memory_arena only supports copying from sibling arenas with the"
                " same store."
            )
        if not source.is_tracked(entity_id):
            raise ValueError(
                f"Node {entity_id} is not tracked in the source arena! Only tracked nodes can be"
                " copied from sibling memory arenas."
            )
        self._track_from_store(entity_id)

    @override
    def copy_to_new_arena(self, node_ids: Collection[FxtrID]) -> MemoryArena:
        """Copy the given nodes to a new arena."""
        for node_id in node_ids:
            if not self.is_tracked(node_id):
                raise ValueError(
                    f"Node {node_id} is not tracked in this arena! Only tracked nodes can be"
                    " preserved in a new arena."
                )
        new_arena = self._store.new_empty_arena()
        for node_id in node_ids:
            new_arena._track_from_store(node_id)
        return new_arena


@dataclasses.dataclass(frozen=True)
class EmittedItems:
    items: tuple[FxtrCbor, ...]
    from_arena: ReadableArena | None


@dataclasses.dataclass(frozen=True)
class EmittedSubstream:
    substream: MemoryStreamSink


def _make_copy_from_arena[
    T: FxtrCbor
](objects: T, from_arena: ReadableArena | None) -> tuple[T, ReadableArena | None]:
    """Make a new arena that guarantees access to the given objects, if needed."""
    required_ids: set[FxtrID] = set()

    def _visitor(item: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID):
        required_ids.add(item.inner_id)
        return item.inner_id

    objects = typing.cast(T, visit_and_project_ids(objects, _visitor))

    if required_ids:
        if from_arena is None:
            raise ValueError(
                "`from_arena` is required when making a reserved arena for objects that contain"
                " entities!"
            )
        return objects, from_arena.copy_to_new_arena(required_ids)
    else:
        return objects, None


class MemoryStreamSink(StreamSink):
    """Stream sink that holds all emitted items in memory.

    Generally used with memory arenas, but could in principle be used with any arena. It simply
    holds all emitted items in memory, associating each with an arena that guarantees access to
    them.
    """

    def __init__(self, stream_key: FxtrID | None = None, from_arena: ReadableArena | None = None):
        if stream_key is None:
            self._stream_key = (None, None)
        else:
            if from_arena is None:
                raise ValueError(
                    "`from_arena` is required when creating a stream sink with a stream key!"
                )
            self._stream_key = (stream_key, from_arena.copy_to_new_arena([stream_key]))
        self._emitted: list[EmittedItems | EmittedSubstream] = []
        self._is_finished = False
        self._final: tuple[FxtrID, ReadableArena] | None = None

    @override
    def emit(self, *items: FxtrCbor, from_arena: ReadableArena | None = None):
        if self._is_finished:
            raise ValueError("Cannot emit items to a finished stream!")
        items_copy, from_arena_copy = _make_copy_from_arena(list(items), from_arena)
        logger.debug(
            f"Emitting items to stream {id(self):x}: {items_copy} (from arena: {from_arena_copy})"
        )
        self._emitted.append(EmittedItems(items=tuple(items_copy), from_arena=from_arena_copy))

    @override
    def finish_with_result(self, result: FxtrID, from_arena: ReadableArena):
        if self._is_finished:
            raise ValueError("Cannot emit items to a finished stream!")

        self._final = (result, from_arena.copy_to_new_arena([result]))
        self._is_finished = True
        self._emitted.clear()
        logger.debug(
            f"Finishing stream {id(self):x} with result {result} (from arena: {from_arena})"
        )

    @override
    def make_substream_sink(
        self, stream_key: FxtrID | None = None, from_arena: ReadableArena | None = None
    ) -> MemoryStreamSink:
        if self._is_finished:
            raise ValueError("Cannot create a substream sink on a finished stream!")
        substream = MemoryStreamSink(stream_key=stream_key, from_arena=from_arena)
        self._emitted.append(EmittedSubstream(substream=substream))
        logger.debug(
            f"Stream {id(self):x} spawned substream {id(substream):x} with key {stream_key} (from"
            f" arena: {from_arena})"
        )
        return substream

    @override
    def is_finished(self) -> bool:
        return self._is_finished
