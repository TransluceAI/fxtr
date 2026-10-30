"""Service handle entities.

A **service handle** is a content-addressed entity identifying a stateless
service that a provider can (re)instantiate on demand. The empty generic marker
base :class:`ServiceHandle` lets a concrete handle declare which service
protocol it spawns; the provider dispatches on the concrete handle type.

This mirrors the ``ChannelTag[S, V, R]`` pattern in ``fxtr2.common.channels``: the
marker base subclasses :class:`~fxtr2.data.entity_ref_scope.Entity` and adds no
fields / no ``_fxtr_store_impl`` (so it stays abstract), while concrete handles
get their serialization from :class:`DataclassEntity`.
"""

from __future__ import annotations

import dataclasses

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import Entity
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import UnknownService

from .._ns import WORKFLOWS_NS

EXTERNAL_SERVICE_HANDLE_TYPE_ID = define_type(WORKFLOWS_NS, "external_service_handle")


class ServiceHandle[T](Entity):
    """Abstract marker base: "I identify a stateless service of protocol ``T``".

    Concrete handles subclass both :class:`DataclassEntity` and
    ``ServiceHandle[<proto>]``::

        @dataclasses.dataclass(frozen=True)
        class MyHandle(DataclassEntity, ServiceHandle[MyProto],
                       fxtr_type=define_type(MY_NS, "my_handle")):
            ...

    Adds no fields and no ``_fxtr_store_impl`` — serialization comes entirely
    from the other base. The phantom ``T`` lets ``spawn_resolved`` recover the
    service protocol to retype the spawned ``ServiceRef`` (see
    ``protocols.spawn_resolved``).
    """


@dataclasses.dataclass(frozen=True)
class ExternalServiceHandle(
    DataclassEntity,
    ServiceHandle[UnknownService],
    fxtr_type=EXTERNAL_SERVICE_HANDLE_TYPE_ID,
):
    """Handle for a *promised-stateless* external service.

    Wraps an arbitrary registrant-chosen ``key`` entity; the handle entity's id
    (a function of the key id) is the cache identity. Registering the same key
    in a later session against a different-but-equivalent connector is exactly
    the "promised stateless" trust contract.
    """

    key: EntityRef[UnknownEntity]
    """The registrant-chosen key entity. The handle's id is a function of this
    key's id, so the same key resolves to the same handle across sessions."""
