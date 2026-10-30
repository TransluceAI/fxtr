"""Entity types for the experiment demo's workflow values.

Every value the demo's pipelines read and write is an :class:`ExpNote`,
so cell contents render as something readable in the browser (the
workdir's ``renderers/exp_note/`` slice routes the type through the
link/panel dispatch keys). Task-fn *handle keys* are ExpNotes too —
content-addressed, so a fn's key id is stable across relaunches (what
makes the runner mount's auto-bind line up with previously written
handle cells).

The namespace UUID is stable so the type's wire identity doesn't change
as long as the label string stays put.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities

#: Stable namespace UUID for the experiment demo's entity types.
EXPERIMENT_NOTES_NS = uuid.UUID("7d3f2a91-5c48-4e06-b7aa-90f14c2d8be3")

EXP_NOTE_TYPE_ID = system_entities.define_type(EXPERIMENT_NOTES_NS, "exp_note")


@dataclasses.dataclass(frozen=True)
class ExpNote(
    DataclassEntity,
    fxtr_type=EXP_NOTE_TYPE_ID,
):
    """A short titled prose note — the demo's universal value type."""

    title: str
    body: str
