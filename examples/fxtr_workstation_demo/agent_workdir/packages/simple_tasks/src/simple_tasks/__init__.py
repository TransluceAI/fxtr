"""Protocols + entity types for the simple workflow demo.

The pieces:

- :class:`SimpleTaskProto` — one opaque entity-to-entity transform.
  At the protocol level inputs/outputs are bare ``FxtrID`` so tasks
  compose without knowing each other's types; impls are free to load
  the id against a concrete ``EntityRef[T]``.
- :class:`SimpleTaskSequenceSchedulerProto` — runs a batch of named
  inputs through a sequential chain of tasks, fully concurrent
  across inputs. Progress streams through a **per-run output
  channel** (:mod:`fxtr2.common.channels`); the scheduler's own event
  stream just announces each run's channel **reader** (consumers
  subscribe through the workstation's ``subscribe_to`` / the agent's
  ``create_callback`` + ``subscribe_to`` invoke — the scheduler is an
  ``EventSource``, and a channel reader exposes ``subscribe_full`` /
  ``subscribe_state``).
- :class:`SchedulerRunTag` — the per-run channel's tag entity type:
  carries the static run metadata and pins the channel's
  status/message/result types via its
  :class:`~fxtr2.common.channels.ChannelTag` base.
- :class:`SimpleRunResult` — the durable result entity a
  ``run_batched`` call returns (by id): every intermediate output +
  per-input failures, so the data outlives the event stream and can
  be presented / reported on. Also the run channel's final value
  (by ref).
- :class:`WrappedEntity` — a small demo output type for the
  ``wrap_entity`` basic task.
- :class:`NoteWithLinks` — a ``presentables.Note`` variant carrying a
  ``links`` dict of named entity references (provenance: tasks link
  their outputs back to what they were derived from). Kept separate
  from ``Note`` because that type is specially mounted into the
  workstation server.
- :class:`PromptTemplate` — a stored prompt template; ``prompt_task``
  takes its FxtrID at connect time and links every output back to it.

Note-consuming tasks should load inputs with
``target_type=Note | NoteWithLinks`` so chains compose regardless of
which step (links-aware or not) produced the intermediate. (The
union lives at each use site, not here — this package deliberately
has no dependency on ``presentables``; the scheduler's spawn env
imports it without ``presentables`` installed.)

Event contract: per-run output channels
---------------------------------------

The scheduler's own event stream carries **run announcements**, not
per-task progress. Each ``run_batched`` creates a per-run output
channel (:func:`fxtr2.common.channels.create_root_channel`) tagged with
a :class:`SchedulerRunTag`; the per-run deltas flow through that
channel. A channel is fronted by a **reader** service (handed to
consumers) and a **writer** (owned by the scheduler). Channel message
payloads are plain ``FxtrServiceData`` and carry **only data** (entity
refs + scalars) — never service refs.

Scheduler-level events (the scheduler is an ``EventSource``; its hub
runs in ``"queue"`` mode — these CAN carry service refs):

- ``{"kind": "run_channel", "run_id": str,
   "reader": <ServiceRef[FxtrChannelReader]>}`` — published when a run
  starts. Also delivered as the seed (first event) of every new
  subscription while that run is the current/most-recent one — possibly
  long after the run finished, in which case the announced channel is
  simply complete. Subscribers arriving before any run get no seed.
- ``{"kind": "scheduler_closing"}`` — the scheduler is shutting down
  (its mount drained); no further events will arrive and the
  subscription is about to be torn down source-side.

Per-run channel messages (``V = FxtrServiceData``; subscribe to the
announced reader's ``subscribe_full`` — replay-then-live semantics,
snapshots and lifecycle events are the channel system's, see
:mod:`fxtr2.common.channels`):

- ``{"kind": "run_inputs", "inputs": {name: FxtrID}}`` — always the
  first message; the per-input entity ids (data-only, so no step
  ``ServiceRef``\\ s — the step display labels live on the
  :class:`SchedulerRunTag`, and the static run metadata — run_id /
  step_labels / input_names — lives there too).
- ``{"kind": "task_started", "run_id", "input_name", "step_index"}``
- ``{"kind": "task_result", "run_id", "input_name", "step_index",
   "output": FxtrID}``
- ``{"kind": "task_failed", "run_id", "input_name", "step_index",
   "error": str}``

(``run_id`` on the ``task_*`` messages is redundant with the tag —
kept for vocabulary continuity.)

The channel's **final value** (``R``) is an
``EntityRef[SimpleRunResult]``: when the run finishes, subscribers
receive the channel's complete event carrying the durable result ref
and the message log is dropped — late subscribers see only the final
ref. There is no ``run_completed`` event; completion *is* the
channel finishing.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Literal, Protocol

from fxtr2.common.channels import ChannelTag
from fxtr2.conversion.converters.mashumaro_entities import ChildFxtrID, DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import service_protocol

#: Namespace UUID stable across host + sandbox so wire-form
#: ``fxtr_type`` FxtrIDs match across processes.
SIMPLE_TASKS_NS = uuid.UUID("3f7a2d91-8c4e-4b6a-a0d5-2e9c1f7b8a36")

#: Service type id shared by every SimpleTask impl (so a renderer /
#: tool can route "any task" uniformly).
SIMPLE_TASK_TYPE_ID = system_entities.define_type(SIMPLE_TASKS_NS, "simple_task")

#: The scheduler's service type id — pinned; the grid renderer routes
#: on this.
SIMPLE_SCHEDULER_TYPE_ID = system_entities.define_type(
    SIMPLE_TASKS_NS, "simple_task_sequence_scheduler"
)


@service_protocol
class SimpleTaskProto(Protocol):
    """One entity-to-entity transform step."""

    async def run(self, input: FxtrID) -> FxtrID:
        """Run this task on an input entity, producing an output
        entity.

        Opaque ``FxtrID`` at the protocol level for easy composition;
        implementations typically load against a concrete
        ``EntityRef[<type>]`` (type substitution is handled separately
        on the ref and impl sides).
        """
        ...


@service_protocol
class SimpleTaskSequenceSchedulerProto(EventSource[FxtrServiceData], Protocol):
    """Runs batches of inputs through a sequential task chain.

    A genuine :class:`~fxtr2.services.subscription_types.EventSource`
    subclass — the scheduler publishes pre-encoded wire dicts
    (``FxtrServiceData``); ``subscribe`` is re-declared below purely
    for its docstring.
    """

    async def run_batched(
        self,
        steps: list[ServiceRef[SimpleTaskProto]],
        inputs: dict[str, FxtrID],
        labels: list[str] | None = None,
    ) -> FxtrID:
        """Run every input through every step, concurrently across
        inputs (each input's steps run as a sequential chain: step
        ``i``'s output is step ``i+1``'s input).

        Blocks until any in-flight ``run_batched`` finishes — only one
        run at a time. A failed cell stops that input's chain; other
        inputs run to completion.

        Returns the ``FxtrID`` of a :class:`SimpleRunResult` holding
        every intermediate output + the per-input failures.

        ``labels`` (optional) names the steps for display; defaults to
        ``step 1..n``.
        """
        ...

    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to run announcements (shapes in
        the module docstring).

        Re-declaration of the inherited
        :meth:`fxtr2.services.subscription_types.EventSource.subscribe`
        purely for this docstring (same annotations, same wire shape).

        The subscriber's first event is the current/most-recent run's
        ``run_channel`` announcement (none if no run has started yet);
        later runs' announcements stream after it. Per-task progress
        is NOT on this stream — subscribe to the announced reader's
        ``subscribe_full`` for that. The standard
        subscription contract applies: hold the returned handle to
        keep the subscription (release/unsubscribe when done); hold
        the scheduler ref itself to keep the scheduler alive — neither
        implies the other. When the scheduler shuts down, subscribers
        receive a ``scheduler_closing`` event and the subscription
        ends.
        """
        ...

    async def debug_state(self) -> FxtrServiceData:
        """Demo introspection: live subscription count + recent
        delivery/publish errors."""
        ...


