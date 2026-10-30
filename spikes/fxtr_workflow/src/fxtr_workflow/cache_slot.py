"""Cache slots: persistent state containers for step runs."""

from __future__ import annotations

import abc
import dataclasses
import enum
from typing import TYPE_CHECKING, Any

import anyio

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_workflow.entities import (
    ForeignSlotRef,
    FrozenInternalSlots,
    FrozenSlotNode,
    InnerSlotArgMapping,
    InternalSlotRef,
    SlotFinished,
    SlotSplit,
    SlotState,
    StepCheckpoint,
    TrackedForeignRun,
    TrackedForeignSlotDeps,
)
from fxtr_workflow.errors import CacheSlotError
from fxtr_workflow.resolve import collect_cache_slots
from fxtr_workflow.step_handle import StepHandle

if TYPE_CHECKING:
    from fxtr2.data.fxtr_context_api import FxtrNamespace
    from fxtr_workflow.pending import Pending


# ---------------------------------------------------------------------------
# CacheSlot ABC
# ---------------------------------------------------------------------------


class CacheSlot(abc.ABC):
    """Abstract cache slot interface."""

    @abc.abstractmethod
    def run(
        self,
        step: StepHandle,
        *args: Any,
        **kwargs: Any,
    ) -> Pending[Any]:
        """Run a step inside this cache slot.

        Synchronous: spawns the step as a background task and immediately
        returns a Pending that can be awaited for the result.

        Concurrency rules for the same slot:

        - **Same cache key**: If a second ``run`` is dispatched while the first
          is still executing, the second blocks until the first completes,
          then does the normal cache-replay check and reuses the result.
          The step function is only called once.
        - **Different cache key**: If a second ``run`` is dispatched with a
          different cache key (different step or args), it raises
          ``CacheSlotError`` (write-once violation), regardless of whether
          the first run is still in progress or already finished.
        """
        ...

    @abc.abstractmethod
    def run_fallible(
        self,
        step: StepHandle,
        *args: Any,
        **kwargs: Any,
    ) -> Pending[Any]:
        """Run a step inside this cache slot, caching errors as valid outcomes.

        Like run(), but exceptions are cached in the slot and re-raised as
        FallibleRunError on replay. Cache-slot args are not allowed.
        """
        ...

    @abc.abstractmethod
    def subslot(self, key: str | int) -> CacheSlot:
        """Access a subslot of this slot.

        Int keys are normalized to their string form — subslot(42) and
        subslot("42") return the same slot.
        """
        ...


# ---------------------------------------------------------------------------
# Replay reference types
# ---------------------------------------------------------------------------


class ReplayMode(enum.Enum):
    """How to handle mismatches between a cache slot and its replay reference."""

    STRICT = "strict"
    BEST_EFFORT = "best_effort"


class RunReplayReference(abc.ABC):
    """Protocol for providing internal cache slot replay references.

    Implemented by FrozenStepContext (for Finished reference state) and
    LiveRunReference (for Partial reference state).
    """

    @abc.abstractmethod
    def get_internal_replay_ref(self, key: str) -> FrozenCacheSlot | LiveCacheSlot | None:
        """Get the replay reference for an internal cache slot by short key."""
        ...


class LiveRunReference(RunReplayReference):
    """Provides internal cache slot replay references from a Partial reference's live pins.

    When the reference slot is in Partial state (not Finished), there's no
    FrozenInternalSlots. Instead, internal cache slots are navigated via live
    /c/ pins in the reference namespace.
    """

    def __init__(
        self,
        reference_slot: LiveCacheSlot,
        namespace: FxtrNamespace,
        backend: _Backend,
    ) -> None:
        self._reference_slot = reference_slot
        self._namespace = namespace
        self._backend = backend

    def get_internal_replay_ref(self, key: str) -> LiveCacheSlot | None:
        pin_key = f"{self._reference_slot.pin_key}/c/{key}"
        return LiveCacheSlot(self._namespace, pin_key, self._backend, is_internal=True)


# ---------------------------------------------------------------------------
# Replay reference config + slot lifecycle states
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ReplayRefConfig:
    """Immutable bundle of replay reference configuration."""

    replay_ref: FrozenCacheSlot | LiveCacheSlot
    replay_mode: ReplayMode
    replay_ref_namespace: FxtrNamespace


@dataclasses.dataclass
class _SlotFresh:
    """Not loaded yet."""

    ref: ReplayRefConfig | None


@dataclasses.dataclass
class _SlotLocalEmpty:
    """Loaded from namespace, nothing there. Reference not yet consulted."""

    ref: ReplayRefConfig | None


