"""Step context: provided to every running step for dispatching sub-steps."""

from __future__ import annotations

import abc
from typing import Any

from fxtr2.data.entity_core import FxtrID
from fxtr_workflow.cache_slot import (
    CacheSlot,
    ForeignRunTracker,
    LiveCacheSlot,
    ReplayRefConfig,
    RunReplayReference,
    TracingCacheSlot,
    _Backend,
    _StepCtxForTracing,
)
from fxtr_workflow.entities import ForeignSlotRef, InternalSlotRef
from fxtr_workflow.errors import StepAbortError
from fxtr_workflow.pending import Pending
from fxtr_workflow.resolve import collect_cache_slot_args, replace_cache_slots
from fxtr_workflow.step_handle import StepHandle


class StepContext(abc.ABC):
    """Context object available inside every step.

    Used to dispatch sub-steps, access internal cache slots, and
    save/load checkpoints for crash recovery.
    """

    @abc.abstractmethod
    def cache_slot(self, key: str | int) -> CacheSlot:
        """Access an internal cache slot for this step's run.

        Int keys are normalized to their string form — cache_slot(42) and
        cache_slot("42") return the same slot.
        """
        ...

    @abc.abstractmethod
    async def save_checkpoint(self, data: Any) -> None:
        """Save checkpoint data for crash recovery. Last-write-wins.

        Data must be serializable (primitives, lists, dicts).
        Checkpoints survive across executions for Partial slot recovery.
        Cleaned up automatically when the slot transitions to Finished.
        """
        ...

    @abc.abstractmethod
    async def load_checkpoint(self) -> Any | None:
        """Load the most recent checkpoint, or None if none exists.

        Works across executions: a checkpoint saved during a prior Partial
        run is loadable on re-execution of the same slot.
        """
        ...

    @abc.abstractmethod
    def get_step_config_id(self) -> FxtrID | None:
        """Get the config entity FxtrID for this step, or None if not set.

        The config entity is bound at bind_external_step time and is NOT
        part of the cache key.
        """
        ...

    @abc.abstractmethod
    def log(self, msg: str) -> None:
        """Log a message."""
        ...


