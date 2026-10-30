"""Cache-key canonicalization: turn task args into a ``WorkflowTaskInvocation``.

A task's cache key is the content-addressed id of its
:class:`~fxtr_workflow_2.entities.WorkflowTaskInvocation`. To make that key
*deterministic* and *provenance-blind* while still pinning the arg closures, we
canonicalize the ``FxtrServiceData`` arg tree into ``FrozenFxtrCbor``:

- entity refs (``EntityRef`` / ``FxtrID``) become ``RawWeakFxtrID`` leaves whose
  inner id has its provenance parent **stripped** (so two invocations differing
  only in arg provenance share a key, while the arg's data closure still travels
  with the invocation and stays loadable);
- mappings / sequences recurse (mappings must have ``str`` keys);
- primitives pass through;
- live ``ServiceRef`` args are rejected — a service has no stable content
  identity, so it can't participate in a cache key. (Task-storage slot args,
  which *do* have a stable identity, are converted to a path marker upstream
  before canonicalization; that is handled by the runner, not here.)
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence

from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData

from .entities import WorkflowTaskInvocation, WorkflowTaskStorageSlotPlaceholder

#: Given a ``ServiceRef`` arg, return the root-relative slot path if it is one of
#: the runner's own task-storage slots (and passes any group check), else raise.
#: ``None`` (the default) means "reject every ``ServiceRef``" (part-2 behavior).
SlotResolver = Callable[[ServiceRef[object]], str]


@dataclasses.dataclass(frozen=True)
class SlotArgMarker:
    """Placeholder carried through :func:`bare_value` for a task-storage slot arg.

    The runner replaces it with a freshly re-minted job-bound slot at ``path``
    before invoking the task fn. Not an ``FxtrServiceData`` value — purely a
    runner-internal carrier."""

    path: str


def strip_provenance(fxtr_id: FxtrID) -> FxtrID:
    """Return ``fxtr_id`` with any provenance parent dropped (bare data id)."""
    return FxtrID(raw_data_id=bytes(fxtr_id))


def canonicalize(
    value: FxtrServiceData,
    *,
    slot_resolver: SlotResolver | None = None,
    placeholder_scope: FxtrScope | None = None,
) -> FrozenFxtrCbor:
    """Canonicalize one ``FxtrServiceData`` value into ``FrozenFxtrCbor``.

    Args:
        value: An arg value (primitive, ref, or nested mapping/sequence).
        slot_resolver: If given, recognizes a task-storage slot ``ServiceRef`` and
            returns its root-relative path; the slot is encoded as a stored
            ``WorkflowTaskStorageSlotPlaceholder`` ref so the cache key depends on
            the path, not the live ref. If None, every ``ServiceRef`` is rejected.
        placeholder_scope: Scope to store slot placeholders into (required when
            ``slot_resolver`` is given).

    Returns:
        The canonical, provenance-blind ``FrozenFxtrCbor`` form (refs as
        ``RawWeakFxtrID`` leaves with provenance stripped).

    Raises:
        TypeError: For a ``ServiceRef`` that isn't a recognized slot, a
            non-``str`` mapping key, or an unsupported value type.
    """

    def recur(v: FxtrServiceData) -> FrozenFxtrCbor:
        return canonicalize(v, slot_resolver=slot_resolver, placeholder_scope=placeholder_scope)

    match value:
        case None | bool() | int() | float() | str() | bytes():
            return value
        case FxtrID():
            return RawWeakFxtrID(inner_id=strip_provenance(value))
        case EntityRef():
            return RawWeakFxtrID(inner_id=strip_provenance(value.entity_id))
        case ServiceRef():
            if slot_resolver is None:
                raise TypeError(
                    "live ServiceRef args cannot participate in a task cache key; pass a "
                    "service-handle entity to spawn inside the task instead."
                )
            assert placeholder_scope is not None
            path = slot_resolver(value)  # raises if not a recognized slot / cross-group
            placeholder_ref = WorkflowTaskStorageSlotPlaceholder(path=path).store(placeholder_scope)
            return RawWeakFxtrID(inner_id=strip_provenance(placeholder_ref.entity_id))
        case Mapping():
            out: dict[str, FrozenFxtrCbor] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"mapping arg keys must be str, got {type(key).__name__}")
                out[key] = recur(item)
            return frozendict(out)
        case Sequence():  # str / bytes already handled above
            return tuple(recur(item) for item in value)
        case _:
            raise TypeError(f"unsupported task arg type: {type(value).__name__}")


def bare_value(value: FxtrServiceData, *, slot_resolver: SlotResolver | None = None) -> object:
    """Return ``value`` with provenance stripped from every ref leaf.

    Like :func:`canonicalize`'s leaf handling, but yields ``FxtrServiceData``
    (``EntityRef`` leaves, not ``RawWeakFxtrID``) — used to forward a task's args
    to its task function, whose loadable data closure is the (provenance-blind)
    invocation closure. A recognized slot ``ServiceRef`` becomes a
    :class:`SlotArgMarker` (the runner re-mints it job-bound before invoking the
    task fn). Provenance plumbing to task fns is a tracked followup.

    Raises:
        TypeError: For a ``ServiceRef`` that isn't a recognized slot, a
            non-``str`` mapping key, or an unsupported value type.
    """
    match value:
        case None | bool() | int() | float() | str() | bytes():
            return value
        case FxtrID():
            return strip_provenance(value)
        case EntityRef():
            return EntityRef(entity_id=strip_provenance(value.entity_id), target_type=UnknownEntity)
        case ServiceRef():
            if slot_resolver is None:
                raise TypeError("live ServiceRef args are not supported in a task invocation")
            return SlotArgMarker(path=slot_resolver(value))
        case Mapping():
            out: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"mapping arg keys must be str, got {type(key).__name__}")
                out[key] = bare_value(item, slot_resolver=slot_resolver)
            return out
        case Sequence():
            return [bare_value(item, slot_resolver=slot_resolver) for item in value]
        case _:
            raise TypeError(f"unsupported task arg type: {type(value).__name__}")


def build_invocation(
    step_fn: EntityRef[UnknownEntity],
    args: Sequence[FxtrServiceData],
    kwargs: Mapping[str, FxtrServiceData],
    *,
    slot_resolver: SlotResolver | None = None,
    placeholder_scope: FxtrScope | None = None,
) -> WorkflowTaskInvocation:
    """Build the (unstored) invocation entity for a task call.

    The returned entity's stored id is the cache key. ``step_fn``'s provenance
    is stripped too, so the key depends only on the data-level identities of the
    handle and the args. ``slot_resolver`` / ``placeholder_scope`` are threaded to
    :func:`canonicalize` for task-storage slot args.

    Args:
        step_fn: The service-handle entity ref for the task function.
        args: Positional args.
        kwargs: Keyword args.
        slot_resolver: See :func:`canonicalize`.
        placeholder_scope: See :func:`canonicalize`.

    Returns:
        A ``WorkflowTaskInvocation`` ready to ``.store()`` (storing pins the arg
        closures and yields the cache-key id).
    """
    bare_step_fn = EntityRef(
        entity_id=strip_provenance(step_fn.entity_id), target_type=UnknownEntity
    )
    canon_args = tuple(
        canonicalize(a, slot_resolver=slot_resolver, placeholder_scope=placeholder_scope)
        for a in args
    )
    canon_kwargs = frozendict(
        {
            k: canonicalize(v, slot_resolver=slot_resolver, placeholder_scope=placeholder_scope)
            for k, v in kwargs.items()
        }
    )
    return WorkflowTaskInvocation(step_fn=bare_step_fn, args=canon_args, kwargs=canon_kwargs)
