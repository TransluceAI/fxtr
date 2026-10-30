"""Top-level entrypoint for the workflow system."""

from __future__ import annotations

import dataclasses
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Self

import anyio
from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.fxtr_context_api import FxtrNamespace
from fxtr_workflow.cache_slot import (
    CacheSlot,
    ForeignRunTracker,
    FrozenStepContext,
    LiveCacheSlot,
    LiveRunReference,
    ReplayMode,
    ReplayRefConfig,
    RunReplayReference,
    _Backend,
    _SlotProbedFromRef,
)
from fxtr_workflow.entities import (
    FrozenInternalSlots,
    FrozenSlotNode,
    SlotFinished,
    SlotPartial,
    SlotSplit,
    SlotState,
    StepError,
    StepOutput,
    TrackedForeignSlotDeps,
)
from fxtr_workflow.errors import (
    CacheSlotError,
    CustomFallibleRunError,
    FallibleRunError,
    ReplayCheckError,
    StepAbortError,
    WrappedFallibleRunError,
)
from fxtr_workflow.network_transport import ExternalNetworkTransportHandle
from fxtr_workflow.pending import Pending
from fxtr_workflow.resolve import (
    collect_cache_slot_args,
    collect_fxtr_ids,
    compute_cache_key,
    resolve_args,
)
from fxtr_workflow.step_context import LiveStepContext
from fxtr_workflow.step_handle import ExternalStepHandle, StepHandle
from fxtr_workflow.types import SerializableTypes


@dataclasses.dataclass(frozen=True)
class _Rejected:
    """Sentinel returned by _handle_finished_slot when best-effort drops the reference."""


_REJECTED = _Rejected()


class TopLevelEntrypoint(anyio.AsyncContextManagerMixin):
    """Top-level workflow entrypoint.

    Use as an async context manager. Captures the currently-active scope as the
    "world scope" and creates an isolated system scope for internal entities.
    Does NOT change the active scope — the caller's scope remains active.

    Scope hierarchy:
      World scope (captured, stays active)
        └─ System scope (isolated; stores workflow internal entities)
    """

    def __init__(self) -> None:
        self._backend: _EntrypointBackend | None = None

    @property
    def system_scope(self) -> FxtrScope:
        assert self._backend is not None, "Must be used inside async context manager"
        return self._backend.system_scope

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        world_scope = FxtrScope.get_active()
        async with FxtrScope(isolated=True, include=[world_scope]) as system_scope:
            async with anyio.create_task_group() as tg:
                self._backend = _EntrypointBackend(world_scope, system_scope, tg)
                yield self

    def bind_external_step(
        self, impl: Any, key_id: FxtrID, config_id: FxtrID | None = None
    ) -> ExternalStepHandle:
        """Create an ExternalStepHandle from a callable and an opaque FxtrID key.

        If config_id is provided, the step can access it at runtime via
        StepContext.get_step_config_id(). The config entity is NOT part of
        the cache key.
        """
        self.system_scope.lift(key_id)
        if config_id is not None:
            self.system_scope.lift(config_id)
        return ExternalStepHandle(impl, key_id, config_id=config_id)

    def bind_network_transport(
        self, connector: Any, key_id: FxtrID
    ) -> ExternalNetworkTransportHandle:
        """Create a NetworkTransportHandle from a ByteStreamConnectable and an opaque FxtrID key.

        The connector must have an async connect() method that returns an anyio.abc.ByteStream.
        The key_id identifies what this transport represents (e.g., which API service).

        The caller is responsible for keeping the connector alive for the
        duration of the workflow. If the connector manages resources (e.g.
        ``TransportRouteConnector`` owns an aiohttp session and task group),
        its context manager must enclose the entrypoint's::

            async with TransportRouteConnector(route) as connector:
                async with TopLevelEntrypoint() as ep:
                    handle = ep.bind_network_transport(connector, key_id)
                    # ... use handle in steps ...
        """
        self.system_scope.lift(key_id)
        return ExternalNetworkTransportHandle(connector, key_id)

    def open_cache_slot(
        self,
        namespace: FxtrNamespace,
        prefix: str,
        replay_reference: CacheSlot | None = None,
        replay_mode: ReplayMode | None = None,
    ) -> LiveCacheSlot:
        """Create a root cache slot backed by the given namespace and prefix.

        If replay_reference is provided, the slot will try to reuse cached
        results from the reference. The reference must be a LiveCacheSlot
        (runtime check). Its namespace must remain open for the workflow duration.

        replay_mode controls mismatch behavior:
        - STRICT: error on any mismatch
        - BEST_EFFORT: fall back to fresh execution on mismatch
        """
        assert self._backend is not None, "Must be used inside async context manager"
        ref_config: ReplayRefConfig | None = None
        if replay_reference is not None:
            assert isinstance(
                replay_reference, LiveCacheSlot
            ), f"replay_reference must be a LiveCacheSlot, got {type(replay_reference).__name__}"
            assert replay_mode is not None, "replay_mode is required when replay_reference is set"
            ref_config = ReplayRefConfig(
                replay_ref=replay_reference,
                replay_mode=replay_mode,
                replay_ref_namespace=replay_reference._namespace,
            )
        return LiveCacheSlot(namespace, prefix, self._backend, ref_config=ref_config)