@dataclasses.dataclass
class _SlotProbedFromRef:
    """Local empty, reference had state, awaiting realize/reject."""

    ref: ReplayRefConfig
    probed: EntityRef[SlotState]


@dataclasses.dataclass
class _SlotRefRejected:
    """Reference rejected (best-effort). Local empty. Ref retained for child propagation."""

    ref: ReplayRefConfig


@dataclasses.dataclass
class _SlotLocalPresent:
    """Has local state (loaded, realized, or written).

    state_ref is loadable in load_scope (always namespace.scope — the
    namespace's backing store has the entity and all its transitive deps
    from the pin operation).
    """

    state_ref: EntityRef[SlotState]
    load_scope: FxtrScope
    ref: ReplayRefConfig | None


_SlotLifecycle = (
    _SlotFresh | _SlotLocalEmpty | _SlotProbedFromRef | _SlotRefRejected | _SlotLocalPresent
)


# ---------------------------------------------------------------------------
# LiveCacheSlot: the real implementation backed by a namespace
# ---------------------------------------------------------------------------


class LiveCacheSlot(CacheSlot):
    """A cache slot backed by persistent storage in a fxtr namespace."""

    def __init__(
        self,
        namespace: FxtrNamespace,
        pin_key: str,
        backend: _Backend,
        parent: LiveCacheSlot | None = None,
        is_internal: bool = False,
        ref_config: ReplayRefConfig | None = None,
    ) -> None:
        self._namespace = namespace
        self._pin_key = pin_key
        self._backend = backend
        self._parent = parent
        self._is_internal = is_internal
        self._lock = anyio.Lock()
        self._execution_finished: anyio.Event | None = None
        self._subslots: dict[str, LiveCacheSlot] = {}
        self._lifecycle: _SlotLifecycle = _SlotFresh(ref=ref_config)

    @property
    def pin_key(self) -> str:
        return self._pin_key

    @property
    def state_ref(self) -> EntityRef[SlotState] | None:
        """The EntityRef for this slot's current state, or None if not locally present."""
        lc = self._lifecycle
        if isinstance(lc, _SlotLocalPresent):
            return lc.state_ref
        return None

    @property
    def ref_config(self) -> ReplayRefConfig | None:
        """The replay reference config, available from any lifecycle state."""
        return self._lifecycle.ref  # type: ignore[union-attr]

    @property
    def _namespace_scope(self) -> FxtrScope:
        """The namespace's scope — canonical long-lived scope for all persisted state."""
        return self._namespace.scope

    async def _load_state(self) -> SlotState | None:
        """Load the current state from the namespace. Returns None for Empty.

        Transitions: SlotFresh → SlotLocalEmpty or SlotLocalPresent.

        Retrieves into namespace.scope so that the entity and its transitive
        deps (stored by pin) are loadable from a long-lived scope that is an
        ancestor of all working scopes.
        """
        lc = self._lifecycle
        if isinstance(lc, _SlotFresh):
            # Retrieve into namespace.scope — the namespace's backing store
            # has the entity data (from a prior pin), and the namespace's
            # arena can serve it lazily.
            ns_scope = self._namespace_scope
            ref_entity = await self._namespace.try_retrieve(
                self._pin_key, type=SlotState, into_scope=ns_scope
            )
            if ref_entity is not None:
                self._lifecycle = _SlotLocalPresent(
                    state_ref=ref_entity, load_scope=ns_scope, ref=lc.ref
                )
            else:
                self._lifecycle = _SlotLocalEmpty(ref=lc.ref)
            lc = self._lifecycle
        if isinstance(lc, _SlotLocalPresent):
            # Load from the scope where the entity was retrieved into.
            return await lc.state_ref.load(scope=lc.load_scope)
        return None

    async def _ensure_split(self, *, store_scope: FxtrScope) -> None:
        """Ensure this slot is in Split state for use as a subslot parent.

        Strictly requires Split (or Empty, which transitions to Split).
        Partial/Finished are integrity violations — a slot cannot be used
        for both a run and a subslot tree.

        If a replay reference is present, uses probe/realize/reject:
        - Reference is Split → realize (copy Split state from reference)
        - Otherwise → reject (structural mismatch)

        Args:
            store_scope: Scope to store the SlotSplit entity in before pinning.
        """
        state = await self._load_state()
        if isinstance(state, SlotSplit):
            return
        if state is not None:
            raise CacheSlotError(
                f"Cannot use slot '{self._pin_key}' for subslots: "
                f"already has state {type(state).__name__}"
            )
        # Empty — check replay reference
        lc = self._lifecycle
        if isinstance(lc, _SlotLocalEmpty) and lc.ref is not None:
            probed_ref = await self.probe_state()
            if probed_ref is not None:
                probed_state = await probed_ref.load()
                if isinstance(probed_state, SlotSplit):
                    await self.realize(probed_ref, into_scope=store_scope)
                    return
                else:
                    self.reject(reason="structural_mismatch")
                    # strict: raised. best-effort: fall through to create fresh Split.
        # Empty → Split (ensure ancestors first, but only up through subslot links)
        if self._parent is not None and not self._is_internal:
            await self._parent._ensure_split(store_scope=store_scope)
        # SlotSplit has no children, so any scope works for storing it.
        await self._persist_state(SlotSplit(), store_scope=store_scope)

    async def _persist_state(
        self, state: SlotState, *, store_scope: FxtrScope
    ) -> EntityRef[SlotState]:
        """Low-level: persist state to the namespace without checking ancestors.

        Transitions any state → SlotLocalPresent.

        Stores the entity in store_scope, pins to the namespace (which
        transitively persists all deps to the backing store), then retrieves
        into namespace.scope so the entity and its children are loadable from
        the long-lived namespace scope.

        Args:
            store_scope: Scope where the state entity (and its children) are
                currently stored. Pin reads from this scope's arena.
        """
        # Store in the provided scope. The state entity's children (e.g.
        # cache_key_ref, result_ref for SlotFinished) must already be stored
        # in store_scope — callers are responsible for this.
        ref = state.store(store_scope)
        # Pin transitively persists the entity and ALL its transitive deps
        # (including EntityRef targets like StepOutput) to the namespace's
        # backing store.
        await self._namespace.pin(self._pin_key, ref, overwrite=True, scope=store_scope)
        # Retrieve into namespace.scope so the entity is loadable from any
        # future scope that has namespace.scope as an ancestor. The namespace's
        # arena can lazily serve all transitive deps from the backing store.
        ns_scope = self._namespace_scope
        ns_ref = await self._namespace.retrieve(self._pin_key, type=SlotState, into_scope=ns_scope)
        self._lifecycle = _SlotLocalPresent(
            state_ref=ns_ref, load_scope=ns_scope, ref=self.ref_config
        )
        return ns_ref

    async def _save_state(
        self, state: SlotState, *, store_scope: FxtrScope
    ) -> EntityRef[SlotState]:
        """Save state to the namespace, returning the EntityRef.

        For subslot children, ensures ancestor slots are in Split state first.
        For internal cache slot children, skips the check — the parent's
        Partial state is established by the execution lifecycle.

        Args:
            store_scope: Scope where the state entity is stored. Passed through
                to _persist_state for pinning.
        """
        if self._parent is not None and not self._is_internal:
            await self._parent._ensure_split(store_scope=store_scope)
        return await self._persist_state(state, store_scope=store_scope)

    def run(
        self,
        step: StepHandle,
        *args: Any,
        **kwargs: Any,
    ) -> Pending[Any]:
        """Run a step inside this cache slot."""
        return self._backend.dispatch_in_slot(self, step, args, kwargs)

    def run_fallible(
        self,
        step: StepHandle,
        *args: Any,
        **kwargs: Any,
    ) -> Pending[Any]:
        """Run a step, caching errors as valid outcomes. No cache-slot args allowed."""
        slots = collect_cache_slots(args, kwargs)
        if slots:
            raise TypeError(
                "run_fallible does not allow cache slot arguments. "
                f"Found {len(slots)} cache slot(s) in args/kwargs."
            )
        return self._backend.dispatch_in_slot(self, step, args, kwargs, fallible=True)

    def subslot(self, key: str | int) -> LiveCacheSlot:
        """Access a subslot. Split state is persisted lazily when the subslot first writes data.

        Propagates replay references from any lifecycle state that carries ref config.
        """
        key = str(key)
        if key in self._subslots:
            return self._subslots[key]
        child_key = f"{self._pin_key}/s/{key}"
        ref_config = self.ref_config
        child_ref_config: ReplayRefConfig | None = None
        if ref_config is not None:
            replay_ref = ref_config.replay_ref
            if isinstance(replay_ref, FrozenCacheSlot):
                child_replay_ref = replay_ref.child_subslot(key)
            else:
                child_replay_ref = replay_ref.subslot(key)
            child_ref_config = ReplayRefConfig(
                replay_ref=child_replay_ref,
                replay_mode=ref_config.replay_mode,
                replay_ref_namespace=ref_config.replay_ref_namespace,
            )
        child = LiveCacheSlot(
            self._namespace,
            child_key,
            self._backend,
            parent=self,
            ref_config=child_ref_config,
        )
        self._subslots[key] = child
        return child

    # ------------------------------------------------------------------
    # Replay reference: probe / realize / reject
    # ------------------------------------------------------------------

    async def probe_state(self) -> EntityRef[SlotState] | None:
        """Check this slot's state, consulting replay reference if empty.

        Transitions:
        - SlotLocalEmpty(ref) → SlotProbedFromRef if ref has state
        - SlotLocalPresent → stays, returns state_ref
        - SlotRefRejected → stays, returns None
        """
        lc = self._lifecycle
        # Ensure loaded
        if isinstance(lc, _SlotFresh):
            await self._load_state()
            lc = self._lifecycle

        if isinstance(lc, _SlotLocalPresent):
            return lc.state_ref

        if isinstance(lc, _SlotLocalEmpty) and lc.ref is not None:
            replay_ref = lc.ref.replay_ref
            if isinstance(replay_ref, FrozenCacheSlot):
                ref = await replay_ref.load_state_ref()
            else:
                await replay_ref._load_state()
                ref = replay_ref.state_ref
            if ref is not None:
                self._lifecycle = _SlotProbedFromRef(ref=lc.ref, probed=ref)
                return ref

        # SlotLocalEmpty(ref=None), SlotRefRejected, or ref was empty
        return None

    async def _realize_inner(
        self, expected_ref: EntityRef[SlotState], *, into_scope: FxtrScope
    ) -> None:
        """Core realize logic. Caller must ensure no concurrent access.

        Transitions: SlotProbedFromRef → SlotLocalPresent.
        If already SlotLocalPresent (local state), no-op.

        Copies the entity from the replay reference namespace into into_scope,
        then pins it into our namespace. After pin+retrieve, the entity is
        loadable from namespace.scope.

        Args:
            into_scope: Scope to copy the entity into before pinning. Must be
                a live scope that can accept copy_from_scope. Typically the
                working scope during cache resolution.
        """
        lc = self._lifecycle
        if isinstance(lc, _SlotLocalPresent):
            return  # Local state — nothing to copy

        assert isinstance(
            lc, _SlotProbedFromRef
        ), f"_realize_inner() requires SlotProbedFromRef, got {type(lc).__name__}"
        assert (
            lc.probed.entity_id == expected_ref.entity_id
        ), "realize() called with stale expected_ref"

        # Copy from reference namespace scope into the provided scope.
        # The entity is in the replay ref namespace's scope (loaded there
        # during probe_state).
        await into_scope.copy_from_scope(expected_ref.entity_id, lc.ref.replay_ref_namespace.scope)
        # Pin into our namespace from into_scope — transitively persists deps.
        await self._namespace.pin(self._pin_key, expected_ref, overwrite=True, scope=into_scope)
        # Retrieve into namespace.scope for long-lived loadability.
        ns_scope = self._namespace_scope
        ns_ref = await self._namespace.retrieve(self._pin_key, type=SlotState, into_scope=ns_scope)
        self._lifecycle = _SlotLocalPresent(state_ref=ns_ref, load_scope=ns_scope, ref=lc.ref)

    async def realize(self, expected_ref: EntityRef[SlotState], *, into_scope: FxtrScope) -> None:
        """Commit to the probed state, acquiring the slot's lock.

        Use this when calling from outside the slot's lock (e.g. replay check
        on foreign slots). For callers that already hold the lock (e.g.
        _execute_in_slot), use _realize_inner() directly.

        Args:
            into_scope: Scope to copy the entity into before pinning.
        """
        async with self._lock:
            await self._realize_inner(expected_ref, into_scope=into_scope)

    def reject(self, reason: str) -> None:
        """The probed state doesn't match what the caller needs.

        If the state is local (not from reference): always raises CacheSlotError.
        If from reference + strict: raises CacheSlotError.
        If from reference + best-effort: transitions SlotProbedFromRef → SlotRefRejected.
        """
        lc = self._lifecycle
        if not isinstance(lc, _SlotProbedFromRef):
            # Local state — always a hard error.
            if reason == "cache_key_mismatch":
                raise CacheSlotError(
                    f"Write-once violation: slot '{self._pin_key}' already has a "
                    f"run with a different cache key."
                )
            elif reason == "structural_mismatch":
                raise CacheSlotError(
                    f"Cannot run in slot '{self._pin_key}': " f"it is already used for subslots."
                )
            else:
                raise CacheSlotError(f"Slot '{self._pin_key}' conflict: {reason}")

        if lc.ref.replay_mode == ReplayMode.STRICT:
            raise CacheSlotError(
                f"Strict replay reference mismatch for slot '{self._pin_key}': {reason}"
            )

        # Best-effort: drop the reference for this slot's state
        self._lifecycle = _SlotRefRejected(ref=lc.ref)

    async def _save_checkpoint(self, data: Any, *, store_scope: FxtrScope) -> None:
        """Persist checkpoint data at <pin_key>/ckpt. Overwrites any existing checkpoint.

        Args:
            store_scope: Scope where the checkpoint entity is stored. Typically
                the exec scope (active when the step function calls save_checkpoint).
                The pin transitively persists any FxtrIDs in checkpoint data.
        """
        ckpt = StepCheckpoint(value=data)
        ref = ckpt.store(scope=store_scope)
        await self._namespace.pin(f"{self._pin_key}/ckpt", ref, overwrite=True, scope=store_scope)

    async def _load_checkpoint(self) -> Any | None:
        """Load the most recent checkpoint, or None if none exists.

        Retrieves into namespace.scope — the backing store has the checkpoint
        data (from a prior _save_checkpoint pin). Loads from namespace.scope
        so that any FxtrIDs in the data are accessible.
        """
        ns_scope = self._namespace_scope
        ref = await self._namespace.try_retrieve(
            f"{self._pin_key}/ckpt", type=StepCheckpoint, into_scope=ns_scope
        )
        if ref is None:
            return None
        # Load from namespace.scope — checkpoint data and any FxtrIDs within
        # it are available via the namespace's backing store arena.
        ckpt = await ref.load(scope=ns_scope)
        return ckpt.value

    async def _delete_checkpoint(self) -> None:
        """Delete the checkpoint pin if it exists."""
        try:
            await self._namespace.unpin(f"{self._pin_key}/ckpt")
        except Exception:
            pass  # No checkpoint to delete


