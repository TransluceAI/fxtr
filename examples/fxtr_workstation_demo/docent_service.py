"""Docent-proxy service for the demo.

Exposes a small read-only slice of the Docent SDK to the agent as
a typed fxtr service. Each method calls the SDK, freezes the JSON
response into a :data:`FrozenFxtrCbor`, wraps it in one of the
entity dataclasses declared in
:mod:`docent_fxtr_proxy` (the agent-side package), stores the
entity in the call scope, and returns its :class:`EntityRef`. The
agent ``.load()``\\ s the ref to read structured fields back.

Methods exposed (all single-collection, read-only):

- ``execute_dql(collection_id, query)`` → :class:`EntityRef[DqlResult]`.
- ``get_dql_schema(collection_id)`` → :class:`EntityRef[DqlSchema]`.
- ``get_metadata_fields(collection_id, include_sample_values, sample_limit)``
  → :class:`EntityRef[MetadataFields]`.

Authentication: handled entirely on the host side. The
:class:`docent.sdk.client.Docent` client auto-loads a
``docent.env`` file from the launch CWD on construction — no
config plumbing reaches the agent. The agent only sees the typed
service ref.

Entity types are intentionally imported from the agent-side
:mod:`docent_fxtr_proxy` package (made importable by
``run_demo_workstation.py``'s ``sys.path.insert``), so a single
source of truth defines the wire types on both sides — matching
the pattern used for :mod:`presentables`.

Mount via :func:`mount_docent_service` (async CM). Same shape as
:func:`mount_report_builder` — opens an isolated scope + impl-owned
task group, mounts the impl, yields a :class:`MountedDocent` for
plugging into ``ExternalServiceConfig``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import anyio
from docent.sdk.client import Docent
from docent_fxtr_proxy import (
    DocentProto,
    DqlResult,
    DqlSchema,
    MetadataFields,
)
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID, freeze_fxtr_cbor
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    EntityRef,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

# Stable namespace UUID for the demo's docent-proxy service type-id.
# Distinct from the entity-type namespace in ``docent_fxtr_proxy`` —
# this one is just for ``_service_type_id()``.
_DOCENT_PROXY_SERVICE_NS = uuid.UUID("3a5b6c7d-8e9f-4012-9a3b-4c5d6e7f8091")

#: Wire-stable type id for the docent-proxy service. Used by
#: :meth:`DocentImpl._service_type_id`.
DOCENT_PROXY_SERVICE_TYPE_ID: FxtrID = system_entities.define_type(
    _DOCENT_PROXY_SERVICE_NS, "docent_proxy_service"
)


def _freeze_response(payload: Any) -> FrozenFxtrCbor:
    """Freeze a JSON-shaped SDK response for storage in a
    :class:`DataclassEntity` field. The SDK methods we proxy all
    return values that came out of ``response.json()`` — i.e. plain
    JSON: dicts, lists, str, int, float, bool, None. ``freeze_fxtr_cbor``
    handles exactly this set (and reuses tuples / frozendicts under
    the hood)."""
    return freeze_fxtr_cbor(payload)


class DocentImpl(TypedServiceImpl[DocentProto]):
    """Implementation of :class:`DocentProto`.

    Holds a single :class:`docent.sdk.client.Docent` instance for the
    service's lifetime. Each ``@service_method`` shells the
    (synchronous) SDK call out to a worker thread via
    :func:`anyio.to_thread.run_sync`, freezes the JSON response,
    constructs a typed entity, and stores it in the dispatch
    ``call_scope``. The workstation's RPC layer copies the returned
    ref back to the caller's scope automatically.
    """

    def __init__(self, client: Docent) -> None:
        self._client = client

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return DocentProto

    def _service_type_id(self) -> FxtrID:
        return DOCENT_PROXY_SERVICE_TYPE_ID

    @service_method
    async def execute_dql(
        self,
        collection_id: str,
        query: str,
    ) -> EntityRef[DqlResult]:
        raw = await anyio.to_thread.run_sync(self._client.execute_dql, collection_id, query)
        columns = tuple(raw.get("columns", ()))
        rows = _freeze_response(raw.get("rows", []))
        result = DqlResult(
            collection_id=collection_id,
            query=query,
            columns=columns,
            rows=rows,
        )
        return result.store(get_active_scope())

    @service_method
    async def get_dql_schema(self, collection_id: str) -> EntityRef[DqlSchema]:
        raw = await anyio.to_thread.run_sync(self._client.get_dql_schema, collection_id)
        schema = DqlSchema(
            collection_id=collection_id,
            schema=_freeze_response(raw),
        )
        return schema.store(get_active_scope())

    @service_method
    async def get_metadata_fields(
        self,
        collection_id: str,
        include_sample_values: bool = False,
        sample_limit: int = 10,
    ) -> EntityRef[MetadataFields]:
        # SDK signature: get_metadata_fields(collection_id, include_sample_values, sample_limit).
        # Response shape varies by SDK version: newer SDKs return a
        # wrapping ``dict`` like ``{"fields": [...], "total_runs": N}``;
        # older SDKs unwrap and return the field list directly. Handle
        # both so the entity's ``fields`` attribute is always the
        # list-of-field-dicts.
        raw = await anyio.to_thread.run_sync(
            self._client.get_metadata_fields,
            collection_id,
            include_sample_values,
            sample_limit,
        )
        if isinstance(raw, dict):
            fields_list = raw.get("fields", [])
            total_runs_val = raw.get("total_runs")
            total_runs = total_runs_val if isinstance(total_runs_val, int) else None
        else:
            fields_list = raw
            total_runs = None
        fields = MetadataFields(
            collection_id=collection_id,
            include_sample_values=include_sample_values,
            sample_limit=sample_limit,
            fields=_freeze_response(fields_list),
            total_runs=total_runs,
        )
        return fields.store(get_active_scope())


@dataclasses.dataclass(frozen=True)
class MountedDocent:
    """Handle yielded by :func:`mount_docent_service`."""

    scope: FxtrScope
    service_ref: ServiceRef[DocentProto]
    impl: DocentImpl


@contextlib.asynccontextmanager
async def mount_docent_service(
    *,
    parent_scope: FxtrScope | None = None,
    scope_name: str = "docent_proxy",
    client: Docent | None = None,
) -> AsyncGenerator[MountedDocent, None]:
    """Open a scope, mount a :class:`DocentImpl`, yield the handle.
    Pair the ``(service_ref, scope)`` with an
    :class:`ExternalServiceConfig` in
    :class:`WorkstationServer.external_services` to expose the
    service to the workstation.

    Auth: if ``client`` is not supplied, this constructs a default
    :class:`docent.sdk.client.Docent`, which auto-loads a
    ``docent.env`` from the launch CWD (the SDK's own behaviour) for
    ``DOCENT_API_KEY`` / ``DOCENT_SERVER_URL`` / etc. Pass an
    explicit instance if you need to control config from elsewhere.
    """
    if client is None:
        client = Docent()

    include = [parent_scope] if parent_scope is not None else [GLOBAL_SCOPE]
    async with FxtrScope(
        isolated=True,
        include=include,
        name=scope_name,
    ) as scope:
        async with anyio.create_task_group() as impl_tg:
            del impl_tg  # reserved for future use; parity with mount_report_builder
            impl = DocentImpl(client)
            service_ref = (await scope.mount(impl)).replace_service_type(DocentProto)
            yield MountedDocent(scope=scope, service_ref=service_ref, impl=impl)


__all__ = [
    "DOCENT_PROXY_SERVICE_TYPE_ID",
    "DocentImpl",
    "MountedDocent",
    "mount_docent_service",
]