class _EntrypointBackend(_Backend):
    """Implements step dispatch. Only deals with plain LiveCacheSlots.

    Tracing is handled by LiveStepContext before calls reach here.

    Scope hierarchy (per dispatch):
      Working scope (isolated; includes caller's active scope)
        - cache key computation, replay check, SlotPartial persistence
        └─ Step execution scope (isolated; includes working scope)
             - step function runs here, result entities stored here
             - SlotFinished persisted from here
    """

    def __init__(
        self, world_scope: FxtrScope, system_scope: FxtrScope, task_group: anyio.abc.TaskGroup
    ) -> None:
        self._world_scope = world_scope
        self._system_scope = system_scope
        self._task_group = task_group

    @property
    def system_scope(self) -> FxtrScope:
        return self._system_scope

    def dispatch_in_slot(
        self,
        slot: LiveCacheSlot,
        step: StepHandle,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        fallible: bool = False,
    ) -> Pending[Any]:
        pending: Pending[Any] = Pending()
        _fallible = fallible
        # The spawned task inherits the caller's active scope via context vars.
        # _execute_in_slot captures it as the caller_scope for the working scope.

        async def _run_step() -> None:
            try:
                result = await self._execute_in_slot(slot, step, args, kwargs, _fallible)
                pending.set_result(result)
            except BaseException as exc:
                pending.set_exception(exc)

        self._task_group.start_soon(_run_step)
        return pending

    async def _execute_in_slot(
        self,
        slot: LiveCacheSlot,
        step: StepHandle,
        raw_args: tuple[Any, ...],
        raw_kwargs: dict[str, Any],
        fallible: bool = False,
    ) -> Any:
        """Cache resolution: resolve args, check cache, dispatch to execute function.

        Creates a working scope (isolated, includes caller's active scope) for
        all cache resolution operations. On cache miss, delegates to
        _execute_external_step which creates its own step execution scope.

        Results always flow through the namespace: on both hit and miss, FxtrIDs
        in results are lifted into the caller's scope.

        The index consistency chain for cache-slot args:
        1. resolve_args: resolves Pendings but doesn't change CacheSlot positions
        2. collect_cache_slot_args on resolved args: produces current_slots
        3. wrap_cache_slot_args on resolved args: assigns ForeignSlotRef(arg_index=idx)
        4. dispatch_from_step on traced args: calls collect_cache_slot_args
        """
        caller_scope = FxtrScope.get_active()

        # Create working scope for cache resolution.
        # Includes both the caller's active scope (for user entities, namespace
        # access) and the system scope (for step identities, config entities
        # lifted by bind_external_step/bind_network_transport).
        async with FxtrScope(
            isolated=True, include=[caller_scope, self._system_scope]
        ) as working_scope:
            with working_scope.as_active_scope():
                return await self._cache_resolution(
                    slot, step, raw_args, raw_kwargs, fallible, caller_scope, working_scope
                )

    async def _cache_resolution(
        self,
        slot: LiveCacheSlot,
        step: StepHandle,
        raw_args: tuple[Any, ...],
        raw_kwargs: dict[str, Any],
        fallible: bool,
        caller_scope: FxtrScope,
        working_scope: FxtrScope,
    ) -> Any:
        """Cache resolution logic, running inside the working scope.

        Separated from _execute_in_slot so the working scope is active for
        all operations (probe_state, realize, persist, etc.).

        All persistent state is stored in namespace.scope (via pin+retrieve
        in _persist_state). The working scope is used for temporary computation
        (arg resolution, cache key). Results are loaded from namespace.scope
        and FxtrIDs are lifted into the caller's scope.
        """
        ns_scope = slot._namespace_scope

        # 1. Resolve all Pendings in args
        args, kwargs = await resolve_args(raw_args, raw_kwargs)

        # 1b. Validate FxtrIDs in resolved args (no provenance parents)
        _validate_fxtr_ids_in_args(args, kwargs)

        # 2. Compute cache key (stored in working scope — temporary, only
        # needs to persist if we end up pinning SlotPartial/SlotFinished)
        cache_key_ref = compute_cache_key(step.identity, args, kwargs, scope=working_scope)

        # 3. Acquire slot lock and check state (using probe/realize/reject)
        wait_event: anyio.Event | None = None
        run_replay_ref: RunReplayReference | None = None
        async with slot._lock:
            ref = await slot.probe_state()

            if ref is None:
                # Empty — claim the slot for execution
                slot._execution_finished = anyio.Event()
                # SlotPartial references cache_key_ref which is in working_scope.
                await slot._save_state(
                    SlotPartial(cache_key_ref=cache_key_ref),
                    store_scope=working_scope,
                )
            else:
                # State was loaded in namespace.scope by _load_state/probe_state.
                state = await ref.load(scope=ns_scope)

                if isinstance(state, SlotFinished):
                    if state.cache_key_ref.entity_id == cache_key_ref.entity_id:
                        result = await self._handle_finished_slot(
                            slot,
                            state,
                            ref,
                            args,
                            kwargs,
                            fallible,
                            working_scope,
                        )
                        if result is not _REJECTED:
                            # Lift result FxtrIDs into caller's scope.
                            # They're in namespace.scope (ancestor of caller),
                            # so lift walks up and finds them.
                            _lift_result_fxtr_ids(result, caller_scope)
                            return result
                        # Best-effort reject fell through — claim the slot below
                    else:
                        slot.reject(reason="cache_key_mismatch")

                    # Build RunReplayReference from the Finished state's frozen tree.
                    # frozen_internal_slots_ref is loadable from ns_scope because
                    # it was pinned as part of SlotFinished's transitive closure.
                    frozen_internals = await state.frozen_internal_slots_ref.load(scope=ns_scope)
                    run_replay_ref = FrozenStepContext(frozen_internals, load_scope=ns_scope)

                elif isinstance(state, SlotPartial):
                    if state.cache_key_ref.entity_id == cache_key_ref.entity_id:
                        if slot._execution_finished is not None:
                            wait_event = slot._execution_finished
                        else:
                            # Resumed after crash
                            if isinstance(slot._lifecycle, _SlotProbedFromRef):
                                rc = slot._lifecycle.ref
                                run_replay_ref = LiveRunReference(
                                    rc.replay_ref,  # type: ignore[arg-type]
                                    rc.replay_ref_namespace,
                                    self,
                                )
                            await slot._realize_inner(ref, into_scope=working_scope)
                            slot._execution_finished = anyio.Event()
                    else:
                        slot.reject(reason="cache_key_mismatch")

                elif isinstance(state, SlotSplit):
                    slot.reject(reason="structural_mismatch")

                if wait_event is None and slot._execution_finished is None:
                    slot._execution_finished = anyio.Event()
                    await slot._save_state(
                        SlotPartial(cache_key_ref=cache_key_ref),
                        store_scope=working_scope,
                    )

        # If another execution is in flight, wait for it and use its result.
        if wait_event is not None:
            await wait_event.wait()
            async with slot._lock:
                state = await slot._load_state()
                if isinstance(state, SlotFinished):
                    result = await self._handle_finished_slot(
                        slot,
                        state,
                        None,
                        args,
                        kwargs,
                        fallible,
                        working_scope,
                    )
                    if result is not _REJECTED:
                        _lift_result_fxtr_ids(result, caller_scope)
                        return result
                raise StepAbortError(
                    RuntimeError(
                        f"Concurrent run in slot '{slot.pin_key}' aborted. "
                        f"The workflow should be retried."
                    )
                )

        # 4. Execute the step
        if not isinstance(step, ExternalStepHandle):
            raise TypeError(f"Only ExternalStepHandle is supported, got {type(step)}")

        await self._execute_external_step(
            slot,
            step,
            args,
            kwargs,
            working_scope,
            cache_key_ref,
            run_replay_ref,
            fallible,
        )

        # After execution, the slot is Finished and persisted in namespace.scope
        # (via _save_state in _execute_external_step). Load the result from
        # namespace.scope so FxtrIDs are accessible for lifting.
        result = await self._load_result_from_slot(slot)
        _lift_result_fxtr_ids(result, caller_scope)
        return result

    async def _execute_external_step(
        self,
        slot: LiveCacheSlot,
        step: ExternalStepHandle,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        working_scope: FxtrScope,
        cache_key_ref: EntityRef[Any],
        run_replay_ref: RunReplayReference | None,
        fallible: bool,
    ) -> None:
        """Execute an external step in its own step execution scope.

        Creates an isolated step execution scope (child of working scope),
        runs the step function with it as active, and persists results into
        the namespace via _save_state (which pins transitively and retrieves
        into namespace.scope).

        On fallible error, raises FallibleRunError (after persisting).
        On non-fallible error, raises StepAbortError (slot stays Partial).
        """
        async with FxtrScope(isolated=True, include=[working_scope]) as exec_scope:
            with exec_scope.as_active_scope():
                tracker = ForeignRunTracker()
                step_ctx = LiveStepContext(
                    slot,
                    self,
                    tracker,
                    run_replay_ref=run_replay_ref,
                    config_id=step.config_id,
                )
                traced_args, traced_kwargs = step_ctx.wrap_cache_slot_args(args, kwargs)

                # Run the step function (exec_scope is active for user code)
                result = None
                exec_error: BaseException | None = None
                try:
                    result = await step.impl(step_ctx, *traced_args, **traced_kwargs)
                    _validate_result(result)
                except StepAbortError as e:
                    exec_error = e
                except BaseException as exc:
                    exec_error = StepAbortError(exc) if not fallible else exc
                    if not fallible:
                        exec_error.__cause__ = exc
                finally:
                    await step_ctx.wait_for_all_substeps()

                # Check for poisoning
                abort_err = step_ctx.check_for_aborts()
                if abort_err is not None and exec_error is None:
                    exec_error = StepAbortError(
                        RuntimeError(
                            "A sub-step aborted during this step's execution. "
                            "The step's result cannot be cached."
                        )
                    )
                    exec_error.__cause__ = abort_err

                # Handle errors
                if exec_error is not None:
                    if fallible:
                        fallible_err = _to_fallible_error(exec_error)
                        step_error = StepError(
                            original_type_name=fallible_err.original_type_name,
                            message=fallible_err.message,
                            serialized_traceback=fallible_err.serialized_traceback,
                        )
                        # Store error entities in exec_scope. The pin in
                        # _save_state will transitively persist them.
                        error_ref = step_error.store(scope=exec_scope)
                        slot_deps_ref = TrackedForeignSlotDeps(runs=()).store(scope=exec_scope)
                        frozen_ref = _freeze_internal_slots(step_ctx, exec_scope)
                        async with slot._lock:
                            # Pin from exec_scope — all deps go to the store.
                            # Retrieve into namespace.scope internally.
                            await slot._save_state(
                                SlotFinished(
                                    cache_key_ref=cache_key_ref,
                                    result_ref=error_ref,
                                    slot_deps_ref=slot_deps_ref,
                                    frozen_internal_slots_ref=frozen_ref,
                                ),
                                store_scope=exec_scope,
                            )
                        await _cleanup_frozen_pins(step_ctx, slot._namespace)
                        await slot._delete_checkpoint()
                        _signal_execution_finished(slot)
                        raise fallible_err from exec_error
                    else:
                        _signal_execution_finished(slot)
                        if isinstance(exec_error, StepAbortError):
                            raise exec_error
                        raise StepAbortError(exec_error) from exec_error

                # Success — persist result.
                # All result entities are stored in exec_scope. The pin in
                # _save_state transitively persists them to the namespace's
                # backing store, then retrieves SlotFinished into namespace.scope.
                try:
                    tracked_runs = tracker.build_slot_deps()
                    slot_deps_ref = TrackedForeignSlotDeps(runs=tracked_runs).store(
                        scope=exec_scope
                    )
                    result_ref = StepOutput(value=result).store(scope=exec_scope)
                    frozen_ref = _freeze_internal_slots(step_ctx, exec_scope)

                    async with slot._lock:
                        await slot._save_state(
                            SlotFinished(
                                cache_key_ref=cache_key_ref,
                                result_ref=result_ref,
                                slot_deps_ref=slot_deps_ref,
                                frozen_internal_slots_ref=frozen_ref,
                            ),
                            store_scope=exec_scope,
                        )

                    await _cleanup_frozen_pins(step_ctx, slot._namespace)
                    await slot._delete_checkpoint()
                finally:
                    _signal_execution_finished(slot)

    async def _load_result_from_slot(self, slot: LiveCacheSlot) -> Any:
        """Load the result value from a Finished slot via namespace.scope.

        Loads SlotFinished → StepOutput from namespace.scope, walking the
        ref chain so that result FxtrIDs are accessible from namespace.scope.
        Used after both execution and concurrent-wait paths.
        """
        ns_scope = slot._namespace_scope
        state = await slot._load_state()
        assert isinstance(
            state, SlotFinished
        ), f"Expected SlotFinished after execution, got {type(state).__name__}"
        # Walk the ref chain in namespace.scope: SlotFinished → result_ref.
        # result_ref (StepOutput or StepError) is in the namespace's backing
        # store because _save_state pinned SlotFinished transitively.
        result_entity = await state.result_ref.load(scope=ns_scope)
        if isinstance(result_entity, StepError):
            raise _reconstruct_fallible_error(result_entity)
        return result_entity.value

    async def _handle_finished_slot(
        self,
        slot: LiveCacheSlot,
        state: SlotFinished,
        probed_ref: EntityRef[SlotState] | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        fallible: bool,
        working_scope: FxtrScope,
    ) -> SerializableTypes | _Rejected:
        """Handle a slot in Finished state with a matching cache key.

        Called from _cache_resolution. Loads entities from namespace.scope
        (where they were retrieved by _persist_state). Performs the replay
        check and returns the cached result, or _REJECTED if best-effort
        mode drops the reference.

        Args:
            working_scope: The current working scope, used as into_scope for
                realize operations.
        """
        ns_scope = slot._namespace_scope
        # result_ref is loadable from namespace.scope because SlotFinished
        # was pinned transitively and retrieved into namespace.scope.
        result_entity = await state.result_ref.load(scope=ns_scope)

        if isinstance(result_entity, StepError):
            if not fallible:
                raise CacheSlotError(
                    f"Cannot run infallible step in slot '{slot.pin_key}': "
                    f"slot contains a cached error from a prior run_fallible "
                    f"call. Clear the cache to retry."
                )
            if probed_ref is not None:
                await slot._realize_inner(probed_ref, into_scope=working_scope)
            raise _reconstruct_fallible_error(result_entity)

        # StepOutput — do replay check.
        # slot_deps_ref is loadable from namespace.scope (same reason).
        slot_deps = await state.slot_deps_ref.load(scope=ns_scope)
        if slot_deps.runs:
            cache_slot_entries = collect_cache_slot_args(args, kwargs)
            cache_slot_args: list[LiveCacheSlot] = []
            for _path, cs in cache_slot_entries:
                assert isinstance(
                    cs, LiveCacheSlot
                ), f"Expected LiveCacheSlot at top-level args, got {type(cs).__name__}"
                cache_slot_args.append(cs)
            # frozen_internal_slots_ref is also loadable from namespace.scope.
            frozen_internals = await state.frozen_internal_slots_ref.load(scope=ns_scope)
            frozen_ctx = FrozenStepContext(
                frozen_internals, load_scope=ns_scope, cache_slot_args=cache_slot_args
            )
            check_ok = await frozen_ctx.replay_check(slot_deps)
            if not check_ok:
                if probed_ref is not None:
                    try:
                        slot.reject(reason="replay_check_failed")
                    except CacheSlotError:
                        raise ReplayCheckError(
                            f"Cache-slot-arg replay check failed for slot "
                            f"'{slot.pin_key}'. The cache slot args have changed "
                            f"since the previous execution."
                        ) from None
                    return _REJECTED
                raise ReplayCheckError(
                    f"Cache-slot-arg replay check failed for slot "
                    f"'{slot.pin_key}'. The cache slot args have changed "
                    f"since the previous execution. Clear the cache or "
                    f"adjust the workflow."
                )

        if probed_ref is not None:
            await slot._realize_inner(probed_ref, into_scope=working_scope)
        return result_entity.value


