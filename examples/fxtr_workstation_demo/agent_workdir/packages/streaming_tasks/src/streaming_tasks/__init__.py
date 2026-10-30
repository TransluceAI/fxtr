"""Protocols + tag types for the streaming workflow demo (nested channels).

The streaming scheduler is the ``simple_scheduler`` shape with one
twist: **each cell is a sub-channel of the run channel, owned by the
task**, so the run channel carries *sub-channel announcements* (by
key) and a watcher can follow every cell's interior progress live.
This exercises the two output-channel features the simple scheduler
doesn't: the caller-passes-**writer** pattern and **subchannels**
(see ``fxtr2.common.channels``).

The pieces:

- :class:`StreamingTaskProto` — one entity-to-entity transform that
  additionally takes a cell-channel **writer ref**. The scheduler
  ``spawn_subchannel``s the cell channel off the run channel and hands
  the un-started writer in; the **task owns it** — it starts the
  channel with a :class:`TaskProgressTag`, emits progress dicts, and
  finishes it with the output id.
- :class:`TaskProgressTag` — the per-cell channel's tag entity type.
  Pins ``S = None``, ``V = FxtrServiceData`` (the progress dicts
  below) and ``R = FxtrID`` (the task's output entity id).
- :class:`StreamingRunTag` — the per-run channel's tag (same static
  metadata as ``simple_tasks.SchedulerRunTag``).
- :class:`StreamingSchedulerProto` — ``run_batched`` over streaming
  tasks. The run channel's final value is an
  ``EntityRef[simple_tasks.SimpleRunResult]`` (the shape fits
  unchanged: every cell output + per-input failures).

Message vocabularies
--------------------

Per-cell channel messages (``V = FxtrServiceData``; what tasks emit —
by convention, not enforcement):

- ``{"kind": "partial", "text": str}`` — the output-so-far (each
  message carries the full text so far, so the latest message alone
  renders the stream).
- ``{"kind": "progress", "text": str, "fraction": float}`` —
  coarse-grained progress for tasks without a textual stream.

The cell channel's **final value** is the task's output ``FxtrID``;
the **task** ``finish``\\ es the cell channel with it. The scheduler
``abort``\\ s the cell channel as an idempotent backstop if the task
returns/raises without finishing.

Per-run channel messages (data-only — no service refs):

- ``{"kind": "run_inputs", "step_names": [str],
  "inputs": {name: FxtrID}}`` — first message. ``step_names`` are the
  per-step cache / column names (also the tag's ``step_labels``). (The
  step task service refs can't ride a data-only channel message; the
  scheduler holds them.)
- ``{"kind": "cell_channel", "input_name": str, "step_index": int,
  "key": str}`` — the **subchannel announcement**, emitted when a cell
  *runs* (a cached cell, see below, has no ``cell_channel``). ``key``
  is the subchannel key from ``spawn_subchannel``; a consumer reaches
  the cell channel via the run reader's ``open_subchannel_reader(key)``
  (the cell reader's ``get_tag`` yields the :class:`TaskProgressTag`).
- ``{"kind": "cell_result", "input_name": str, "step_index": int,
  "ok": bool, "output": FxtrID | None, "error": str | None,
  "cached": bool}`` — emitted when the cell finishes, so a run-grid
  fold doesn't need to subscribe to every cell channel to know cell
  completion. ``cached: true`` means the output was served from the
  scheduler's cache **without running the task** — such a cell emits
  *only* this event (no preceding ``cell_channel`` for it), so a grid
  renders it directly from the result.

Scheduler-level events are identical to ``simple_tasks``'s:
``{"kind": "run_channel", "run_id", "reader"}`` announcements (seeded
to new subscribers) + ``{"kind": "scheduler_closing"}``.

Backpressure note: the channel hubs run in queue mode (unbounded); a
chatty task across many cells can pile up undelivered events on slow
subscribers. Fine for the prototype — ``word_shout`` sleeps between
partials — but worth remembering before pointing a real token
stream at it.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Protocol

from fxtr2.common.channels import ChannelTag, FxtrChannelWriter
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing as _U
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import service_protocol
from simple_tasks import SimpleRunResult

#: Namespace UUID stable across host + sandbox so wire-form
#: ``fxtr_type`` FxtrIDs match across processes.
STREAMING_TASKS_NS = uuid.UUID("c5f02847-20c3-4cdc-a0c3-fbcb5fe4525f")

#: Service type id shared by every StreamingTask impl.
STREAMING_TASK_TYPE_ID = system_entities.define_type(STREAMING_TASKS_NS, "streaming_task")

#: The streaming scheduler's service type id — pinned; the thin panel
#: renderer routes on this.
STREAMING_SCHEDULER_TYPE_ID = system_entities.define_type(STREAMING_TASKS_NS, "streaming_scheduler")


@dataclasses.dataclass(frozen=True)
class TaskProgressTag(
    DataclassEntity,
    ChannelTag[None, FxtrServiceData, FxtrID],
    fxtr_type=system_entities.define_type(STREAMING_TASKS_NS, "task_progress_tag"),
):
    """Tag entity for one cell's progress channel.

    ``S = None`` (no status dimension); ``V = FxtrServiceData`` (the
    ``partial`` / ``progress`` dicts in the module docstring); ``R =
    FxtrID`` (the task's output entity). The **task** creates this tag
    when it starts its cell channel, so it carries only what the task
    knows (``task_label``); the cell's grid position (input / step) is
    carried by the scheduler's ``cell_channel`` message instead. The
    frontend's streaming text view is registered as a
    ``ChannelContentCard`` renderer for this type.
    """

    task_label: str


@dataclasses.dataclass(frozen=True)
class StreamingRunTag(
    DataclassEntity,
    ChannelTag[None, FxtrServiceData, EntityRef[SimpleRunResult]],
    fxtr_type=system_entities.define_type(STREAMING_TASKS_NS, "streaming_run_tag"),
):
    """Tag entity for one streaming ``run_batched``'s output channel
    (same static metadata as ``simple_tasks.SchedulerRunTag``; the
    message vocabulary differs — see the module docstring)."""

    run_id: str
    step_labels: tuple[str, ...]
    input_names: tuple[str, ...]


@service_protocol
class StreamingTaskProto(Protocol):
    """One entity-to-entity transform that streams its progress."""

    async def run(
        self, input: FxtrID, channel: ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]]
    ) -> FxtrID:
        """Run this task on an input entity, producing an output entity
        and streaming progress through ``channel``.

        ``channel`` is an **un-started cell-channel writer** the
        scheduler spawned for this cell and handed in (caller-passes-
        writer). The task **owns** it: ``start_channel_and_infer_types``
        it with a :class:`TaskProgressTag`, ``emit`` ``partial`` /
        ``progress`` messages, and ``finish`` it with the output id. The
        scheduler ``abort``s it as an idempotent backstop if the task
        returns/raises without finishing.
        """
        ...


@service_protocol
class StreamingSchedulerProto(EventSource[FxtrServiceData], Protocol):
    """Runs batches of inputs through a sequential streaming-task
    chain. Like ``simple_tasks.SimpleTaskSequenceSchedulerProto`` but
    with **named steps** (each step is ``(name, task_ref)``; the name
    is the cache identity + column label) and a per-cell output cache
    (see ``run_batched`` / ``clear_cache``). The event vocabularies
    are in this module's docstring.
    """

    async def run_batched(
        self,
        steps: list[tuple[str, ServiceRef[StreamingTaskProto]]],
        inputs: dict[str, FxtrID],
    ) -> FxtrID:
        """Run every input through every step (rows concurrent, one
        run at a time); returns the ``FxtrID`` of a
        ``simple_tasks.SimpleRunResult``. Per-cell progress streams
        through nested channels announced on the run channel.

        Each step is ``(name, task_ref)``: the ``name`` is both the
        column label and the step's **cache identity** — the
        scheduler caches each cell's output under
        ``(step input id, name)`` and, on a later run, serves a hit
        without invoking the task (marking the cell ``cached``). Pick
        a stable name per logical transform; reusing a name shares its
        cache slot. Caching is on unless the scheduler was connected
        with ``{"cache": False}``.
        """
        ...

    async def clear_cache(self) -> int:
        """Drop every cached cell output; returns the number of
        entries cleared. The next run recomputes everything."""
        ...

    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to run announcements (re-declared for this
        docstring; same contract as the simple scheduler's: seeded
        with the current/most-recent run's ``run_channel``)."""
        ...

    async def debug_state(self) -> FxtrServiceData:
        """Demo introspection: live subscription count + recent
        errors."""
        ...
