"""Report-builder service for the assembly-stage demo.

Exposes :class:`ReportBuilderProto` over the workstation's typed
service interface. The agent invokes ``build_root_report(...)`` /
``build_sub_report(...)`` with an MDX source string + named entity
inputs; the impl shells out to the TS MDX subprocess via
:mod:`fxtr_report_builder.mdx_builder`, stores the resulting
:class:`RootReport` / :class:`SubReport` entity in the call scope,
and returns its :class:`EntityRef`. The agent then exposes the ref
to the operator (e.g. via ``present_to_user``) — and the frontend
renders it through the typed RootReport / SubReport renderers
registered in ``renderers/builtin/index.tsx``.

Wire shape: typed-protocol invokes. The ``inputs`` kwargs travel as
``Mapping[str, EntityRef[UnknownEntity]]``; the sandbox bridge's
wire walker handles dict-of-refs naturally.

Mount via :func:`mount_report_builder` (async CM). Same shape as the
workstation's ``mount_*`` external-service helpers — opens an isolated
scope + impl-owned task group, mounts the impl, yields a
:class:`MountedReportBuilder` for plugging into
``ExternalServiceConfig``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncGenerator, Mapping
from typing import Protocol

import anyio
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    EntityRef,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_report_builder import RootReport, SubReport, build_root_report, build_sub_report

# Stable namespace UUID for the demo's service type-ids.
_DEMO_REPORT_BUILDER_NS = uuid.UUID("1f4e5d6c-2b3a-4978-8c1e-6d5a4b3c2e1f")

#: Wire-stable type id for the report-builder service. Used by
#: :meth:`ReportBuilderImpl._service_type_id`.
REPORT_BUILDER_SERVICE_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_REPORT_BUILDER_NS, "report_builder_service"
)


@service_protocol
class ReportBuilderProto(Protocol):
    """Host-side mirror of the report-builder protocol.

    Intentionally duplicated from
    ``packages/report_builder.ReportBuilderProto`` in the agent
    workdir — wire dispatch is by method name, so the two
    declarations only need to agree on method shapes.

    ``inputs`` is a mapping from a name (referenced inside the MDX
    source as ``props.fxtr_inputs.<name>``) to an entity ref. The
    refs are extracted into the underlying ``RawWeakFxtrID`` form
    by the impl before shipping to the MDX subprocess.
    """

    async def build_root_report(
        self,
        title: str,
        summary: str,
        source: str,
        inputs: Mapping[str, EntityRef[UnknownEntity]],
    ) -> EntityRef[RootReport]: ...

    async def build_sub_report(
        self,
        label: str,
        overview_source: str,
        detail_source: str,
        overview_inputs: Mapping[str, EntityRef[UnknownEntity]],
        detail_inputs: Mapping[str, EntityRef[UnknownEntity]],
    ) -> EntityRef[SubReport]: ...


class ReportBuilderImpl(TypedServiceImpl[ReportBuilderProto]):
    """Implementation of :class:`ReportBuilderProto`.

    Stateless across invocations — each call shells out to the MDX
    subprocess and stores the resulting entity in the dispatch
    ``call_scope``. The returned ref is automatically copied back
    to the caller's scope by the workstation's RPC layer.
    """

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return ReportBuilderProto

    def _service_type_id(self) -> FxtrID:
        return REPORT_BUILDER_SERVICE_TYPE_ID

    @service_method
    async def build_root_report(
        self,
        title: str,
        summary: str,
        source: str,
        inputs: Mapping[str, EntityRef[UnknownEntity]],
    ) -> EntityRef[RootReport]:
        report = await build_root_report(
            title=title,
            summary=summary,
            source=source,
            inputs=dict(inputs),
        )
        # Store into the dispatch call scope so the ref lifetime is
        # bound to the invoke; the workstation copies the result
        # entity back to the caller's scope automatically.
        call_scope = get_active_scope()
        return report.store(call_scope)

    @service_method
    async def build_sub_report(
        self,
        label: str,
        overview_source: str,
        detail_source: str,
        overview_inputs: Mapping[str, EntityRef[UnknownEntity]],
        detail_inputs: Mapping[str, EntityRef[UnknownEntity]],
    ) -> EntityRef[SubReport]:
        report = await build_sub_report(
            label=label,
            overview_source=overview_source,
            detail_source=detail_source,
            overview_inputs=dict(overview_inputs),
            detail_inputs=dict(detail_inputs),
        )
        call_scope = get_active_scope()
        return report.store(call_scope)


@dataclasses.dataclass(frozen=True)
class MountedReportBuilder:
    """Handle yielded by :func:`mount_report_builder`."""

    scope: FxtrScope
    service_ref: ServiceRef[ReportBuilderProto]
    impl: ReportBuilderImpl


@contextlib.asynccontextmanager
async def mount_report_builder(
    *,
    parent_scope: FxtrScope | None = None,
    scope_name: str = "report_builder",
) -> AsyncGenerator[MountedReportBuilder, None]:
    """Open a scope, mount a :class:`ReportBuilderImpl`, yield the
    handle. Pair the ``(service_ref, scope)`` with an
    :class:`ExternalServiceConfig` in
    :class:`WorkstationServer.external_services` to expose the
    service to the workstation.

    The impl is stateless across invocations and holds no per-call
    resources, so the task group is a formality — included for
    parity with the other ``mount_*`` external-service helpers and
    future expansion.
    """
    include = [parent_scope] if parent_scope is not None else [GLOBAL_SCOPE]
    async with FxtrScope(
        isolated=True,
        include=include,
        name=scope_name,
    ) as scope:
        async with anyio.create_task_group() as impl_tg:
            del impl_tg  # reserved for future use; see docstring above
            impl = ReportBuilderImpl()
            service_ref = (await scope.mount(impl)).replace_service_type(ReportBuilderProto)
            yield MountedReportBuilder(scope=scope, service_ref=service_ref, impl=impl)


__all__ = [
    "REPORT_BUILDER_SERVICE_TYPE_ID",
    "MountedReportBuilder",
    "ReportBuilderImpl",
    "ReportBuilderProto",
    "mount_report_builder",
]
