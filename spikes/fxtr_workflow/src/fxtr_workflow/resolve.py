"""Arg resolution: deep-walk to resolve Pendings, compute cache keys, index cache-slot args."""

from __future__ import annotations

from typing import Any

from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_workflow.entities import (
    CacheKeyEntity,
    CacheSlotMarker,
    FrozenCacheKeyData,
    NetworkTransportHandleMarker,
    StepHandleMarker,
)
from fxtr_workflow.pending import Pending

# ---------------------------------------------------------------------------
# Arg resolution
# ---------------------------------------------------------------------------


async def resolve_args(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Deep-walk args/kwargs to await all Pending values, returning fully concrete values.

    Invariant: step results are always SerializableTypes (validated by _validate_result),
    so Pendings never resolve to CacheSlot or StepHandle. This means resolution does not
    change the set of CacheSlot/StepHandle instances in the arg tree — only Pendings that
    resolve to primitives/lists/dicts are replaced. Callers that call collect_cache_slot_args
    before and after resolution will find the same slots at the same structural positions.
    """
    resolved_args = tuple([await _resolve_value(a) for a in args])
    resolved_kwargs = {k: await _resolve_value(v) for k, v in kwargs.items()}
    return resolved_args, resolved_kwargs


async def _resolve_value(value: Any) -> Any:
    """Recursively resolve a single value, awaiting any Pendings."""
    if isinstance(value, Pending):
        resolved = await value
        return await _resolve_value(resolved)
    if isinstance(value, list):
        return [await _resolve_value(v) for v in value]
    if isinstance(value, dict):
        return {k: await _resolve_value(v) for k, v in value.items()}
    # Primitives, CacheSlot, StepHandle pass through unchanged
    return value


# ---------------------------------------------------------------------------
# Cache key computation
# ---------------------------------------------------------------------------


def compute_cache_key(
    step_identity_id: FxtrID,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    scope: FxtrScope | None = None,
) -> EntityRef[CacheKeyEntity]:
    """Compute a cache key from resolved args.

    Creates and stores a CacheKeyEntity with the step identity and serialized args.
    Returns an EntityRef whose entity_id is the content-addressed cache key.

    The step identity FxtrID must already be tracked in the given scope.
    Args must already be resolved (no Pendings).
    """
    serialized_args: FrozenCacheKeyData = frozendict(
        {
            "args": tuple(_serialize_for_cache_key(a, scope) for a in args),
            "kwargs": frozendict(
                {k: _serialize_for_cache_key(v, scope) for k, v in sorted(kwargs.items())}
            ),
        }
    )
    entity = CacheKeyEntity(step_identity=step_identity_id, args_data=serialized_args)
    return entity.store(scope=scope)


def _serialize_for_cache_key(value: Any, scope: FxtrScope | None) -> FrozenCacheKeyData:
    """Serialize a single resolved value for inclusion in a cache key entity.

    Uses proper entity marker types instead of magic dicts, so there's no
    risk of collision with user data.
    """
    from fxtr_workflow.cache_slot import CacheSlot
    from fxtr_workflow.network_transport import NetworkTransportHandle
    from fxtr_workflow.step_handle import StepHandle

    if isinstance(value, CacheSlot):
        return CacheSlotMarker().store(scope=scope)
    if isinstance(value, StepHandle):
        return StepHandleMarker(step_identity=value.identity).store(scope=scope)
    if isinstance(value, NetworkTransportHandle):
        return NetworkTransportHandleMarker(transport_identity=value.identity).store(scope=scope)
    if isinstance(value, list):
        return tuple(_serialize_for_cache_key(v, scope) for v in value)
    if isinstance(value, dict):
        return frozendict({k: _serialize_for_cache_key(v, scope) for k, v in sorted(value.items())})
    if isinstance(value, FxtrID):
        return value
    if isinstance(value, (int, str, bool, bytes, float, type(None))):
        return value
    raise TypeError(f"Cannot serialize {type(value).__name__} for cache key")


# ---------------------------------------------------------------------------
# Cache slot arg traversal
#
# collect_cache_slots and replace_cache_slots define the canonical traversal
# order for CacheSlot instances in args/kwargs. Both use the same order:
#   - Positional args: left to right
#   - kwargs: sorted by key
#   - lists: by index
#   - dicts: sorted by key
#   - Primitives, StepHandles, Pendings: skipped (not containers of slots)
#
# This order determines the arg_index values used by ForeignSlotRef,
# InnerSlotArgMapping, and the replay check's current_cache_slot_args list.
# All callers that need index consistency must use these functions:
#   - wrap_cache_slot_args: uses replace_cache_slots to assign arg indices
#   - dispatch_from_step: uses collect_cache_slot_args for inner slot entries
#   - _execute_in_slot: uses collect_cache_slot_args for replay check slots
#
# Works on both traced args (TracingCacheSlot, a CacheSlot subclass) and
# resolved args (LiveCacheSlot). Both are found at the same structural
# positions, producing the same ordering.
# ---------------------------------------------------------------------------


from collections.abc import Callable


def collect_cache_slots(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[Any]:
    """Collect all CacheSlot instances from args/kwargs in canonical order.

    Returns a list of CacheSlot instances. The index of each entry is its
    canonical arg_index.
    """
    from fxtr_workflow.cache_slot import CacheSlot

    result: list[Any] = []

    def _collect(value: Any) -> None:
        if isinstance(value, CacheSlot):
            result.append(value)
        elif isinstance(value, list):
            for v in value:
                _collect(v)
        elif isinstance(value, dict):
            for k in sorted(value.keys()):
                _collect(value[k])

    for arg in args:
        _collect(arg)
    for key in sorted(kwargs.keys()):
        _collect(kwargs[key])
    return result


def replace_cache_slots(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    fn: Callable[[int, Any], Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Replace each CacheSlot in args/kwargs by calling fn(index, slot).

    Uses the same canonical traversal order as collect_cache_slots. The index
    passed to fn matches the position that collect_cache_slots would return
    for the same slot occurrence. Each occurrence gets its own call, even if
    the same slot object appears multiple times.
    """
    from fxtr_workflow.cache_slot import CacheSlot

    counter = [0]

    def _replace(value: Any) -> Any:
        if isinstance(value, CacheSlot):
            idx = counter[0]
            counter[0] += 1
            return fn(idx, value)
        if isinstance(value, list):
            return [_replace(v) for v in value]
        if isinstance(value, dict):
            return {k: _replace(v) for k, v in sorted(value.items())}
        return value

    new_args = tuple(_replace(a) for a in args)
    new_kwargs = {k: _replace(v) for k, v in sorted(kwargs.items())}
    return new_args, new_kwargs


def collect_fxtr_ids(value: Any) -> list[FxtrID]:
    """Deep-walk a value tree and return all FxtrID instances found.

    Used by validation (provenance check) and by scope loading (lifting
    result FxtrIDs into the caller's scope).
    """
    result: list[FxtrID] = []

    def _collect(v: Any) -> None:
        if isinstance(v, FxtrID):
            result.append(v)
        elif isinstance(v, list):
            for item in v:
                _collect(item)
        elif isinstance(v, dict):
            for item in v.values():
                _collect(item)

    _collect(value)
    return result


def collect_cache_slot_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[tuple[str, Any]]:
    """Find all CacheSlot values in args/kwargs, returning (path, slot) pairs.

    Paths are debug strings like "args[0]", "kwargs[key]", etc. The list is
    in canonical traversal order (same as collect_cache_slots).
    """
    from fxtr_workflow.cache_slot import CacheSlot

    results: list[tuple[str, Any]] = []

    def _collect(value: Any, path: str) -> None:
        if isinstance(value, CacheSlot):
            results.append((path, value))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _collect(v, f"{path}[{i}]")
        elif isinstance(value, dict):
            for k in sorted(value.keys()):
                _collect(value[k], f"{path}[{k}]")

    for i, arg in enumerate(args):
        _collect(arg, f"args[{i}]")
    for key in sorted(kwargs.keys()):
        _collect(kwargs[key], f"kwargs[{key}]")
    return results
