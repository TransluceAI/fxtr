"""Core type aliases for the workflow system."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fxtr2.data.entity_core import FxtrID

if TYPE_CHECKING:
    from fxtr_workflow.cache_slot import CacheSlot
    from fxtr_workflow.network_transport import NetworkTransportHandle
    from fxtr_workflow.pending import Pending
    from fxtr_workflow.step_handle import StepHandle

# Concrete workflow values (fully resolved, no Pendings).
type ConcreteTypes = (
    int
    | str
    | bool
    | bytes
    | float
    | None
    | FxtrID
    | CacheSlot
    | StepHandle
    | NetworkTransportHandle
    | list[ConcreteTypes]
    | dict[str, ConcreteTypes]
)

# Values that may contain unresolved Pendings (step inputs before resolution).
type PendingOrConcreteTypes = (
    int
    | str
    | bool
    | bytes
    | float
    | None
    | FxtrID
    | CacheSlot
    | StepHandle
    | NetworkTransportHandle
    | list[PendingOrConcreteTypes]
    | dict[str, PendingOrConcreteTypes]
    | Pending[Any]
)

# Subset of ConcreteTypes that can be serialized for cache storage (phase 1 results).
# NOTE: dict must come before list in this union, because mashumaro tries union
# members in order and iterating a dict yields keys as a list, so
# list[SerializableTypes] would match first and lose the values.
type SerializableTypes = (
    int
    | str
    | bool
    | bytes
    | float
    | None
    | FxtrID
    | dict[str, SerializableTypes]
    | list[SerializableTypes]
)
