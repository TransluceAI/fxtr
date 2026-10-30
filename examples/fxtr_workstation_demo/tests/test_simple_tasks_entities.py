"""Entity round-trip coverage for the ``simple_tasks`` marker package.

Stores each entity type into a scope and loads it back, verifying the
dataclass payloads survive the conversion layer (including the
``ChildFxtrID`` strong-link fields the scheduler's result entities lean
on).
"""

from __future__ import annotations

import pytest
from fxtr2.data.entity_ref_scope import FxtrScope

from simple_tasks import (
    NoteWithLinks,
    PromptTemplate,
    SimpleRunResult,
    WrappedEntity,
)

pytestmark = pytest.mark.anyio


async def test_entity_round_trip() -> None:
    async with FxtrScope(name="simple_tasks_round_trip") as scope:
        note = NoteWithLinks(title="a note", body="its body")
        note_ref = note.store(scope)

        linked = NoteWithLinks(title="derived", body="DERIVED", links={"input": note_ref.entity_id})
        linked_ref = linked.store(scope)

        wrapped = WrappedEntity(label="wrapped", inner=note_ref.entity_id)
        wrapped_ref = wrapped.store(scope)

        template = PromptTemplate(label="shout", template="Shout {title}: {body}")
        template_ref = template.store(scope)

        result = SimpleRunResult(
            run_id="run123",
            step_labels=("shout",),
            input_names=("a",),
            inputs={"a": note_ref.entity_id},
            cells={"a": {"0": linked_ref.entity_id}},
            failures={"b": (0, "boom")},
            completed_at="2026-06-10T00:00:00+00:00",
        )
        result_ref = result.store(scope)

        assert await note_ref.load(scope) == note
        assert await linked_ref.load(scope) == linked
        assert await wrapped_ref.load(scope) == wrapped
        assert await template_ref.load(scope) == template
        assert await result_ref.load(scope) == result