def _freeze_slot_tree(slot: LiveCacheSlot, scope: FxtrScope) -> EntityRef[FrozenSlotNode]:
    """Recursively build a FrozenSlotNode for a cache slot and its subslots."""
    frozen_subslots: dict[str, EntityRef[FrozenSlotNode]] = {}
    for key, child in slot._subslots.items():
        frozen_subslots[key] = _freeze_slot_tree(child, scope)
    node = FrozenSlotNode(
        state_ref=slot.state_ref,
        subslots=frozendict(frozen_subslots),
    )
    return node.store(scope=scope)


def _freeze_internal_slots(
    step_ctx: LiveStepContext, scope: FxtrScope
) -> EntityRef[FrozenInternalSlots]:
    """Freeze the step context's internal cache slots into an entity."""
    frozen: dict[str, EntityRef[FrozenSlotNode]] = {}
    for key, child in step_ctx._internal_cache_slots.items():
        frozen[key] = _freeze_slot_tree(child, scope)
    entity = FrozenInternalSlots(slots=frozendict(frozen))
    return entity.store(scope=scope)


def _collect_pins_to_delete(slot: LiveCacheSlot) -> list[str]:
    """Collect all pin keys for a slot tree (for cleanup after freezing).

    Walks subslots only — inner steps' internal cache slots are owned by their
    own step contexts and already cleaned up when those inner steps finished.
    """
    pins = [slot.pin_key]
    for child in slot._subslots.values():
        pins.extend(_collect_pins_to_delete(child))
    return pins


