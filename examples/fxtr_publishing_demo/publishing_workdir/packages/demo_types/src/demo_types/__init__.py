"""Entity types for the publishing demo's report graph.

Shared by every stage of the build → serve arc:

- ``seed_data.py`` constructs :class:`Note` inputs and pins them into the repo
  store (committed as ``*.fxtr.tsv`` pins).
- the postprocess builder reads the notes and pins a :class:`Stats` aggregate.
- the subreport / report builders embed both as ``RenderFigure`` refs.
- the in-flake renderers (``renderers/note`` / ``renderers/stats``) dispatch on
  these two type ids.

The namespace UUID is stable so a type's wire identity doesn't change as long as
its label string stays put. The hand-pasted id_strings in ``flake.nix``'s
``mkType`` calls must match these (print them with
``python -c "import demo_types; print(demo_types.Note.fxtr_type.id_string())"``).
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities

#: Stable namespace UUID for the publishing demo's entity types.
_DEMO_NS = uuid.UUID("b2d9f4e1-3a6c-4e08-9f57-1c8a0d5b7e23")


@dataclasses.dataclass(frozen=True)
class Note(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_DEMO_NS, "publishing_demo_note"),
):
    """A short prose note — the seeded, **bare** input to the report graph."""

    title: str
    body: str


@dataclasses.dataclass(frozen=True)
class Stats(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_DEMO_NS, "publishing_demo_stats"),
):
    """Aggregate statistics over the seeded notes — the postprocess builder's
    **provenanced** output (carries the nix build as provenance)."""

    note_count: int
    total_words: int
    longest_title: str


__all__ = ["Note", "Stats"]