class LiveStepContext(StepContext, _StepCtxForTracing):
    """Real implementation of StepContext, created per step execution.

    Owns the ForeignRunTracker and all tracing. Wraps cache-slot args with
    TracingCacheSlot proxies. Routes all dispatches from within the step
    through dispatch_from_step(), which records tracking info and delegates
    to the backend.
    """

    def __init__(
        self,
        slot: LiveCacheSlot,
        backend: _Backend,
        tracker: ForeignRunTracker,
        run_replay_ref: RunReplayReference | None = None,
        config_id: FxtrID | None = None,
    ) -> None:
        self._slot = slot
        self._backend = backend
        self._tracker = tracker
        self._run_replay_ref = run_replay_ref
        self._config_id = config_id
        self._internal_cache_slots: dict[str, LiveCacheSlot] = {}
        self._spawned_pendings: list[Pending[Any]] = []

    @property
    def tracker(self) -> ForeignRunTracker:
        return self._tracker

    def get_step_config_id(self) -> FxtrID | None:
        return self._config_id

    def wrap_cache_slot_args(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Wrap CacheSlot values in args/kwargs with TracingCacheSlot proxies.

        Uses replace_cache_slots to visit each CacheSlot in canonical order and
        replace it with a TracingCacheSlot. Each occurrence gets its own proxy
        with a unique arg_index, even if the same slot object appears multiple
        times. The arg_index matches the position in collect_cache_slots output.
        """
        step_ctx = self

        def _make_proxy(idx: int, slot: Any) -> TracingCacheSlot:
            assert isinstance(slot, LiveCacheSlot)
            origin = ForeignSlotRef(arg_index=idx, subslot_path=())
            return TracingCacheSlot(slot, origin, step_ctx)

        return replace_cache_slots(args, kwargs, _make_proxy)

    def _internal_cache_slot(self, key: str) -> LiveCacheSlot:
        """Get or create an internal cache slot by short key.

        If _run_replay_ref is set, propagates the replay reference to the
        child slot (FrozenCacheSlot or LiveCacheSlot from the reference).
        """
        if key not in self._internal_cache_slots:
            pin_key = f"{self._slot.pin_key}/c/{key}"
            child_ref_config: ReplayRefConfig | None = None
            if self._run_replay_ref is not None:
                replay = self._run_replay_ref.get_internal_replay_ref(key)
                parent_ref = self._slot.ref_config
                if replay is not None and parent_ref is not None:
                    child_ref_config = ReplayRefConfig(
                        replay_ref=replay,
                        replay_mode=parent_ref.replay_mode,
                        replay_ref_namespace=parent_ref.replay_ref_namespace,
                    )
            child = LiveCacheSlot(
                self._slot._namespace,
                pin_key,
                self._backend,
                is_internal=True,
                ref_config=child_ref_config,
            )
            self._internal_cache_slots[key] = child
        return self._internal_cache_slots[key]

    def cache_slot(self, key: str | int) -> TracingCacheSlot:
        """Access an internal cache slot, wrapped with tracing."""
        inner = self._internal_cache_slot(str(key))
        origin = InternalSlotRef(cache_slot_key=str(key), subslot_path=())
        return TracingCacheSlot(inner, origin, self)

    async def save_checkpoint(self, data: Any) -> None:
        """Save checkpoint data. Delegates to the slot's private method.

        User-boundary active-scope usage: save_checkpoint is called by user
        code (the step function) with the exec scope active. We capture it
        here because this is the boundary between user code and library code.
        """
        from fxtr2.data.entity_ref_scope import FxtrScope

        # The step function runs with exec_scope as the active scope.
        # Checkpoint data (including any FxtrIDs) is stored in this scope.
        exec_scope = FxtrScope.get_active()
        await self._slot._save_checkpoint(data, store_scope=exec_scope)

    async def load_checkpoint(self) -> Any | None:
        """Load the most recent checkpoint. Delegates to the slot's private method."""
        return await self._slot._load_checkpoint()

    def dispatch_from_step(
        self,
        target: TracingCacheSlot,
        step: StepHandle,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        fallible: bool = False,
    ) -> Pending[Any]:
        """Dispatch a run from within this step. Handles all tracking.

        Called by TracingCacheSlot.run() / run_fallible(). The args here are the
        step function's local values — they contain TracingCacheSlots (from
        wrap_cache_slot_args or ctx.cache_slot()) and possibly Pendings from
        other dispatches.

        When fallible=True, passes fallible=True to the backend so errors are
        cached as StepError. No foreign cache-slot args are allowed in that case
        (validated by LiveCacheSlot.run_fallible before reaching here).

        Key invariant: collect_cache_slot_args on these traced args produces the
        same sorted-path ordering as it will on the inner step's resolved args
        (after unwrapping and resolution in _execute_in_slot). This holds because:
        (1) TracingCacheSlot is a CacheSlot, so it's found at the same positions,
        (2) Pendings never resolve to CacheSlots (step results are SerializableTypes),
            so resolution doesn't add or remove CacheSlot entries.
        The enumerate index of each entry therefore matches the arg_index that the
        inner step's wrap_cache_slot_args will assign.
        """
        # Collect all cache-slot args in the inner run's args, in the same
        # sorted-path order that the inner step's wrap_cache_slot_args will use.
        inner_slot_entries = collect_cache_slot_args(args, kwargs)

        # Record this dispatch in the tracker
        self._tracker.record_run(target, target.inner, inner_slot_entries)

        # Unwrap tracing: the backend only sees plain LiveCacheSlots
        unwrapped_args = tuple(_unwrap_tracing(a) for a in args)
        unwrapped_kwargs = {k: _unwrap_tracing(v) for k, v in kwargs.items()}

        # Dispatch via the backend
        pending = self._backend.dispatch_in_slot(
            target.inner, step, unwrapped_args, unwrapped_kwargs, fallible=fallible
        )

        # Track the pending for containment (wait_for_all_substeps)
        self._spawned_pendings.append(pending)

        return pending

    def track_pending(self, pending: Pending[Any]) -> None:
        """Register a Pending spawned during this step's execution."""
        self._spawned_pendings.append(pending)

    async def wait_for_all_substeps(self) -> None:
        """Wait for all spawned sub-steps to complete."""
        for p in self._spawned_pendings:
            if not p.is_resolved:
                try:
                    await p
                except BaseException:
                    pass

    def check_for_aborts(self) -> StepAbortError | None:
        """Check if any spawned sub-step failed with StepAbortError."""
        for p in self._spawned_pendings:
            if p.is_resolved and p._exception is not None:
                if isinstance(p._exception, StepAbortError):
                    return p._exception
        return None

    def log(self, msg: str) -> None:
        print(f"[step] {msg}")


def _unwrap_tracing(value: Any) -> Any:
    """Recursively unwrap TracingCacheSlot to get the inner LiveCacheSlot."""
    if isinstance(value, TracingCacheSlot):
        return value.inner
    if isinstance(value, list):
        return [_unwrap_tracing(v) for v in value]
    if isinstance(value, dict):
        return {k: _unwrap_tracing(v) for k, v in value.items()}
    return value