# ---------------------------------------------------------------------------
# FrozenCacheSlot: read-only wrapper for frozen slot trees
# ---------------------------------------------------------------------------


class FrozenCacheSlot:
    """Read-only wrapper around a FrozenSlotNode for navigating frozen slot trees.

    Supports subslot navigation via path accumulation — chained child_subslot()
    calls are sync (just extend the path tuple). The actual entity loading happens
    on the first async method call (_ensure_loaded / load_state).

    All entity loads use an explicit load_scope (typically namespace.scope),
    which has access to the frozen entities via the namespace's backing store.

    Does NOT implement CacheSlot ABC — no run/run_fallible. Does NOT handle
    internal cache slot navigation — that belongs to FrozenStepContext.
    """

    def __init__(
        self,
        root_ref: EntityRef[FrozenSlotNode],
        load_scope: FxtrScope,
        subslot_path: tuple[str, ...] = (),
    ) -> None:
        self._root_ref = root_ref
        self._load_scope = load_scope
        self._subslot_path = subslot_path
        self._node: FrozenSlotNode | None = None
        self._loaded = False

    def child_subslot(self, key: str | int) -> FrozenCacheSlot:
        """Sync: returns a new FrozenCacheSlot with extended subslot path."""
        return FrozenCacheSlot(
            self._root_ref,
            self._load_scope,
            self._subslot_path + (str(key),),
        )

    async def _ensure_loaded(self) -> FrozenSlotNode | None:
        """Load the root ref and walk the subslot path. Caches the result."""
        if self._loaded:
            return self._node
        # root_ref is loadable in _load_scope because it was stored as part
        # of a FrozenInternalSlots entity that was pinned with SlotFinished,
        # and the namespace's backing store has all transitive deps.
        node = await self._root_ref.load(scope=self._load_scope)
        for seg in self._subslot_path:
            ref = node.subslots.get(seg)
            if ref is None:
                self._node = None
                self._loaded = True
                return None
            # Subslot refs are children of the FrozenSlotNode, loadable
            # from the same scope via the namespace's backing store.
            node = await ref.load(scope=self._load_scope)
        self._node = node
        self._loaded = True
        return node

    async def load_state(self) -> SlotState | None:
        """Load the slot state from the frozen node, or None if empty."""
        node = await self._ensure_loaded()
        if node is None or node.state_ref is None:
            return None
        # state_ref is a child of the FrozenSlotNode, loadable from the
        # same scope via the namespace's backing store.
        return await node.state_ref.load(scope=self._load_scope)

    async def load_state_ref(self) -> EntityRef[SlotState] | None:
        """Load the frozen node and return its state_ref (without loading the state itself)."""
        node = await self._ensure_loaded()
        if node is None:
            return None
        return node.state_ref