async def _cleanup_frozen_pins(step_ctx: LiveStepContext, namespace: FxtrNamespace) -> None:
    """Delete pins for all internal cache slots that were frozen.

    Bare unpin (no try/except) — we know these pins exist because we just
    froze them. A KeyError would indicate an integrity violation.

    Checkpoint pins are NOT cleaned up here — each step's _execute_in_slot
    already cleans up its own checkpoint via _delete_checkpoint() before we
    reach this point (guaranteed by wait_for_all_substeps).
    """
    for child in step_ctx._internal_cache_slots.values():
        for pin_key in _collect_pins_to_delete(child):
            await namespace.unpin(pin_key)


def _lift_result_fxtr_ids(result: Any, target_scope: FxtrScope) -> None:
    """Lift any FxtrIDs in the result into the target scope.

    After a cache hit or successful execution, result FxtrIDs are in the
    namespace scope (via pinning). The target scope (caller's scope) has
    the namespace scope in its ancestry, so lift() works.
    """
    for fid in collect_fxtr_ids(result):
        target_scope.lift(fid)


def _signal_execution_finished(slot: LiveCacheSlot) -> None:
    """Signal any waiters that this slot's execution is done, and clear the event."""
    if slot._execution_finished is not None:
        slot._execution_finished.set()
        slot._execution_finished = None


