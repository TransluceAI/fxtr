"""Entity type definitions for the workflow system.

All fxtr2 entity types and serializable data types used by fxtr_workflow
are defined here, consolidated from cache_slot.py and resolve.py.

Entity types (DataclassEntity, content-addressed, independently referenceable):
  - SlotSplit, SlotPartial, SlotFinished: cache slot state
  - CacheKeyEntity: step identity + serialized args
  - CacheSlotMarker, StepHandleMarker: leaf markers in cache key data
  - StepOutput: wrapper for step result data
  - TrackedForeignSlotDeps: wrapper for foreign run tracking data

Plain serializable types (DataclassFxtrSubpart, always inline):
  - ForeignSlotRef, InternalSlotRef: relative slot derivation paths
  - InnerSlotArgMapping: maps inner run's cache-slot arg to containing step's source
  - TrackedForeignRun: a run that interacted with foreign-derived slots
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Annotated, Literal

import mashumaro
import mashumaro.types
from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import (
    DataclassEntity,
    DataclassFxtrSubpart,
    EntityRef,
)
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr_workflow.types import SerializableTypes

# Namespace UUID for all fxtr_workflow entity types
WORKFLOW_NS = uuid.UUID("1a39d997-3113-43b8-a18d-0a98db1879e0")

# ---------------------------------------------------------------------------
# Frozen cache key data type alias
# ---------------------------------------------------------------------------

# NOTE: frozendict must come before tuple in this union, because mashumaro
# tries union members in order and iterating a frozendict yields keys as a
# tuple, so tuple[...] would match first and lose the values.
type FrozenCacheKeyData = (
    int
    | str
    | bool
    | bytes
    | float
    | None
    | FxtrID
    | EntityRef[CacheSlotMarker]
    | EntityRef[StepHandleMarker]
    | EntityRef[NetworkTransportHandleMarker]
    | frozendict[str, FrozenCacheKeyData]
    | tuple[FrozenCacheKeyData, ...]
)

# ---------------------------------------------------------------------------
# Cache key marker entities
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CacheSlotMarker(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "cache_slot_marker"),
):
    """Singleton marker for cache slot placeholders in cache keys.

    All cache slots produce the same marker (and thus the same entity ID),
    since cache slots are excluded from the cache key by design.
    """


@dataclasses.dataclass(frozen=True)
class StepHandleMarker(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "step_handle_marker"),
):
    """Marker for step handle references in cache keys.

    Different step handles produce different entity IDs because the
    step_identity field differs.
    """

    step_identity: FxtrID


@dataclasses.dataclass(frozen=True)
class NetworkTransportHandleMarker(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "network_transport_handle_marker"),
):
    """Marker for network transport handle references in cache keys.

    Different transport handles produce different entity IDs because the
    transport_identity field differs.
    """

    transport_identity: FxtrID


# ---------------------------------------------------------------------------
# Cache key entity
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CacheKeyEntity(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "cache_key"),
):
    """Entity representing a cache key: step identity + serialized args.

    Stored as a fxtr2 entity so the cache key is content-addressed and
    inspectable (you can load it to see what step/args produced a cached result).
    """

    step_identity: FxtrID
    args_data: FrozenCacheKeyData


# ---------------------------------------------------------------------------
# Step output entity
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StepOutput(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "step_output"),
):
    """Wrapper entity for step result data.

    Referenced via EntityRef from SlotFinished, so the result can be
    lazy-loaded separately. The value field uses SerializableTypes
    (which may contain mutable list/dict), but this is safe because
    the entity is behind a ref — its content hash is computed from the
    serialized CBOR, not the Python object.
    """

    value: SerializableTypes


@dataclasses.dataclass(frozen=True)
class StepError(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "step_error"),
):
    """Cached error outcome from a run_fallible call.

    Referenced via EntityRef from SlotFinished (same field as StepOutput).
    The original_type_name is None for CustomFallibleRunError, or the
    exception class name (e.g. "ValueError") for WrappedFallibleRunError.
    """

    original_type_name: str | None
    message: str
    serialized_traceback: str


@dataclasses.dataclass(frozen=True)
class StepCheckpoint(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "step_checkpoint"),
):
    """Checkpoint data saved by a running step.

    Stored at <slot_pin_key>/ckpt. Overwritten on each save_checkpoint call.
    Cleaned up when the slot transitions to Finished.
    """

    value: SerializableTypes


# ---------------------------------------------------------------------------
# Slot ref types (plain serializable, always inline)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ForeignSlotRef(DataclassFxtrSubpart):
    """A relative reference to a slot derived from a cache-slot arg."""

    arg_index: int
    subslot_path: tuple[str, ...]
    _k: Literal["foreign"] = "foreign"


@dataclasses.dataclass(frozen=True)
class InternalSlotRef(DataclassFxtrSubpart):
    """A relative reference to a slot derived from the step's own cache slot."""

    cache_slot_key: str
    subslot_path: tuple[str, ...]
    _k: Literal["internal"] = "internal"