# ---------------------------------------------------------------------------
# FrozenStepContext: frozen internal slot lookup for replay checks
# ---------------------------------------------------------------------------


class FrozenStepContext(RunReplayReference):
    """Frozen counterpart of LiveStepContext for replay checking finished runs.

    Wraps FrozenInternalSlots + cache-slot args, providing slot ref resolution
    and replay checking using frozen state (no live pins needed).

    Used when a slot is already Finished: _handle_finished_slot creates one
    and delegates the replay check to it. Also used recursively when verifying
    inner runs during replay check.

    All entity loads use load_scope (namespace.scope) — the frozen entities
    are in the namespace's backing store from the original pin.

    Implements RunReplayReference: provides frozen internal cache slot
    references for replay reference propagation during execution.
    """

    def __init__(
        self,
        frozen_internals: FrozenInternalSlots,
        load_scope: FxtrScope,
        cache_slot_args: list[LiveCacheSlot | FrozenCacheSlot] | None = None,
    ) -> None:
        self._frozen_internals = frozen_internals
        self._load_scope = load_scope
        self._cache_slot_args: list[LiveCacheSlot | FrozenCacheSlot] = cache_slot_args or []

    def get_internal_slot(self, key: str) -> FrozenCacheSlot | None:
        """Look up a frozen internal cache slot by short key. Sync."""
        ref = self._frozen_internals.slots.get(key)
        if ref is None:
            return None
        return FrozenCacheSlot(root_ref=ref, load_scope=self._load_scope)

    def get_internal_replay_ref(self, key: str) -> FrozenCacheSlot | None:
        """RunReplayReference implementation. Delegates to get_internal_slot."""
        return self.get_internal_slot(key)

    async def _resolve_slot_ref(
        self, ref: ForeignSlotRef | InternalSlotRef
    ) -> FrozenCacheSlot | LiveCacheSlot:
        """Resolve a relative slot reference from frozen state.

        ForeignSlotRef: indexes into _cache_slot_args, walks subslot_path.
        InternalSlotRef: looks up in frozen internals, walks subslot_path.
        """
        if isinstance(ref, ForeignSlotRef):
            slot: LiveCacheSlot | FrozenCacheSlot = self._cache_slot_args[ref.arg_index]
            for seg in ref.subslot_path:
                if isinstance(slot, FrozenCacheSlot):
                    slot = slot.child_subslot(seg)
                else:
                    slot = slot.subslot(seg)
            return slot
        elif isinstance(ref, InternalSlotRef):
            frozen = self.get_internal_slot(ref.cache_slot_key)
            if frozen is None:
                raise KeyError(
                    f"Internal slot {ref.cache_slot_key!r} not found in frozen tree — "
                    f"tracked runs should only reference slots that existed during the run"
                )
            for seg in ref.subslot_path:
                frozen = frozen.child_subslot(seg)
            return frozen
        else:
            raise TypeError(f"Unknown slot ref type: {type(ref)}")

    async def replay_check(self, slot_deps: TrackedForeignSlotDeps) -> bool:
        """Recursively verify that cached results are still valid.

        Resolves all slot references from frozen state and cache-slot args.
        For each tracked run, compares the current slot state's entity ID to
        what was recorded. Recurses into inner runs by building inner
        FrozenStepContext instances from each run's frozen_internal_slots_ref.

        For LiveCacheSlot entries (foreign slots), uses probe_state() to
        consult replay references, and realize() to commit matching state.
        For FrozenCacheSlot entries (internal slots), reads state directly.
        """
        for tracked_run in slot_deps.runs:
            current_slot = await self._resolve_slot_ref(tracked_run.run_location)

            # Load state and get entity ID — different paths for Live vs Frozen
            probed_ref: EntityRef[SlotState] | None = None
            if isinstance(current_slot, FrozenCacheSlot):
                state_ref = await current_slot.load_state_ref()
                current_finished_id = state_ref.entity_id if state_ref is not None else None
                # Frozen state is loadable from _load_scope (namespace.scope)
                # because it was pinned transitively with SlotFinished.
                current_state = (
                    await state_ref.load(scope=self._load_scope) if state_ref is not None else None
                )
            else:
                # LiveCacheSlot — use probe_state() to consult replay reference.
                # probe_state loads into the slot's namespace.scope internally.
                probed_ref = await current_slot.probe_state()
                current_finished_id = probed_ref.entity_id if probed_ref is not None else None
                # Load from the slot's namespace scope — the entity was
                # retrieved there by _load_state or _persist_state.
                current_state = (
                    await probed_ref.load(scope=current_slot._namespace_scope)
                    if probed_ref is not None
                    else None
                )

            if current_finished_id != tracked_run.observed_finished_ref.entity_id:
                return False
            assert isinstance(current_state, SlotFinished)

            # Realize the probed state if it came from a replay reference.
            # into_scope: use the slot's namespace scope — realize will
            # copy_from_scope + pin + retrieve into namespace.scope.
            if probed_ref is not None:
                await current_slot.realize(  # type: ignore[union-attr]
                    probed_ref, into_scope=current_slot._namespace_scope  # type: ignore[union-attr]
                )

            # Build inner_args for recursive check
            mappings = tracked_run.inner_slot_arg_mappings
            inner_args: list[LiveCacheSlot | FrozenCacheSlot] = []
            if mappings:
                max_idx = max(m.inner_arg_index for m in mappings)
                inner_args_build: list[LiveCacheSlot | FrozenCacheSlot | None] = [None] * (
                    max_idx + 1
                )
                for mapping in mappings:
                    inner_args_build[mapping.inner_arg_index] = await self._resolve_slot_ref(
                        mapping.source
                    )
                # All slots should be resolved (no Nones) — tracked runs
                # record mappings for every cache-slot arg position.
                inner_args = [s for s in inner_args_build if s is not None]
                assert len(inner_args) == len(inner_args_build)

            # Build inner FrozenStepContext for the recursive replay check.
            # frozen_internal_slots_ref is loadable from _load_scope because
            # it was pinned as part of SlotFinished's transitive closure.
            frozen_internals = await current_state.frozen_internal_slots_ref.load(
                scope=self._load_scope
            )
            inner_ctx = FrozenStepContext(
                frozen_internals,
                load_scope=self._load_scope,
                cache_slot_args=inner_args,
            )
            # slot_deps_ref is also loadable from _load_scope for the same reason.
            inner_deps = await current_state.slot_deps_ref.load(scope=self._load_scope)
            if not await inner_ctx.replay_check(inner_deps):
                return False

        return True