def _to_fallible_error(exc: BaseException) -> FallibleRunError:
    """Convert an exception to a FallibleRunError for caching.

    If the exception is already a CustomFallibleRunError, preserve it.
    Otherwise wrap it in WrappedFallibleRunError with stringified info.
    """
    tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if isinstance(exc, CustomFallibleRunError):
        # Preserve the custom error but capture the traceback
        return CustomFallibleRunError(
            message=exc.message,
            serialized_traceback=tb_str,
        )
    return WrappedFallibleRunError(
        original_type_name=type(exc).__name__,
        message=str(exc),
        serialized_traceback=tb_str,
    )


def _reconstruct_fallible_error(step_error: StepError) -> FallibleRunError:
    """Reconstruct a FallibleRunError from a cached StepError entity."""
    if step_error.original_type_name is None:
        return CustomFallibleRunError(
            message=step_error.message,
            serialized_traceback=step_error.serialized_traceback,
        )
    return WrappedFallibleRunError(
        original_type_name=step_error.original_type_name,
        message=step_error.message,
        serialized_traceback=step_error.serialized_traceback,
    )


def _validate_fxtr_ids_in_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Validate that FxtrIDs in resolved args have no provenance parent.

    Called after arg resolution, before cache key computation. FxtrIDs with
    provenance parents cannot be used as step arguments because the provenance
    chain is not part of the content-addressed identity.
    """
    for a in args:
        for fid in collect_fxtr_ids(a):
            if fid._provenance_parent is not None:
                raise TypeError("Step arguments cannot contain FxtrIDs with a provenance parent.")
    for v in kwargs.values():
        for fid in collect_fxtr_ids(v):
            if fid._provenance_parent is not None:
                raise TypeError("Step arguments cannot contain FxtrIDs with a provenance parent.")


def _validate_result(result: Any) -> None:
    """Validate that a step result is serializable.

    Accepts primitives, lists, dicts, and FxtrID values.
    FxtrIDs with a provenance parent are rejected (they must be
    stored as plain data-level IDs).
    """
    if result is None or isinstance(result, (int, str, bool, bytes, float)):
        return
    if isinstance(result, FxtrID):
        if result._provenance_parent is not None:
            raise TypeError(
                f"Step results cannot contain FxtrIDs with a provenance parent. "
                f"Got FxtrID with provenance parent set."
            )
        return
    if isinstance(result, list):
        for v in result:
            _validate_result(v)
        return
    if isinstance(result, dict):
        for k, v in result.items():
            if not isinstance(k, str):
                raise TypeError(f"Result dict keys must be strings, got {type(k).__name__}")
            _validate_result(v)
        return
    raise TypeError(
        f"Step results must be serializable (primitives/lists/dicts/FxtrID), "
        f"got {type(result).__name__}. Returning CacheSlot, StepHandle, "
        f"or NetworkTransportHandle is not allowed."
    )