@dataclasses.dataclass(frozen=True)
class SimpleRunResult(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SIMPLE_TASKS_NS, "simple_run_result"),
):
    """Durable record of one ``run_batched`` call.

    ``cells[input_name]`` maps ``str(step_index)`` to the output
    entity of that step (strong links — the result keeps every
    intermediate alive). A missing key means the step never produced
    an output (its chain failed earlier). ``failures[input_name]`` is
    ``(failed_step_index, error_message)``; absence means the input
    ran the full chain.

    ``cached[input_name]`` maps ``str(step_index)`` to whether that
    step's output was served from the scheduler's cache (no task
    invocation) rather than freshly computed. Steps absent from the
    map were freshly computed (the default empty map means no caching
    — e.g. the simple scheduler, which never sets it).

    ``completed_at`` is an ISO-8601 UTC timestamp stamped by the
    scheduler when the run finishes (``None`` on results stored by
    older scheduler builds).
    """

    run_id: str
    step_labels: tuple[str, ...]
    input_names: tuple[str, ...]
    inputs: dict[str, ChildFxtrID]
    cells: dict[str, dict[str, ChildFxtrID]]
    failures: dict[str, tuple[int, str]]
    cached: dict[str, dict[str, bool]] = dataclasses.field(default_factory=dict)
    completed_at: str | None = None