# ---------------------------------------------------------------------------
# TracingCacheSlot: proxy that records derivation paths for replay checking
# ---------------------------------------------------------------------------


class TracingCacheSlot(CacheSlot):
    """Proxy around a CacheSlot that carries derivation path info.

    Wraps both foreign-derived slots (from cache-slot args) and internal
    cache slots (from ctx.cache_slot()). The _origin field says which kind.

    All dispatches from a TracingCacheSlot route through the step context,
    which records the tracking info and delegates to the backend.
    """

    def __init__(
        self,
        inner: LiveCacheSlot,
        origin: ForeignSlotRef | InternalSlotRef,
        step_ctx: _StepCtxForTracing,
    ) -> None:
        self._inner = inner
        self._origin = origin
        self._step_ctx = step_ctx

    @property
    def inner(self) -> LiveCacheSlot:
        return self._inner

    @property
    def origin(self) -> ForeignSlotRef | InternalSlotRef:
        return self._origin

    def run(
        self,
        step: StepHandle,
        *args: Any,
        **kwargs: Any,
    ) -> Pending[Any]:
        """Route through the step context for tracking."""
        return self._step_ctx.dispatch_from_step(self, step, args, kwargs)

    def run_fallible(
        self,
        step: StepHandle,
        *args: Any,
        **kwargs: Any,
    ) -> Pending[Any]:
        """Route fallible run through the step context."""
        return self._step_ctx.dispatch_from_step(self, step, args, kwargs, fallible=True)

    def subslot(self, key: str | int) -> TracingCacheSlot:
        """Returns a new TracingCacheSlot with extended path.

        The key is coerced to str for the subslot_path (since it's serialized as
        a tuple[str, ...]). LiveCacheSlot.subslot() also normalizes to str, so
        the same object is used regardless of key type.
        """
        inner_sub = self._inner.subslot(key)
        new_origin = dataclasses.replace(
            self._origin,
            subslot_path=self._origin.subslot_path + (str(key),),
        )
        return TracingCacheSlot(inner_sub, new_origin, self._step_ctx)


