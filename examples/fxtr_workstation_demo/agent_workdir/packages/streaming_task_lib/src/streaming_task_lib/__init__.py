"""Streaming tasks for the nested-channel demo.

Spawned as ``fxtr.services.streaming_task_lib`` (wasm, git-ref
spawn). The connect config is a plain string naming the task
(currently just one); connect to the same spawned wrapper multiple
times to pull out several task refs.

Tasks (all ``StreamingTaskProto``:
``run(FxtrID, cell_writer) -> FxtrID``):

- ``"word_shout"`` — loads the input as a ``Note`` /
  ``NoteWithLinks``, then uppercases it word by word, emitting
  ``{"kind": "partial", "text": <uppercased so far>}`` through its cell
  channel after each word (with a small sleep between words — a
  deterministic token-stream stand-in), and ``finish``es the channel
  with the stored shouted ``NoteWithLinks``'s id (also its return).

The task **owns** the cell channel: the scheduler hands it the
un-started writer, and the task starts it (with a
:class:`streaming_tasks.TaskProgressTag`), emits, and finishes.
Progress-message shapes are documented on :mod:`streaming_tasks`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fxtr2.common.channels import FxtrChannelWriter, start_channel_and_infer_types
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing as _U
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from presentables import Note
from simple_tasks import NoteWithLinks
from streaming_tasks import STREAMING_TASK_TYPE_ID, StreamingTaskProto, TaskProgressTag

#: Pause between word emissions — keeps the stream watchable (and the
#: queue-mode hubs tame) without dragging the demo out.
_WORD_DELAY_SECONDS = 0.15


class _WordShoutImpl(TypedServiceImpl[StreamingTaskProto]):
    def _target_type_annotation(self) -> Any:
        return StreamingTaskProto

    def _service_type_id(self) -> FxtrID:
        return STREAMING_TASK_TYPE_ID

    @service_method
    async def run(
        self, input: FxtrID, channel: ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]]
    ) -> FxtrID:
        note = await EntityRef(entity_id=input, target_type=Note | NoteWithLinks).load()
        scope = get_active_scope()  # dispatch scope; dies with the call
        # The task owns its cell channel: start it with a progress tag,
        # stream partials, finish with the output id.
        tag = TaskProgressTag(task_label="word_shout").store(scope)
        writer_ref = await start_channel_and_infer_types(channel, tag, None, scope=scope)
        writer = await writer_ref.resolve(scope)
        words = note.body.split()
        shouted_so_far: list[str] = []
        for word in words:
            shouted_so_far.append(word.upper())
            await writer.emit(events=[{"kind": "partial", "text": " ".join(shouted_so_far)}])
            await asyncio.sleep(_WORD_DELAY_SECONDS)
        out = NoteWithLinks(
            title=f"{note.title} (WORD-SHOUTED)",
            body=" ".join(shouted_so_far),
            links={"input": input},
        ).store()
        await writer.finish(out.entity_id)
        return out.entity_id


_TASKS = {"word_shout": _WordShoutImpl}


async def run(config: str) -> ServiceRef[StreamingTaskProto]:
    """``fxtr.service`` entry point. ``config`` names the task."""
    if config not in _TASKS:
        raise ValueError(f"unknown task {config!r}; available: {sorted(_TASKS)}")
    return await get_active_scope().mount(_TASKS[config]())
