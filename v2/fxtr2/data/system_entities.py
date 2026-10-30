"""System-defined entity types.

These are built-in entity types used by fxtr to represent context and path root information
for provenance tracking. They are typically created by the task runner or provenance infrastructure,
not by user code directly.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from . import fxtr_scope
from .entity_core import FxtrID
from .entity_serdes import DataclassEntity


@dataclasses.dataclass(frozen=True)
class ContextEntity(DataclassEntity, fxtr_type=fxtr_scope.track_builtin_id("ctx")):
    """Data associated with a context ("c") provenance node.

    Context entities store metadata about an execution context. When a "c" node is created,
    it references a "d" node containing a ContextEntity. The "c" node unwraps to whatever
    is in the "value" field (typically the "." ground node).

    Attributes:
        value: The node ID that this context unwraps to. Defaults to "." (ground node).
        ctx_key: An identifier for this context (e.g., job ID, environment hash).
        metadata: Additional information about the context (e.g., lockfile, timestamps).
    """

    ctx_key: Any
    metadata: Any
    value: FxtrID = dataclasses.field(default_factory=fxtr_scope.get_active_context_id)


@dataclasses.dataclass(frozen=True)
class PathRootEntity(DataclassEntity, fxtr_type=fxtr_scope.track_builtin_id("pathroot")):
    """Data associated with a path ("p") provenance node's root.

    Path root entities store metadata about a subgraph being spliced into a new provenance
    context. When a "p" node is created, the first element of its path references a "d" node
    containing a PathRootEntity. The "p" node unwraps to the node reached by following the
    path from the "value" field.

    Attributes:
        value: The root node ID of the subgraph being wrapped with provenance.
        ctx_key: An identifier for this path context (e.g., function call ID).
        metadata: Additional information about this path context.
    """

    value: FxtrID
    ctx_key: Any
    metadata: Any


@dataclasses.dataclass(frozen=True)
class FxtrBasicType(DataclassEntity, fxtr_type=fxtr_scope.track_builtin_id("basic_type")):
    """A basic type without a hardcoded schema, deduplicated via a namespace UUID.

    Attributes:
        namespace_uuid: The UUID of the namespace that this type belongs to. Used to avoid name
            conflicts. Each independent project should define its own namespace UUID for the types
            it owns.
        type_name: The name of the type.
    """

    namespace_uuid: uuid.UUID
    type_name: str


def define_type(namespace_uuid: uuid.UUID, type_name: str) -> FxtrID:
    """Define a new FxtrID for a given namespace and type name."""
    type_entity = FxtrBasicType(namespace_uuid=namespace_uuid, type_name=type_name)
    return type_entity.store().ref