@dataclasses.dataclass(frozen=True)
class RunStatus:
    """The run channel's replace-only **status** (the channel's ``S``).

    A plain data value (serializes to ``FxtrChannelData`` as a dict), not a
    stored entity — it rides the channel's status dimension and is delivered
    by ``subscribe_state`` / carried on every ``subscribe_full`` update. The
    scheduler sets ``running`` at ``start`` and ``done`` at ``finish``
    (``detail`` is a short human-readable note, e.g. a progress count)."""

    phase: Literal["running", "done", "error"]
    detail: str | None = None


@dataclasses.dataclass(frozen=True)
class SchedulerRunTag(
    DataclassEntity,
    ChannelTag[RunStatus, FxtrServiceData, EntityRef[SimpleRunResult]],
    fxtr_type=system_entities.define_type(SIMPLE_TASKS_NS, "scheduler_run_tag"),
):
    """Tag entity for one ``run_batched`` call's output channel.

    Carries the run's static metadata (run_id / step_labels /
    input_names); the per-input ``FxtrID``\\ s ride in the channel's
    first message, ``run_inputs``, since a stored tag entity can't hold
    them directly. The :class:`~fxtr2.common.channels.ChannelTag` base
    pins the channel's status type (:class:`RunStatus`), message type
    (``FxtrServiceData`` — the
    delta-event dicts in the module docstring), and final-value type
    (``EntityRef[SimpleRunResult]``), so ``create_root_channel`` and
    typed subscribers infer all three from the tag.

    The frontend dispatches per-stream rendering on this type: the
    demo's grid card is registered as a ``ChannelContentCard``
    renderer for it.
    """

    run_id: str
    step_labels: tuple[str, ...]
    input_names: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class WrappedEntity(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SIMPLE_TASKS_NS, "wrapped_entity"),
):
    """Demo output type: an entity wrapped with a label (the
    ``wrap_entity`` basic task produces these)."""

    label: str
    inner: ChildFxtrID


@dataclasses.dataclass(frozen=True)
class NoteWithLinks(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SIMPLE_TASKS_NS, "note_with_links"),
):
    """A ``presentables.Note`` variant with named provenance links.

    Same title/body surface as ``Note`` plus ``links`` — a dict of
    named entity references (strong links: the note keeps everything
    it was derived from alive, so a final output can be traced back
    step by step at the data layer). Deliberately a separate type
    from ``Note``, which is specially mounted into the workstation
    server.
    """

    title: str
    body: str
    links: dict[str, ChildFxtrID] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class PromptTemplate(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SIMPLE_TASKS_NS, "prompt_template"),
):
    """A stored LLM prompt template (``str.format`` style; tasks
    substitute ``{title}`` / ``{body}`` from their input note).

    ``prompt_task`` takes a ``PromptTemplate``'s FxtrID at connect
    time and adds a ``"prompt"`` link to every output, so a result
    records both its data input and the instructions that produced
    it.
    """

    label: str
    template: str
