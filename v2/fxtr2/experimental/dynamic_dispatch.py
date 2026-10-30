"""Dynamic-dispatch entity types — the backend side of the frontend's
runtime renderer-resolution protocol.

The dynamic dispatch system lets a frontend resolve a renderer component for an
entity / service type at runtime, by invoking a remote dispatcher service that
returns a renderer entity (compiled ESM bytes). These are the *entity types* and
*type-id constants* of that protocol — the pieces both a serving backend (the
``fxtr_nix_runner`` published-flake serve) and the workstation's dispatcher impls
need. They live here, in ``fxtr2`` (next to ``experimental/http``), so any serving
layer can mint/serve them without depending on the workstation.

The dispatcher *protocol* (``WorkstationDispatcherProto`` / ``NixDispatcherProto``)
and the resolve-result dataclass (``RendererResolution``) live in
``fxtr_workstation.dynamic_dispatch`` — they're consumed by the workstation's typed
service machinery and aren't needed to serve a renderer table.

Type ids are content-addressed by fixed UUIDs, so the location of these
definitions does not affect any entity's id or wire form.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities

#: Stable namespace UUID for dynamic-dispatch-defined types. Don't change.
DYNAMIC_DISPATCH_NS = uuid.UUID("4e9b1c7f-3a2d-4b8e-9c1f-0d5a6b7e8c9d")

#: Type ID for the dispatcher service protocol. A dispatcher service emits this
#: as its ``$fxtr.type`` so peers (the frontend connector) recognise that a
#: service ref implements the dispatcher protocol.
WORKSTATION_DISPATCHER_TYPE_ID = system_entities.define_type(
    DYNAMIC_DISPATCH_NS, "workstation_dispatcher"
)

#: Type ID for the renderer entity itself.
RENDERER_ENTITY_TYPE_ID = system_entities.define_type(DYNAMIC_DISPATCH_NS, "renderer")

#: Type ID for the v1 "reload everything" notification entity.
RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID = system_entities.define_type(
    DYNAMIC_DISPATCH_NS, "reload_everything_notification"
)


@dataclasses.dataclass(frozen=True)
class RendererEntity(DataclassEntity, fxtr_type=RENDERER_ENTITY_TYPE_ID):
    """A compiled ESM renderer module.

    The compiled bundle exports a default value with shape
    ``Record<subtype_key, Record<dispatch_key_uuid, Component>>``; the frontend
    looks up ``module[resolution.subtype_key][dispatch_key.uuid]`` to find the
    component to render.

    The entity's content-addressed FxtrID is what the frontend uses as the
    in-memory module cache key — a server guarantees its short_id is stable for
    its lifetime, so it never needs to invalidate.
    """

    module: bytes


@dataclasses.dataclass(frozen=True)
class ReloadEverythingNotification(
    DataclassEntity, fxtr_type=RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID
):
    """Notification payload broadcast to subscribed listeners when a dispatcher's
    renderer table has changed.

    v1: empty body — the frontend treats every notification as "drop all per-type
    caches and re-resolve every entry." Two empty instances content-hash-dedup to
    the same FxtrID, so emitting one per broadcast does not accumulate distinct
    entity records on the server.

    Later versions may carry narrower signals (e.g. a list of changed type-id
    strings, or changed dispatch-key uuids) so the frontend can invalidate more
    surgically. The wire shape evolves additively; existing frontends that only
    understand the empty form keep working.
    """
