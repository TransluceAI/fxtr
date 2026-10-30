"""Basic SimpleTask transforms for the scheduler prototype.

Spawned as ``fxtr.services.simple_task_lib`` (wasm, git-ref spawn).
The connect config is a plain string naming the task; connect to the
same spawned wrapper multiple times to pull out several tasks:

.. code-block:: python

    wrapper = await retrieve_result("task_lib_running")
    await schedule_invoke("t_wrap", wrapper, lambda s: s.connect("wrap_entity"))
    await schedule_invoke("t_note", wrapper, lambda s: s.connect("note_transform"))

Tasks (all ``SimpleTaskProto``: ``run(FxtrID) -> FxtrID``):

- ``"transcript_to_note"`` — loads the input as a ``Transcript``,
  produces a ``NoteWithLinks`` summarizing it (title from the
  description, body from the messages).
- ``"wrap_entity"`` — wraps *any* entity in a ``WrappedEntity``
  (never loads the input).
- ``"note_transform"`` — loads the input as a ``Note`` /
  ``NoteWithLinks``, produces a shouting (uppercased) copy.
- ``"sleep_passthrough"`` — sleeps for a random 0–2 s delay, then
  returns its input unchanged. Useful for testing scheduler timing.

Note-producing tasks emit ``NoteWithLinks`` with an ``"input"``
provenance link back to what they were derived from.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, Coroutine

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from presentables import Note
from simple_tasks import (
    SIMPLE_TASK_TYPE_ID,
    NoteWithLinks,
    SimpleTaskProto,
    WrappedEntity,
)
from transcripts import Transcript


async def _transcript_to_note(input: FxtrID) -> FxtrID:
    transcript = await EntityRef(entity_id=input, target_type=Transcript).load()
    lines = [
        f"[{m.role}] {m.content if isinstance(m.content, str) else str(m.content)}"
        for m in transcript.messages
    ]
    note = NoteWithLinks(
        title=f"Notes on transcript: {transcript.description or transcript.name}",
        body=(
            f"Transcript {transcript.transcript_id} ({len(transcript.messages)} messages)\n\n"
            + "\n".join(lines)
        ),
        links={"input": input},
    ).store()
    return note.entity_id


async def _wrap_entity(input: FxtrID) -> FxtrID:
    wrapped = WrappedEntity(label="wrapped by simple_task_lib", inner=input).store()
    return wrapped.entity_id


async def _note_transform(input: FxtrID) -> FxtrID:
    note = await EntityRef(entity_id=input, target_type=Note | NoteWithLinks).load()
    shouted = NoteWithLinks(
        title=f"{note.title} (SHOUTED)",
        body=note.body.upper(),
        links={"input": input},
    ).store()
    return shouted.entity_id


async def _sleep_passthrough(input: FxtrID) -> FxtrID:
    await asyncio.sleep(random.uniform(0, 2))
    return input


_TASKS: dict[str, Callable[[FxtrID], Coroutine[Any, Any, FxtrID]]] = {
    "transcript_to_note": _transcript_to_note,
    "wrap_entity": _wrap_entity,
    "note_transform": _note_transform,
    "sleep_passthrough": _sleep_passthrough,
}


class _TaskImpl(TypedServiceImpl[SimpleTaskProto]):
    def __init__(self, name: str) -> None:
        self._fn = _TASKS[name]

    def _target_type_annotation(self) -> Any:
        return SimpleTaskProto

    def _service_type_id(self) -> FxtrID:
        return SIMPLE_TASK_TYPE_ID

    @service_method
    async def run(self, input: FxtrID) -> FxtrID:
        return await self._fn(input)


async def run(config: str) -> ServiceRef[SimpleTaskProto]:
    """``fxtr.service`` entry point. ``config`` names the task."""
    if config not in _TASKS:
        raise ValueError(f"unknown task {config!r}; available: {sorted(_TASKS)}")
    return await get_active_scope().mount(_TaskImpl(config))