class _StepCtxForTracing(abc.ABC):
    """Interface used by TracingCacheSlot to dispatch runs. Implemented by LiveStepContext."""

    @abc.abstractmethod
    def dispatch_from_step(
        self,
        target: TracingCacheSlot,
        step: StepHandle,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        fallible: bool = False,
    ) -> Pending[Any]: ...


# ---------------------------------------------------------------------------
# ForeignRunTracker: accumulates TrackedForeignRun entries during a step
# ---------------------------------------------------------------------------


class ForeignRunTracker:
    """Tracks runs that interact with foreign-derived cache slots during step execution.

    All tracked locations are stored as relative derivation paths, never absolute
    pin keys, so the cache tree is portable.
    """

    def __init__(self) -> None:
        self._tracked_runs: list[_PendingTrackedRun] = []

    def record_run(
        self,
        target: TracingCacheSlot,
        inner_slot: LiveCacheSlot,
        inner_slot_entries: list[tuple[str, Any]],
    ) -> None:
        """Record a dispatched run and its cache-slot arg interactions.

        Called by LiveStepContext.dispatch_from_step() for every dispatch
        from within the step. inner_slot_entries is the result of
        collect_cache_slot_args on the traced args — each entry is
        (path, TracingCacheSlot) in the inner step's sorted-path order.
        """
        # Only track this run if it involves foreign-derived slots:
        # either the target is foreign-derived, or any cache-slot arg is.
        has_foreign_target = isinstance(target.origin, ForeignSlotRef)
        has_foreign_arg = any(
            isinstance(ts.origin, ForeignSlotRef)
            for _path, ts in inner_slot_entries
            if isinstance(ts, TracingCacheSlot)
        )

        if not has_foreign_target and not has_foreign_arg:
            return  # Purely internal — no tracking needed

        self._tracked_runs.append(
            _PendingTrackedRun(
                location=target.origin,
                inner_slot=inner_slot,
                inner_slot_entries=inner_slot_entries,
            )
        )

    def build_slot_deps(self) -> tuple[TrackedForeignRun, ...]:
        """Build the final slot_deps after the step completes.

        Called after wait_for_all_substeps() and check_for_aborts() — all tracked
        runs should have completed successfully, so each inner_slot should be in
        Finished state with a non-None state_ref.

        The inner_slot is the same LiveCacheSlot object that _execute_in_slot wrote
        to, so its state_ref reflects the final persisted state.
        """
        result: list[TrackedForeignRun] = []
        for entry in self._tracked_runs:
            # Get the EntityRef for the inner slot's current state.
            # This should always be non-None: we only reach build_slot_deps after
            # wait_for_all_substeps + check_for_aborts, so all tracked runs have
            # completed successfully and their slots should be in Finished state.
            state_ref = entry.inner_slot.state_ref
            assert state_ref is not None, (
                f"Tracked run at {entry.location} has no persisted state — "
                f"this indicates a bug in the step execution lifecycle"
            )

            # Build arg mappings for all cache-slot args the inner run received
            arg_mappings: list[InnerSlotArgMapping] = []
            for inner_idx, (_path, ts) in enumerate(entry.inner_slot_entries):
                assert isinstance(ts, TracingCacheSlot)
                arg_mappings.append(
                    InnerSlotArgMapping(
                        inner_arg_index=inner_idx,
                        source=ts.origin,
                    )
                )

            observed_ref: EntityRef[SlotFinished] = EntityRef(state_ref.entity_id, SlotFinished)
            result.append(
                TrackedForeignRun(
                    run_location=entry.location,
                    observed_finished_ref=observed_ref,
                    inner_slot_arg_mappings=tuple(arg_mappings),
                )
            )
        return tuple(result)


@dataclasses.dataclass
class _PendingTrackedRun:
    """Mutable tracking entry accumulated during step execution."""

    location: ForeignSlotRef | InternalSlotRef
    inner_slot: LiveCacheSlot
    # (path, TracingCacheSlot) entries from collect_cache_slot_args on traced args
    inner_slot_entries: list[tuple[str, Any]]


# ---------------------------------------------------------------------------
# Backend protocol (implemented by entrypoint, used by LiveCacheSlot)
# ---------------------------------------------------------------------------


class _Backend(abc.ABC):
    """Internal interface for dispatching steps. Implemented by the entrypoint."""

    @abc.abstractmethod
    def dispatch_in_slot(
        self,
        slot: LiveCacheSlot,
        step: StepHandle,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        fallible: bool = False,
    ) -> Pending[Any]: ...
