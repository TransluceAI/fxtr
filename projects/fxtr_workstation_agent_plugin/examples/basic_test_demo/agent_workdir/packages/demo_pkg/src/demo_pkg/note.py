"""Tiny entity type used by the basic_test_demo scenarios.

Shared between the agent's sandboxed scripts (which import this
module via the src-layout sys-path injection the workstation
MCP's sandbox bridge sets up for each ``packages/<pkg>/src``)
and the demo workstation script (which adds the same ``src/`` to
its own ``sys.path``). Both sides must register the same
``fxtr_type`` for serialization to round-trip — that's why the
namespace UUID and ``define_type`` call live here, in one place.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID

#: Stable namespace UUID for the basic_test_demo's entity types.
_DEMO_NS = uuid.UUID("e2b3f01a-4d2e-4b80-9a3a-1f5e8c9d7b40")
NOTE_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "demo_note")


@dataclasses.dataclass(frozen=True)
class Note(DataclassEntity, fxtr_type=NOTE_TYPE_ID):
    """A small note entity. The demo workstation copies one in under
    the external_name ``demo-note`` at startup so the agent can
    discover it via ``lookup_entity_by_name``."""

    title: str
    body: str
