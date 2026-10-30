"""Agent-side typed view of the demo's report-builder service.

The demo backend registers a service under the well-known external
name ``"report-builder"`` (see ``run_demo_workstation.py``). It
exposes two methods:

- ``build_root_report(title, summary, source, inputs)`` →
  :class:`EntityRef[RootReport]`. Compiles MDX ``source`` against
  the named entity ``inputs`` (each referenced inside the MDX as
  ``props.fxtr_inputs.<name>``), wraps the result in a
  :class:`RootReport(title=, summary=, contents=...)`, and returns
  the ref.

- ``build_sub_report(label, overview_source, detail_source,
  overview_inputs, detail_inputs)`` →
  :class:`EntityRef[SubReport]`. Same shape but with separate
  overview / detail MDX bodies + per-body input maps.

This module exposes:

- :class:`ReportBuilderProto` — typed protocol declaration the
  agent uses to re-type the service ref before invoking.
  Intentionally duplicated from the host-side declaration in
  ``report_builder_service.py``; wire dispatch is by method name
  so the two only need to agree on method shapes.
- :func:`schedule_build_root_report` /
  :func:`schedule_build_sub_report` — convenience wrappers that
  look up the service, retype it, and schedule the invoke. Result
  lands on the supplied ``slot_key`` in the agent's event log.

Typical use:

.. code-block:: python

    import report_builder
    from _fxtr_mcp_builtin import lookup_entity, present_to_user, retrieve_result

    note_ref = await lookup_entity("demo-note")  # any entity ref
    await report_builder.schedule_build_root_report(
        "demo_report",
        title="My report",
        summary="A short summary",
        source='''
            # Hello

            Here's the note: <RenderLink entity={fxtr_inputs.note}/>
        ''',
        inputs={"note": note_ref},
    )
    # In a follow-up run_script (or with wait_seconds on the
    # enclosing run_script):
    report_ref = await retrieve_result("demo_report")
    await present_to_user(report_ref)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.services.typed_service import service_protocol

#: External name the report-builder service is registered under.
#: Wire-stable; don't change.
REPORT_BUILDER_EXTERNAL_NAME = "report-builder"


@service_protocol
class ReportBuilderProto(Protocol):
    """Typed view of the demo's report-builder service.

    Returns ``EntityRef[UnknownEntity]`` (rather than the more
    precise :class:`EntityRef[RootReport]` / :class:`EntityRef[SubReport]`)
    to avoid taking a dependency on :mod:`fxtr_report_builder` from
    sandboxed agent code. The wire shape is identical — the agent
    can ``replace_target_type(RootReport)`` on the returned ref if
    they want a typed handle.
    """

    async def build_root_report(
        self,
        title: str,
        summary: str,
        source: str,
        inputs: Mapping[str, EntityRef[UnknownEntity]],
    ) -> EntityRef[UnknownEntity]: ...

    async def build_sub_report(
        self,
        label: str,
        overview_source: str,
        detail_source: str,
        overview_inputs: Mapping[str, EntityRef[UnknownEntity]],
        detail_inputs: Mapping[str, EntityRef[UnknownEntity]],
    ) -> EntityRef[UnknownEntity]: ...


async def schedule_build_root_report(
    slot_key: str,
    *,
    title: str,
    summary: str,
    source: str,
    inputs: Mapping[str, EntityRef[Any]] | None = None,
) -> None:
    """Schedule a ``build_root_report`` invoke. Result lands on
    ``slot_key`` as an ``EntityRef`` to the constructed
    :class:`RootReport`.

    Read the outcome from a subsequent ``run_script`` via
    ``retrieve_result(slot_key)``, or pass ``wait_seconds=N`` to
    the enclosing ``run_script`` to block inline.

    On MDX compile / build failure the slot promotes to
    ``invoke_error`` carrying the underlying ``MdxBuildError`` /
    ``MdxSubprocessError`` message.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    ref = await lookup_service_by_name(REPORT_BUILDER_EXTERNAL_NAME)
    typed = ref.replace_service_type(ReportBuilderProto)
    inputs_map: Mapping[str, EntityRef[Any]] = inputs or {}
    await schedule_invoke(
        slot_key,
        typed,
        lambda b: b.build_root_report(
            title=title,
            summary=summary,
            source=source,
            inputs=inputs_map,
        ),
    )


async def schedule_build_sub_report(
    slot_key: str,
    *,
    label: str,
    overview_source: str,
    detail_source: str,
    overview_inputs: Mapping[str, EntityRef[Any]] | None = None,
    detail_inputs: Mapping[str, EntityRef[Any]] | None = None,
) -> None:
    """Schedule a ``build_sub_report`` invoke. Result lands on
    ``slot_key`` as an ``EntityRef`` to the constructed
    :class:`SubReport`. Same wait-semantics as
    :func:`schedule_build_root_report`.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    ref = await lookup_service_by_name(REPORT_BUILDER_EXTERNAL_NAME)
    typed = ref.replace_service_type(ReportBuilderProto)
    overview_map: Mapping[str, EntityRef[Any]] = overview_inputs or {}
    detail_map: Mapping[str, EntityRef[Any]] = detail_inputs or {}
    await schedule_invoke(
        slot_key,
        typed,
        lambda b: b.build_sub_report(
            label=label,
            overview_source=overview_source,
            detail_source=detail_source,
            overview_inputs=overview_map,
            detail_inputs=detail_map,
        ),
    )


__all__ = [
    "REPORT_BUILDER_EXTERNAL_NAME",
    "ReportBuilderProto",
    "schedule_build_root_report",
    "schedule_build_sub_report",
]