SlotRef = Annotated[
    ForeignSlotRef | InternalSlotRef,
    mashumaro.types.Discriminator(field="_k", include_supertypes=True),
]


# ---------------------------------------------------------------------------
# Inner slot arg mapping (plain serializable, always inline)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InnerSlotArgMapping(DataclassFxtrSubpart):
    """Maps one of an inner run's cache-slot args to its source in the containing step.

    The inner_arg_index is the position in the inner step's collect_cache_slot_args
    ordering. The source is a SlotRef (ForeignSlotRef or InternalSlotRef) relative
    to the containing step.
    """

    inner_arg_index: int
    source: SlotRef


# ---------------------------------------------------------------------------
# Tracked foreign run (plain serializable, always inline)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TrackedForeignRun(DataclassFxtrSubpart):
    """A run that interacted with foreign-derived cache slots.

    All slot references are relative derivation paths, never absolute pin keys.
    This makes the cache tree portable across namespaces and prefixes.
    """

    # Where this run's slot lives, as a relative derivation path
    run_location: SlotRef
    # The SlotFinished entity observed when the containing step completed
    observed_finished_ref: EntityRef[SlotFinished]
    # Mappings for all cache-slot args the run received, indexed by the
    # inner step's arg ordering so the recursive replay check can resolve them
    inner_slot_arg_mappings: tuple[InnerSlotArgMapping, ...]


# ---------------------------------------------------------------------------
# Tracked foreign slot deps entity
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TrackedForeignSlotDeps(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "tracked_foreign_slot_deps"),
):
    """Wrapper entity for the list of tracked foreign runs.

    Referenced via EntityRef from SlotFinished, so slot deps can be
    lazy-loaded separately from the result data.
    """

    runs: tuple[TrackedForeignRun, ...]


# ---------------------------------------------------------------------------
# Slot state entity types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SlotSplit(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "slot_split"),
):
    """Slot is broken into subslots; no run in this slot itself."""


@dataclasses.dataclass(frozen=True)
class SlotPartial(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "slot_partial"),
):
    """A run is in progress or was interrupted."""

    cache_key_ref: EntityRef[CacheKeyEntity]


@dataclasses.dataclass(frozen=True)
class SlotFinished(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "slot_finished"),
):
    """A run completed (successfully or with a cached fallible error).

    result_ref points to either a StepOutput (success) or StepError (fallible
    error). Callers must load the ref and check the type to distinguish.
    """

    cache_key_ref: EntityRef[CacheKeyEntity]
    result_ref: EntityRef[StepOutput | StepError]
    slot_deps_ref: EntityRef[TrackedForeignSlotDeps]
    frozen_internal_slots_ref: EntityRef[FrozenInternalSlots]


# Union type for slot state
SlotState = SlotSplit | SlotPartial | SlotFinished


# ---------------------------------------------------------------------------
# Frozen internal slot entities
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FrozenSlotNode(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "frozen_slot_node"),
):
    """A snapshot of a single cache slot's state + its subslot children.

    Used to freeze internal cache slot state into a finished run's entity graph.
    Internal cache slots of a node are NOT stored here — if the node's state is
    SlotFinished, its own frozen_internal_slots_ref captures them (natural recursion).
    """

    state_ref: EntityRef[SlotState] | None  # None = empty
    subslots: frozendict[str, EntityRef[FrozenSlotNode]]


@dataclasses.dataclass(frozen=True)
class FrozenInternalSlots(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WORKFLOW_NS, "frozen_internal_slots"),
):
    """The frozen internal cache slots of a finished run.

    Maps short keys to their frozen slot trees. Referenced via EntityRef
    from SlotFinished, making finished runs self-contained.
    """

    slots: frozendict[str, EntityRef[FrozenSlotNode]]
