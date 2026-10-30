"""Fxtr scopes, which govern the lifetime of retrievable IDs.

A fxtr scope is a context within which entities can be saved into IDs and retrieved again later,
with guarantees that, under normal program operation:
- Anything saved into a scope is retrievable from that scope later.
- Any children of something retrieved from the scope are also retrievable from the scope.
- If scope B is a child of scope A, then anything retrievable in scope A is also retrievable in
  scope B.
- After a scope ends, the scope cannot be used to retrieve any entities.
- If scope B is a child of scope A, then scope A does not end until AFTER scope B ends; scope B
  keeps scope A alive.

Scopes are a higher-level abstraction around the lower-level arena concept as well as the memory
arena inheritance system. They make it possible to manipulate data based on lifetimes without
needing to explicitly manage arena state.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import logging
from collections.abc import Sequence
from typing import Any, AsyncGenerator, Callable, Generator, Self

import anyio

from . import arena_util, memory_arena
from .entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID

GLOBAL_BACKING_STORE = contextvars.ContextVar[
    memory_arena.LMDBBytesBackingStore | memory_arena.InMemoryBytesBackingStore | None
]("fxtr_scope.GLOBAL_BACKING_STORE", default=memory_arena.InMemoryBytesBackingStore())
"""A global shared backing store used by all new scopes."""

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _KeepAliveWaiter:
    """A waiter for a keep-alive event."""

    done_evt: anyio.Event
    on_unexpected_close: Callable[[BaseException], None] | None

    def notify_unexpected_close(self, exc: BaseException) -> None:
        """Set the unexpected close event."""
        if self.on_unexpected_close is not None:
            self.on_unexpected_close(exc)


class _KeepAlive:
    """An object that allows other async tasks to keep this object alive."""

    def __init__(self):
        self._shutdown_requested = anyio.Event()
        self._waiters: set[_KeepAliveWaiter] = set()

    @contextlib.contextmanager
    def keep_alive(
        self, on_unexpected_close: Callable[[BaseException], None] | None = None
    ) -> Generator[None, None, None]:
        """Keep this object alive during a particular scope.

        Args:
            on_unexpected_close: A callback to call if the scope is unexpectedly closed. Will
                be called with the exception that caused the scope to be closed. The caller
                should NOT re-raise this exception; it is provided for information only.
        """
        evt = anyio.Event()
        if self._shutdown_requested.is_set():
            raise RuntimeError("Scope has already been shut down")
        waiter = _KeepAliveWaiter(evt, on_unexpected_close)
        self._waiters.add(waiter)
        try:
            yield
        finally:
            self._waiters.discard(waiter)
            waiter.done_evt.set()

    async def shutdown(self) -> None:
        """Start shutdown process and wait for all tasks to complete."""
        self._shutdown_requested.set()
        try:
            while self._waiters:
                waiter = self._waiters.pop()
                try:
                    await waiter.done_evt.wait()
                except BaseException as exc:
                    waiter.notify_unexpected_close(exc)
                    raise
        except BaseException as exc:
            while self._waiters:
                waiter = self._waiters.pop()
                waiter.notify_unexpected_close(exc)
            raise


class FxtrScope(anyio.AsyncContextManagerMixin):
    """A scope within which entities can be saved into IDs and retrieved again later."""

    def __init__(
        self, include: Sequence[FxtrScope] = (), isolated: bool = False, name: str | None = None
    ):
        """Create a new scope, including the given scopes as parents.

        Args:
            include: The scopes to include as additional parents of this scope
            isolated: If True, this scope will implicitly include the currently-active scope
                as an additional parent, and will also set itself as the currently-active scope when
                entered as a context manager. If False, this scope will be isolated: it will not
                be set as the currently-active scope when entered, and will not inherit from the
                currently-active scope.
            name: A name for the scope, for debugging purposes.
        """
        self._isolated = isolated
        if not isolated:
            self._outer_scope = ACTIVE_SCOPE.get()
            include = (self._outer_scope, *include)
        else:
            self._outer_scope = None

        self._parents = include
        self._dead_parents: list[FxtrScope] = []
        self._name = name
        self._all_ancestor_scope_ids: set[int] = {id(pscope) for pscope in include}.union(
            {aid for pscope in include for aid in pscope._all_ancestor_scope_ids}
        )

        self._evt_startup = anyio.Event()
        self._evt_shutdown_requested = anyio.Event()
        self._keep_alive = _KeepAlive()
        self._evt_shutdown_complete = anyio.Event()

        self._store: memory_arena.MemoryStore = memory_arena.MemoryStore(
            backing_store=GLOBAL_BACKING_STORE.get()
        )
        self._main_arena = self._store.new_empty_arena()
        self._loads_safe_to_cache: set[FxtrID] = set()

        self._context_marker = self._main_arena.track_builtin(FxtrID.from_tag_and_payload(".", b""))

    def is_alive(self) -> bool:
        return self._evt_startup.is_set() and not self._evt_shutdown_complete.is_set()

    def check_alive(self) -> None:
        if not self._evt_startup.is_set():
            raise RuntimeError("Scope has not been started!")
        if self._evt_shutdown_complete.is_set():
            raise RuntimeError("Scope has already been shut down!")

    @property
    def outer(self) -> FxtrScope:
        if self._isolated:
            raise RuntimeError("Cannot access outer scope of an isolated scope")
        assert self._outer_scope is not None
        return self._outer_scope

    def get_context_marker_id(self) -> FxtrID:
        return self._context_marker

    def __repr__(self) -> str:
        if self._name:
            return f"<FxtrScope {repr(self._name)} at {id(self):x}>"
        else:
            return f"<FxtrScope {id(self):x}>"

    def _start_as_root_scope(self) -> Self:
        """Start this scope as a root scope."""
        if self._evt_startup.is_set():
            raise RuntimeError("Scope has already been started")

        if not self._isolated:
            raise RuntimeError("Root scope must be isolated")

        if self._parents:
            raise RuntimeError("Root scope cannot have parents")

        self._evt_startup.set()
        return self

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        """Enter the scope."""
        if self._evt_startup.is_set():
            raise RuntimeError("Scope has already been started")

        self._evt_startup.set()
        try:
            # Wait for parent contexts and keep them alive while this context is active
            async with contextlib.AsyncExitStack() as stack:
                for parent in self._parents:
                    await parent._evt_startup.wait()
                    if parent._evt_shutdown_complete.is_set():
                        raise RuntimeError(f"Parent scope {parent} has already been shut down!")
                    if parent._evt_shutdown_requested.is_set():
                        raise RuntimeError(f"Parent scope {parent} is already shutting down!")

                    def _handle_close(exc: BaseException, parent=parent) -> None:
                        logger.warning(
                            "Scope %s: Parent scope %s closed unexpectedly (%s)",
                            self,
                            parent,
                            exc,
                        )
                        self._dead_parents.append(parent)

                    stack.enter_context(parent._keep_alive.keep_alive(_handle_close))

                if self._isolated:
                    yield self
                else:
                    parent_scope = ACTIVE_SCOPE.get()
                    if parent_scope != self._outer_scope:
                        raise RuntimeError(
                            f"Scope {self._outer_scope} was active when this scope ({self})"
                            " was constructed, but is no longer active when this scope is entered;"
                            " this is not allowed unless isolated=True was set."
                        )
                    scope_override_token = ACTIVE_SCOPE.set(self)
                    try:
                        logger.debug("Active scope updated from %s to %s", parent_scope, self)
                        yield self
                    finally:
                        ACTIVE_SCOPE.reset(scope_override_token)
                        logger.debug("Active scope updated from %s to %s", self, parent_scope)

        finally:
            self._evt_shutdown_requested.set()
            await self._keep_alive.shutdown()
            self._main_arena.close()
            self._evt_shutdown_complete.set()
            if self._store._arenas:
                logger.warning(
                    "Scope %s has %d remaining arenas open in its store at shutdown; arenas should"
                    " not generally outlive their scope during normal operation.",
                    self,
                    len(self._store._arenas),
                )
            else:
                self._store.close()

    @contextlib.contextmanager
    def as_active_scope(self) -> Generator[None, None, None]:
        """Context manager that temporarily sets this scope as the active scope.

        Note that non-isolated scopes are automatically set as the active scope during their
        lifetime in the task where they are created. This method can be used to temporarily override
        the active scope for a particular block of code.
        """
        self.check_alive()
        scope_override_token = ACTIVE_SCOPE.set(self)
        try:
            with self._keep_alive.keep_alive(
                on_unexpected_close=lambda exc: logger.warning(
                    "Scope %s: Unexpected close of active scope during as_active_scope context"
                    " manager (%s)",
                    self,
                    exc,
                )
            ):
                yield
        finally:
            ACTIVE_SCOPE.reset(scope_override_token)

    def included_in_active_scope(self) -> bool:
        """Check if this scope is included in the active scope.

        If this returns True, then any IDs in this scope can be loaded with a bare .load() call
        in the active scope, and can also be lifted there. If False, then IDs in this scope may not
        be automatically loadable in the active scope, and must be explicitly copied out.
        """
        return self.included_in_scope(ACTIVE_SCOPE.get())

    def included_in_scope(self, scope: FxtrScope) -> bool:
        """Check if this scope is included in the given scope.

        If this returns True, then any IDs in this scope can be loaded into the given scope, and
        can also be lifted there. If False, then IDs in this scope may not be automatically loadable
        into the given scope, and must be explicitly copied out.

        A scope is always included in itself.

        Args:
            scope: The scope to check inclusion in.

        Returns:
            True if this scope is included in the given scope, False otherwise.
        """
        return self is scope or id(self) in scope._all_ancestor_scope_ids

    def is_tracked(self, node: FxtrID | entity_serdes.EntityRef[Any]) -> bool:
        """Check if a node is tracked in this scope."""
        if isinstance(node, entity_serdes.EntityRef):
            node_id = node.ref
        else:
            node_id = node
        return self._main_arena.is_tracked(node_id)

    def _find_owning_parent_arena(self, node_id: FxtrID) -> memory_arena.MemoryArena | None:
        """Find an arena that we could use to load the given node.

        Args:
            node_id: The node to lift.

        Returns:
            An arena reachable from the parent scopes that contains the node. Note: DO NOT use this
            arena to load the node, instead always clone it into a new arena first.
        """
        stack: list[FxtrScope] = [self]
        seen = set[int]([id(self)])
        while stack:
            scope = stack.pop()
            for parent in scope._parents:
                if id(parent) not in seen:
                    if parent.is_tracked(node_id):
                        return parent._main_arena
                    seen.add(id(parent))
                    stack.append(parent)
        return None

    def lift[IDOrRef: (FxtrID, entity_serdes.EntityRef[Any])](self, node: IDOrRef) -> IDOrRef:
        """Ensure that a node is tracked in this scope, assuming it was tracked in some parent.

        After `lift` is called, the node will remain tracked in this scope, even if it is abandoned
        from the parent scope.

        Args:
            node_id: The node to lift.

        Returns:
            The same node ID, for convenience.
        """
        self.check_alive()

        if isinstance(node, entity_serdes.EntityRef):
            node_id = node.ref
        else:
            node_id = node

        if self.is_tracked(node_id):
            return node

        parent_arena = self._find_owning_parent_arena(node_id)
        if parent_arena:
            # Found it in a parent main arena. Bridge it into this main arena. (Note that
            # unsafe_lazily_attach_from_arena will derive and hold onto its own reference to the
            # node.)
            self._main_arena.unsafe_lazily_attach_from_arena(node_id, parent_arena)
            return node

        raise ValueError(f"Node {node_id} was not found in any parent scope!")

    async def load_cbor(self, node_id: FxtrID) -> FxtrCbor:
        """Load an entity from the scope."""
        result = await self._main_arena.load(self.lift(node_id))
        # It's OK to cache a load of this node until we remove something from the liveset.
        self._loads_safe_to_cache.add(node_id)
        return result

    def abandon(self, node_id: FxtrID | entity_serdes.EntityRef[Any]) -> None:
        """Abandon an entity from the scope."""
        self.check_alive()
        if isinstance(node_id, entity_serdes.EntityRef):
            node_id = node_id.ref

        if not self.is_tracked(node_id):
            raise ValueError(f"Node {node_id} is not tracked in this scope!")
        self._main_arena.abandon(node_id)
        # Any time any node is abandoned, we clear the arena cache of all loads. This is a
        # conservative measure to ensure that any IDs that would become tracked during a load
        # are not accidentally re-used without explicitly tracking them again.
        # (This could be optimized if needed. However, we expect `abandon` to be rare in user code
        # and the actual filesystem operations may be cached in a memory store regardless, in which
        # case the main overhead is deserialization overhead.)
        self._loads_safe_to_cache.clear()

    def store_cbor(self, cbor: FxtrCbor) -> FxtrID:
        """Store an entity into the scope."""
        self.check_alive()
        arena_util.visit_ids(cbor, lambda fid: self.lift(fid.inner_id))
        return self._main_arena.store(cbor)

    def track_cached_store_cbor(
        self, entity_thunk: Callable[[], FxtrCbor], cached_id: FxtrID
    ) -> None:
        """Track a cached store operation.

        This method indicates that an entity is being stored, but the caller already knows its ID.
        The scope should track this entity as if it was stored in the current arena, but if the
        entity is already known to the scope, the scope can optimize its tracking.

        Args:
            entity_thunk: A thunk that returns the entity to store. Assumed to match the cached ID.
            cached_id: The ID of the entity that is already known to the store. If the store already
                knows about this entity, it can freely ignore the `entity_thunk` argument.
        """
        self.check_alive()
        if self.is_tracked(cached_id):
            # We already know about the entity that is claimed to be stored. We will trust the
            # caller that the entity data is still the same.
            return
        entity = entity_thunk()
        arena_util.visit_ids(entity, lambda fid: self.lift(fid.inner_id))
        node_id = self._main_arena.store(entity)
        if node_id != cached_id:
            raise ValueError(
                f"Cached ID {cached_id} does not match ID {node_id} from storing the entity!"
                " Possibly mutation after caching?"
            )

    def can_use_cached_load(self, node_id: FxtrID) -> bool:
        """Check if all invariants are met such that we can reuse the result of a load operation.

        This method indicates that an entity is being loaded, but the caller already knows its data.
        The scope will return True if it can determine that loading the data again would not change
        would not change the tracked state of the scope, and thus that all invariants maintained
        during a load are still valid.

        If `can_use_cached_load` returns True, all IDs inside the loaded entity are already tracked
        by the scope. In this case, it is assumed that the caller has actually saved the loaded
        entity data somewhere and that it has not been mutated.

        If `can_use_cached_load` returns False, the caller should use the async `load` method to
        load the entity data again to ensure that it is tracked correctly.

        If `load` was previously called successfully for this node in this scope, and no nodes have
        been abandoned since `load` was called, then this method is required to return True. (This
        ensures callers can safely cache the results of load operations run sequentially.)

        It is the caller's responsibility to ensure that any cached data they are storing is not
        mutated if they plan to use it as the result of a load operation.

        Args:
            node_id: The canonical identifier for the entity being loaded.

        Returns:
            True if all invariants are met such that the results of loading this node can be re-used
            without calling `load` again. False if the caller should call `load` again to ensure
            that the invariants are met.
        """
        self.check_alive()
        return node_id in self._loads_safe_to_cache

    async def load[T: entity_serdes.Entity](self, ref: entity_serdes.EntityRef[T]) -> T:
        """Load an entity from the scope.

        Convenience wrapper around entity_serdes.EntityRef.load.
        """
        return await ref.load(scope=self)

    async def deep_load[T: entity_serdes.Entity](self, thunk: Callable[[], T]) -> T:
        """Load all EntityRefs whose .val are accessed by a thunk, calling it repeatedly if necessary.

        Convenience wrapper around entity_serdes.deep_load.
        """
        return await entity_serdes.deep_load(thunk, scope=self)

    async def store[
        T: entity_serdes.Entity
    ](self, entity: T, as_type: type[T] | None = None) -> entity_serdes.EntityRef[T]:
        """Save an entity into the scope.

        Convenience wrapper around entity_serdes.store_entity.
        """
        return entity_serdes.store_entity(entity, as_type=as_type, scope=self)

    async def copy_from_scope[
        T: entity_serdes.FxtrSendable
    ](self, value: T, source_scope: FxtrScope | None = None) -> T:
        """Copy an ID or group of IDs into this scope from another (by default the active scope).

        Does not actually copy the object, but instead copies data referenced by all IDs reachable
        from the object. Can be used to extract entities from the active scope into a different
        scope.

        Args:
            value: The value to copy.
            source_scope: The scope to copy the value from. Defaults to the active scope.

        Returns:
            The copied value itself, for convenience.
        """
        self.check_alive()
        if source_scope is None:
            source_scope = ACTIVE_SCOPE.get()

        if isinstance(value, FxtrID):
            await self._main_arena.copy_from_arena(value, source_scope._main_arena)
            return value

        await entity_serdes.async_visit_ids_in(
            value, lambda fid: self._main_arena.copy_from_arena(fid, source_scope._main_arena)
        )
        return value

    def lift_all[T: entity_serdes.FxtrSendable](self, value: T) -> T:
        """Lift all IDs in a value to this scope."""
        entity_serdes.visit_ids_in(value, lambda fid: self.lift(fid))
        return value

    def track_builtin(self, builtin_id: FxtrID) -> FxtrID:
        """Track a builtin ID in this scope."""
        return self._main_arena.track_builtin(builtin_id)


# Note: calling FxtrScope with isolated=True avoids us circularly asking for ACTIVE_SCOPE while
# setting it up.
GLOBAL_SCOPE = FxtrScope(isolated=True)._start_as_root_scope()
"""The global scope, where top-level entities (e.g. type definitions) are stored."""

ACTIVE_SCOPE = contextvars.ContextVar[FxtrScope]("fxtr_scope.ACTIVE_SCOPE", default=GLOBAL_SCOPE)
"""The current scope where operations execute by default."""


def get_active_context_id() -> FxtrID:
    """Get the current context ID for the active scope."""
    return ACTIVE_SCOPE.get().get_context_marker_id()


def get_active_context_marker() -> RawFxtrEntityMarkerID:
    """Get the current context marker for the active scope."""
    return RawFxtrEntityMarkerID(inner_id=get_active_context_id())


def track_builtin_id(name: str) -> FxtrID:
    """Track a builtin ID in the active scope."""
    return ACTIVE_SCOPE.get()._main_arena.track_builtin(FxtrID.builtin(name))


# Circular import at end of file; needed for convenience wrappers.
from . import entity_serdes
