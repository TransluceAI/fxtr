"""System-defined entity types.

These are built-in entity types used by fxtr to represent context and path root information
for provenance tracking. They are typically created by the task runner or provenance infrastructure,
not by user code directly.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity

from . import entity_ref_scope
from .entity_core import FxtrID


def _ground_fxtr_id() -> FxtrID:
    return FxtrID.from_tag_and_payload(".", b"")


@dataclasses.dataclass(frozen=True)
class FallbackContextMarker(
    DataclassEntity, fxtr_type=entity_ref_scope.track_builtin_id("fallback_ctx")
):
    """Data payload for the lazily-minted fallback c-node used by ``store_provenance="auto"``.

    When a scope in ``"auto"`` mode stores a cbor whose ``$fxtr.ctx`` marker is bare but
    whose children carry provenance parents, the scope substitutes the marker with a
    layered ``(., c)`` form. If the scope has no explicit ``provenance_context``, the
    c-node is lazily minted from this entity. ``scope_name`` is the field that gives
    fallback c-nodes per-scope identity (so different code paths produce different
    fallback contexts), while ``value`` is hardcoded to the ground node so the c-node
    unwraps to ``"."`` as ``create_c_node`` requires.

    Attributes:
        scope_name: The name of the scope that produced this marker.
        value: The node ID that this context unwraps to. Always ``"."``.
    """

    scope_name: str
    value: FxtrID = dataclasses.field(default_factory=_ground_fxtr_id)


@dataclasses.dataclass(frozen=True)
class FxtrBasicType(DataclassEntity, fxtr_type=entity_ref_scope.track_builtin_id("basic_type")):
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
    """Define a new FxtrID for a given namespace and type name.

    Intended for **top-level / module-import-time use** — typical call
    sites are ``fxtr_type=define_type(...)`` arguments on
    :class:`DataclassEntity` subclasses. The type entity is **always
    stored into** :data:`GLOBAL_SCOPE`, regardless of which scope (if
    any) happens to be active when the call runs. This keeps the
    type-descriptor reachable from every later scope that includes
    GLOBAL_SCOPE as a parent.

    Why this is GLOBAL_SCOPE rather than the active scope: in practice
    these calls fire at module import. If the importing code happens
    lazily from within a transient scope (and the active-scope contextvar
    reflects that), the type ID entity would not be available to any other
    scope even if that scope includes GLOBAL_SCOPE as a parent.
    """
    type_entity = FxtrBasicType(namespace_uuid=namespace_uuid, type_name=type_name)
    return type_entity.store(scope=entity_ref_scope.GLOBAL_SCOPE).entity_id
