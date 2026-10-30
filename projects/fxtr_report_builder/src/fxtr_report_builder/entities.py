"""Report entity types — ``RootReport`` and ``SubReport``.

Both store an MDX-compiled component tree as a
:data:`FrozenFxtrCbor` field. The tree is opaque to mashumaro and
the storage layer; the workstation wire codec walks it on
load-time, converting :class:`RawWeakFxtrID` leaves to
``["entity", short_id]`` tags so the frontend sees an
``EntityRefStub`` at each ref position.

Tree shape: ``[type, props, children]`` tuples, where ``type`` is a
string (HTML element name, SVG element name, ``"__fragment"``,
``"RenderFigure"``, or ``"RenderLink"``). The
:mod:`fxtr_report_builder.mdx_builder` helpers are how you
typically build these trees.

A fresh hardcoded namespace UUID is used for the type ids — no
relation to the older TS-side ``fxtr-reports`` namespace. These are
new Python-owned entities.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID

_FXTR_REPORTS_NS = uuid.UUID("8e1a2c6d-4a9b-4d70-8f53-2c7b9e1d4a05")

ROOT_REPORT_TYPE_ID: FxtrID = system_entities.define_type(_FXTR_REPORTS_NS, "RootReport")
SUB_REPORT_TYPE_ID: FxtrID = system_entities.define_type(_FXTR_REPORTS_NS, "SubReport")


@dataclasses.dataclass(frozen=True)
class RootReport(DataclassEntity, fxtr_type=ROOT_REPORT_TYPE_ID):
    """A top-level report rendered as a ``FullPanel`` in the workstation.

    The ``contents`` field is the report body — an MDX-compiled
    component tree that the frontend's tree renderer walks to emit
    HTML, embedded ``RenderFigure`` dispatches, and inline
    ``RenderLink`` reflinks.
    """

    title: str
    summary: str
    contents: FrozenFxtrCbor


@dataclasses.dataclass(frozen=True)
class SubReport(DataclassEntity, fxtr_type=SUB_REPORT_TYPE_ID):
    """A sub-section of a report — small enough to render inline as a figure.

    ``overview`` is shown when the entity is rendered through the
    ``Figure`` dispatch key (compact figure-mode rendering inside a
    parent report); ``detail`` is shown below ``overview`` in
    ``FullPanel`` mode. Both fields are MDX-compiled component
    trees.
    """

    label: str
    overview: FrozenFxtrCbor
    detail: FrozenFxtrCbor
