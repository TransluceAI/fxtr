"""Entity types the agent can construct + pass to ``present_to_user``.

The agent typically uses one of these to send a structured result
to the operator. Pattern:

    from presentables import Note
    from _fxtr_mcp_builtin import present_to_user

    note = Note(title="hello", body="world").store(scope)
    await present_to_user(note)

The frontend's ``ScriptExecutionStreamEntry`` renders presented
payloads in an emphasized footer block; entity refs in the payload
become clickable ``RefLink`` instances (so the operator can click
through to open the entity in another panel).

Add new types here as the demo grows. The namespace UUID is
stable so a type's wire identity doesn't change as long as the
label string stays put.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities

#: Stable namespace UUID for the demo's presentable entity types.
_PRESENTABLES_NS = uuid.UUID("e6c9d5a3-4f72-4bf1-8a05-2d6b9c1e74af")


@dataclasses.dataclass(frozen=True)
class Note(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_PRESENTABLES_NS, "note"),
):
    """A short prose note. Useful for ad-hoc text the agent wants
    to surface to the operator without imposing more structure."""

    title: str
    body: str


@dataclasses.dataclass(frozen=True)
class TodoList(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_PRESENTABLES_NS, "todo_list"),
):
    """A simple ordered list of items. The agent can use this to
    surface a structured plan / checklist; the frontend renders
    each item via the ``JsonRefTree`` walker, so item strings (or
    nested entity refs) display naturally."""

    title: str
    items: tuple[str, ...]


#: A demo Note materialized by the agent_workdir flake as the DATA entity
#: ``fxtr.entities.demo_note`` (via ``mkEntityFromPython`` over the
#: ``type_entities`` aggregator venv), to exercise ``retrieve_nix_entity``
#: end-to-end. ``mkEntityFromPython`` resolves ``presentables:DEMO_NOTE`` to
#: this instance and pins it into a layered store.
DEMO_NOTE = Note(
    title="demo note (nix DATA entity)",
    body=(
        "Materialized from packages/presentables by fxtr.entities.demo_note; "
        "pull it into a script with retrieve_nix_entity('demo_note')."
    ),
)


__all__ = ["Note", "TodoList", "DEMO_NOTE"]
