"""Host-side tests for the ``simple_scheduler`` impl.

The scheduler is mounted the way a guest mounts it (on a scope that
starts draining right after the ref is handed out — the
connect-dispatch reality), with the scheduler ref held in a dedicated
holder scope and subscribers/tasks mounted in a sibling caller scope.
Every harness teardown therefore exercises the source-shutdown path:
dropping the holder scope drains the mount, the lifespan teardown
publishes ``scheduler_closing`` + flushes, and the hub tears the
remaining subscriptions down (releasing the subscriber mounts so the
caller scope can close).

Focus areas (the stage-13post1 part-3 list): the ``run_channel``
announcement carries a usable **reader** ref (resolve + ``get_tag()`` →
tag loads, ``peek_state()`` → stage), mid-run channel subscription
composes (snapshot replay + batched deltas fold to the same grid as a
from-start subscriber), post-run channel subscription sees only the
final ``SimpleRunResult`` ref, post-run *scheduler* subscription seeds
the most-recent run's announcement, failed cells still finish the run,
and the close message on teardown.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import anyio.abc
import pytest
from fxtr2.common.channels import ChannelStateEvent, ChannelUpdate, FxtrChannelReader
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    EntityRef,
    FxtrScope,
    ServiceKey,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from simple_scheduler import _SchedulerImpl
from simple_tasks import (
    SIMPLE_TASK_TYPE_ID,
    NoteWithLinks,
    RunStatus,
    SchedulerRunTag,
    SimpleRunResult,
    SimpleTaskProto,
)

pytestmark = pytest.mark.anyio

#: The fully parameterized reader annotation for a run channel — what
#: wire-received reader refs are retyped to for typed calls.
_RUN_READER_TYPE = FxtrChannelReader[
    SchedulerRunTag, RunStatus, FxtrServiceData, EntityRef[SimpleRunResult]
]
#: The subscriber annotation the reader's ``subscribe_full`` expects.
_RUN_UPDATE_SUBSCRIBER = Subscriber[
    ChannelUpdate[RunStatus, FxtrServiceData, EntityRef[SimpleRunResult]]
]


# ---------------------------------------------------------------------------
# Test impls
# ---------------------------------------------------------------------------


class _ShoutTask(TypedServiceImpl[SimpleTaskProto]):
    """Loads a NoteWithLinks, returns an uppercased copy."""

    def _target_type_annotation(self) -> Any:
        return SimpleTaskProto

    def _service_type_id(self) -> FxtrID:
        return SIMPLE_TASK_TYPE_ID

    @service_method
    async def run(self, input: FxtrID) -> FxtrID:
        note = await EntityRef(entity_id=input, target_type=NoteWithLinks).load()
        out = NoteWithLinks(
            title=f"{note.title} (SHOUTED)", body=note.body.upper(), links={"input": input}
        )
        return out.store().entity_id


class _GatedTask(TypedServiceImpl[SimpleTaskProto]):
    """Each ``run`` parks on the semaphore until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = anyio.Semaphore(0)

    def _target_type_annotation(self) -> Any:
        return SimpleTaskProto

    def _service_type_id(self) -> FxtrID:
        return SIMPLE_TASK_TYPE_ID

    @service_method
    async def run(self, input: FxtrID) -> FxtrID:
        await self.gate.acquire()
        return NoteWithLinks(title="gated", body="done", links={"input": input}).store().entity_id


class _FailingTask(TypedServiceImpl[SimpleTaskProto]):
    def _target_type_annotation(self) -> Any:
        return SimpleTaskProto

    def _service_type_id(self) -> FxtrID:
        return SIMPLE_TASK_TYPE_ID

    @service_method
    async def run(self, input: FxtrID) -> FxtrID:
        del input
        raise ValueError("task boom")


def _task_output_ids(payload: dict[str, Any]) -> list[FxtrID]:
    """Output entity ids referenced by one channel wire event
    (``task_result`` messages inside batched ``update`` deltas / the
    ``snapshot_open`` replay)."""
    if payload.get("kind") == "update":
        messages: list[Any] = list(payload["events"])
    elif payload.get("kind") == "snapshot_open":
        messages = list(payload["values"])
    else:
        return []
    return [m["output"] for m in messages if isinstance(m, dict) and m.get("kind") == "task_result"]


class _RecordingSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Records every payload (works against both the scheduler hub and
    a channel storage — payloads are wire dicts either way).

    ``load_outputs=True`` additionally loads each ``task_result``
    output found in channel events inside the delivery dispatch —
    exercising payload-entity reachability through the per-delivery
    scope (the channel's retention lease) end-to-end.

    ``bridge_channels_into=<scope>`` bridges any ``run_channel``
    announcement's reader ref into that scope inside the delivery
    dispatch (the ``iter_channel`` internal-subscriber pattern), so
    the test can resolve / subscribe to the channel afterwards.
    """

    def __init__(
        self,
        *,
        load_outputs: bool = False,
        bridge_channels_into: FxtrScope | None = None,
    ) -> None:
        super().__init__()
        self.received: list[Any] = []
        self.loaded_titles: list[str] = []
        self._load_outputs = load_outputs
        self._bridge_into = bridge_channels_into

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-recording-subscriber"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        self.received.append(payload)
        if not isinstance(payload, dict):
            return
        reader = payload.get("reader")
        if self._bridge_into is not None and isinstance(reader, ServiceRef):
            key = ServiceKey(handle=reader.handle, type_id=reader.service_type_id)
            if key not in self._bridge_into._known_handles:  # noqa: SLF001 — iter_channel's pattern
                await self._bridge_into.bridge_service_from_scope(
                    reader, self._fxtr_get_input_scope(), keepalive_via="independent_lease"
                )
        if self._load_outputs:
            for fid in _task_output_ids(payload):
                loaded = await EntityRef(entity_id=fid, target_type=NoteWithLinks).load()
                self.loaded_titles.append(loaded.title)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Harness:
    caller: FxtrScope
    work: FxtrScope
    sched_ref: ServiceRef[Any]
    proxy: Any
    tg: anyio.abc.TaskGroup

    async def mount_subscriber(self, impl: TypedServiceImpl[Any]) -> ServiceRef[Any]:
        return await self.caller.mount(impl)

    async def mount_task(self, impl: TypedServiceImpl[Any]) -> ServiceRef[Any]:
        return await self.caller.mount(impl)

    def store_input(self, title: str) -> FxtrID:
        return NoteWithLinks(title=title, body=f"body of {title}").store(self.caller).entity_id

    async def watch_scheduler(self, *, load_outputs: bool = False) -> _RecordingSubscriberImpl:
        """Subscribe a recording subscriber (announcement-bridging into
        ``work``) to the scheduler."""
        sub = _RecordingSubscriberImpl(load_outputs=load_outputs, bridge_channels_into=self.work)
        sub_ref = await self.mount_subscriber(sub)
        await self.proxy.subscribe(sub_ref)
        return sub

    async def watch_channel(
        self, reader_ref: ServiceRef[Any], *, load_outputs: bool = False
    ) -> _RecordingSubscriberImpl:
        """Subscribe a recording subscriber to a (work-bridged) channel
        reader ref via ``subscribe_full``."""
        sub = _RecordingSubscriberImpl(load_outputs=load_outputs)
        sub_ref = await self.mount_subscriber(sub)
        reader_proxy = await reader_ref.replace_service_type(_RUN_READER_TYPE).resolve(self.work)
        await reader_proxy.subscribe_full(sub_ref.replace_service_type(_RUN_UPDATE_SUBSCRIBER))
        return sub

    async def watch_state(self, reader_ref: ServiceRef[Any]) -> _RecordingSubscriberImpl:
        """Subscribe a recording subscriber to a run reader's cheap
        replace-only ``subscribe_state`` stream (records ``ChannelState``
        wire dicts: ``{status, kind}``)."""
        sub = _RecordingSubscriberImpl()
        sub_ref = await self.mount_subscriber(sub)
        reader_proxy = await reader_ref.replace_service_type(_RUN_READER_TYPE).resolve(self.work)
        await reader_proxy.subscribe_state(
            sub_ref.replace_service_type(Subscriber[ChannelStateEvent[RunStatus]])
        )
        return sub


@asynccontextmanager
async def _scheduler_harness() -> AsyncGenerator[_Harness]:
    """Scheduler mounted guest-style: the mount scope starts draining
    as soon as the ref is bridged out (the host task exits its CM
    immediately), held open by the ref bridge in ``ref_scope``. The
    ``work`` scope (seeing both the ref bridge and the caller's mounts)
    is where dispatches are sent from.

    Teardown order is the source-shutdown walkthrough: ``work`` closes
    (dropping any handles still held there), ``ref_scope`` closes (the
    last scheduler-ref holder → the mount drains → ``scheduler_closing``
    \\+ ``hub.aclose()``), then ``caller`` closes (its subscriber mounts
    were released by the hub teardown).
    """
    async with anyio.create_task_group() as tg:
        async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
            async with FxtrScope(
                name="sched_ref_holder", include=[GLOBAL_SCOPE], isolated=True
            ) as ref_scope:

                async def _host(
                    *,
                    task_status: anyio.abc.TaskStatus[ServiceRef[Any]] = anyio.TASK_STATUS_IGNORED,
                ) -> None:
                    async with FxtrScope(
                        name="sched_mount", include=[GLOBAL_SCOPE], isolated=True
                    ) as mount:
                        ref = await mount.mount(_SchedulerImpl())
                        await ref_scope.bridge_service_from_scope(
                            ref, mount, keepalive_via="independent_lease"
                        )
                        task_status.started(ref)
                    # The CM exits at once: the mount drains, kept open
                    # only by ref_scope's bridge (the guest reality).

                sched_ref = await tg.start(_host)
                async with FxtrScope(
                    name="work", include=[ref_scope, caller], isolated=True
                ) as work:
                    proxy = await sched_ref.resolve(work)
                    yield _Harness(
                        caller=caller, work=work, sched_ref=sched_ref, proxy=proxy, tg=tg
                    )


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


async def _settle() -> None:
    await anyio.sleep(0.1)


# ---------------------------------------------------------------------------
# Folds over channel wire events
# ---------------------------------------------------------------------------


def _channel_messages(events: list[Any]) -> list[dict[str, Any]]:
    """Replay a channel subscription's wire events into the message
    log (snapshot replay + live emits, in order)."""
    messages: list[dict[str, Any]] = []
    for ev in events:
        kind = ev.get("kind")
        if kind == "snapshot_open":
            messages = list(ev["values"])
        elif kind == "update":
            messages.extend(ev["events"])
    return messages


def _channel_result(events: list[Any]) -> FxtrID | None:
    for ev in events:
        if ev.get("kind") in ("complete", "snapshot_complete"):
            return ev["result"]
    return None


def _fold_cells(messages: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Fold ``task_*`` messages into per-cell terminal state (same
    semantics as the grid renderer's fold; ``run_inputs`` carries no
    cell state)."""
    cells: dict[tuple[str, int], dict[str, Any]] = {}
    for m in messages:
        kind = m.get("kind")
        if kind not in ("task_started", "task_result", "task_failed"):
            continue
        key = (m["input_name"], m["step_index"])
        if kind == "task_started":
            if cells.get(key, {}).get("status") != "done":
                cells[key] = {"status": "running"}
        elif kind == "task_result":
            cells[key] = {"status": "done", "output": m["output"]}
        else:
            cells[key] = {"status": "failed", "error": m["error"]}
    return cells


def _count_kind(events: list[Any], kind: str) -> int:
    return sum(1 for m in _channel_messages(events) if m.get("kind") == kind)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_run_announcement_and_channel_status() -> None:
    """Plan item 1: the scheduler announces ``run_channel`` with a
    usable reader ref — resolve + ``get_tag()`` yields the tag (loads to
    the run's static metadata), ``peek_state()`` yields the stage."""
    async with _scheduler_harness() as h:
        sched_sub = await h.watch_scheduler()
        await _settle()
        assert sched_sub.received == []  # no run yet -> no seed

        task_ref = await h.mount_task(_ShoutTask())
        inputs = {"a": h.store_input("a"), "b": h.store_input("b")}
        result_id = await h.proxy.run_batched([task_ref, task_ref], inputs, ["s1", "s2"])

        await _wait_until(lambda: len(sched_sub.received) == 1)
        (announcement,) = sched_sub.received
        assert announcement["kind"] == "run_channel"
        assert isinstance(announcement["reader"], ServiceRef)

        # Typed path: retype the wire ref, get_tag() decodes, tag loads.
        reader = announcement["reader"].replace_service_type(_RUN_READER_TYPE)
        reader_proxy = await reader.resolve(h.work)
        tag_ref = await reader_proxy.get_tag()
        tag = await tag_ref.load(h.work)
        assert isinstance(tag, SchedulerRunTag)
        assert tag.run_id == announcement["run_id"]
        assert tag.step_labels == ("s1", "s2")
        assert tag.input_names == ("a", "b")
        # The run is finished by now: peek_state reports the complete stage
        # and the final typed status (the channel's S dimension).
        state = await reader_proxy.peek_state()
        assert state.kind == "complete"
        assert isinstance(state.status, RunStatus)
        assert state.status.phase == "done"

        # The durable result loads from the caller side too (returned
        # by the dispatch, so it's tracked in the awaiting scope).
        result = await EntityRef(entity_id=result_id, target_type=SimpleRunResult).load(h.work)
        assert result.input_names == ("a", "b")
        assert set(result.cells["a"]) == {"0", "1"}

        state = await h.proxy.debug_state()
        assert state["live_subscriptions"] == 1
        assert state["has_run"] is True
        assert state["run_in_flight"] is False


async def test_mid_run_channel_seed_and_deltas_compose() -> None:
    """Plan item 2: a mid-run channel subscriber's snapshot replay +
    deltas fold to the same grid as a from-start subscriber's full
    stream."""
    async with _scheduler_harness() as h:
        sched_sub = await h.watch_scheduler()

        gated = _GatedTask()
        task_ref = await h.mount_task(gated)
        inputs = {"a": h.store_input("a"), "b": h.store_input("b")}
        result_box: dict[str, FxtrID] = {}

        async def _run() -> None:
            result_box["id"] = await h.proxy.run_batched([task_ref], inputs)

        h.tg.start_soon(_run)

        # Subscribe to the channel as soon as it's announced.
        await _wait_until(lambda: len(sched_sub.received) == 1)
        early = await h.watch_channel(sched_sub.received[0]["reader"], load_outputs=True)

        # Let exactly one cell finish, then subscribe mid-run.
        await _wait_until(lambda: _count_kind(early.received, "task_started") == 2)
        gated.gate.release()
        await _wait_until(lambda: _count_kind(early.received, "task_result") == 1)

        late = await h.watch_channel(sched_sub.received[0]["reader"], load_outputs=True)
        await _wait_until(lambda: len(late.received) >= 1)
        snap = late.received[0]
        assert snap["kind"] == "snapshot_open"  # the replay precedes any delta
        assert snap["values"][0]["kind"] == "run_inputs"
        statuses = sorted(c["status"] for c in _fold_cells(list(snap["values"])).values())
        assert statuses == ["done", "running"]

        gated.gate.release()
        await _wait_until(lambda: "id" in result_box)
        await _wait_until(lambda: _channel_result(late.received) is not None)
        await _wait_until(lambda: _channel_result(early.received) is not None)

        # Snapshot consistency (the headline): replay + deltas fold to
        # the same grid as the from-start subscriber's full stream.
        assert _fold_cells(_channel_messages(late.received)) == _fold_cells(
            _channel_messages(early.received)
        )
        assert _channel_result(late.received) == result_box["id"]
        # The run_inputs message replays identically — data-only, carrying
        # the per-input entity ids (no service refs in channel messages).
        late_inputs = _channel_messages(late.received)[0]
        assert late_inputs["kind"] == "run_inputs"
        assert set(late_inputs["inputs"]) == {"a", "b"}
        # Outputs were loadable inside both subscribers' delivery
        # dispatches (live updates AND the snapshot replay).
        assert early.loaded_titles == ["gated", "gated"]
        assert late.loaded_titles[0] == "gated"  # the replayed task_result


async def test_post_run_channel_subscribe_sees_final_only() -> None:
    """Plan item 3: subscribing to a finished run's channel delivers
    exactly one ``snapshot_complete`` carrying the loadable
    ``SimpleRunResult`` ref — the message log is gone."""
    async with _scheduler_harness() as h:
        sched_sub = await h.watch_scheduler()
        task_ref = await h.mount_task(_ShoutTask())
        result_id = await h.proxy.run_batched([task_ref], {"x": h.store_input("x")})

        await _wait_until(lambda: len(sched_sub.received) == 1)
        sub = await h.watch_channel(sched_sub.received[0]["reader"])
        await _wait_until(lambda: len(sub.received) == 1)
        (snap,) = sub.received
        assert snap["kind"] == "snapshot_complete"
        assert snap["result"] == result_id
        # The final value's closure is retained against the scheduler's
        # scope — it loads long after the run dispatch drained.
        result = await EntityRef(entity_id=snap["result"], target_type=SimpleRunResult).load(h.work)
        assert result.input_names == ("x",)

        # Post-run peek_state reports the complete stage.
        reader_proxy = await (
            sched_sub.received[0]["reader"].replace_service_type(_RUN_READER_TYPE).resolve(h.work)
        )
        assert (await reader_proxy.peek_state()).kind == "complete"

        await _settle()
        assert len(sub.received) == 1  # the snapshot never repeats


async def test_post_run_scheduler_subscribe_seeds_announcement() -> None:
    """Plan item 4: a scheduler subscriber arriving after the run gets
    the seeded ``run_channel`` announcement (ServiceRef-in-seed), and
    the announced channel is complete."""
    async with _scheduler_harness() as h:
        task_ref = await h.mount_task(_ShoutTask())
        result_id = await h.proxy.run_batched([task_ref], {"x": h.store_input("x")})

        sub = await h.watch_scheduler()
        await _wait_until(lambda: len(sub.received) == 1)
        (announcement,) = sub.received
        assert announcement["kind"] == "run_channel"
        # The ServiceRef-in-seed risk item: the storage ref decodes and
        # is usable from the seed alone.
        assert isinstance(announcement["reader"], ServiceRef)
        ch_sub = await h.watch_channel(announcement["reader"])
        await _wait_until(lambda: len(ch_sub.received) == 1)
        assert ch_sub.received[0]["kind"] == "snapshot_complete"
        assert ch_sub.received[0]["result"] == result_id

        await _settle()
        assert len(sub.received) == 1  # the seed never repeats


async def test_second_run_swaps_announcement_seed() -> None:
    async with _scheduler_harness() as h:
        early = await h.watch_scheduler()
        task_ref = await h.mount_task(_ShoutTask())
        await h.proxy.run_batched([task_ref], {"first": h.store_input("first")})
        second_result = await h.proxy.run_batched([task_ref], {"second": h.store_input("second")})

        # The live stream carried both announcements, in order.
        await _wait_until(lambda: len(early.received) == 2)
        first_ann, second_ann = early.received
        assert first_ann["run_id"] != second_ann["run_id"]

        # A late subscriber seeds with the most recent run only.
        late = await h.watch_scheduler()
        await _wait_until(lambda: len(late.received) == 1)
        assert late.received[0]["run_id"] == second_ann["run_id"]
        late_ch = await h.watch_channel(late.received[0]["reader"])
        await _wait_until(lambda: len(late_ch.received) == 1)
        assert late_ch.received[0]["result"] == second_result

        # Channels stay mounted: the FIRST run's channel still answers
        # (complete) — no previous-run retention swap anymore.
        first_ch = await h.watch_channel(first_ann["reader"])
        await _wait_until(lambda: len(first_ch.received) == 1)
        assert first_ch.received[0]["kind"] == "snapshot_complete"


async def test_failed_cell_reported() -> None:
    """Plan item 5a: a failing cell streams ``task_failed`` and the run
    still *finishes* (the result entity records the failure)."""
    async with _scheduler_harness() as h:
        sched_sub = await h.watch_scheduler()
        gated = _GatedTask()
        ok_ref = await h.mount_task(gated)
        fail_ref = await h.mount_task(_FailingTask())
        result_box: dict[str, FxtrID] = {}

        async def _run() -> None:
            result_box["id"] = await h.proxy.run_batched(
                [ok_ref, fail_ref], {"a": h.store_input("a")}, ["ok", "boom"]
            )

        h.tg.start_soon(_run)
        await _wait_until(lambda: len(sched_sub.received) == 1)
        sub = await h.watch_channel(sched_sub.received[0]["reader"])
        gated.gate.release()
        await _wait_until(lambda: "id" in result_box)
        await _wait_until(lambda: _channel_result(sub.received) is not None)

        cells = _fold_cells(_channel_messages(sub.received))
        assert cells[("a", 0)]["status"] == "done"
        assert cells[("a", 1)]["status"] == "failed"
        assert "task boom" in cells[("a", 1)]["error"]

        result = await EntityRef(entity_id=result_box["id"], target_type=SimpleRunResult).load(
            h.work
        )
        assert result.failures["a"][0] == 1
        assert "task boom" in result.failures["a"][1]
        assert set(result.cells["a"]) == {"0"}  # step 0 produced, step 1 didn't


async def test_unsubscribe_stops_delivery() -> None:
    async with _scheduler_harness() as h:
        sub = _RecordingSubscriberImpl()
        sub_ref = await h.mount_subscriber(sub)
        handle_ref = await h.proxy.subscribe(sub_ref)
        assert handle_ref.target_type is UnsubscribeHandle

        handle = await handle_ref.resolve(h.work)
        await handle.unsubscribe()

        task_ref = await h.mount_task(_ShoutTask())
        await h.proxy.run_batched([task_ref], {"a": h.store_input("a")})
        await _settle()
        assert sub.received == []

        state = await h.proxy.debug_state()
        assert state["live_subscriptions"] == 0


async def test_scheduler_closing_on_ref_drop() -> None:
    """The end-to-end source-shutdown walkthrough: the last holder of
    the scheduler ref drops it while a subscription is live → the mount
    drains → subscribers receive ``scheduler_closing`` and the
    subscription is torn down source-side (no unsubscribe call)."""
    async with anyio.create_task_group() as tg:
        async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
            sub = _RecordingSubscriberImpl()
            sub_ref = await caller.mount(sub)

            async with FxtrScope(
                name="sched_ref_holder", include=[GLOBAL_SCOPE], isolated=True
            ) as ref_scope:

                async def _host(
                    *,
                    task_status: anyio.abc.TaskStatus[ServiceRef[Any]] = anyio.TASK_STATUS_IGNORED,
                ) -> None:
                    async with FxtrScope(
                        name="sched_mount", include=[GLOBAL_SCOPE], isolated=True
                    ) as mount:
                        ref = await mount.mount(_SchedulerImpl())
                        await ref_scope.bridge_service_from_scope(
                            ref, mount, keepalive_via="independent_lease"
                        )
                        task_status.started(ref)

                sched_ref = await tg.start(_host)
                async with FxtrScope(
                    name="work", include=[ref_scope, caller], isolated=True
                ) as work:
                    proxy = await sched_ref.resolve(work)
                    handle_ref = await proxy.subscribe(sub_ref)
                    # Keep the subscription alive past `work`: the handle
                    # is the keepalive, so move it into `caller`.
                    await caller.bridge_service_from_scope(
                        handle_ref, work, keepalive_via="independent_lease"
                    )
            # ref_scope is gone: the subscription alone must not keep the
            # scheduler alive — the mount drains and says goodbye.
            await _wait_until(lambda: any(e == {"kind": "scheduler_closing"} for e in sub.received))


async def test_subscribe_state_tracks_run_status() -> None:
    """Part 5: the run channel carries a typed ``RunStatus`` on its status
    dimension — ``subscribe_state`` delivers it (coalesce, latest-wins:
    running while in flight → done at finish), and it also threads onto every
    ``subscribe_full`` event."""
    async with _scheduler_harness() as h:
        sched_sub = await h.watch_scheduler()
        gated = _GatedTask()
        task_ref = await h.mount_task(gated)
        inputs = {"a": h.store_input("a")}
        result_box: dict[str, FxtrID] = {}

        async def _run() -> None:
            result_box["id"] = await h.proxy.run_batched([task_ref], inputs)

        h.tg.start_soon(_run)
        await _wait_until(lambda: len(sched_sub.received) == 1)
        reader = sched_sub.received[0]["reader"]

        # While the gated task is in flight: state stream says running.
        state_sub = await h.watch_state(reader)
        await _wait_until(lambda: len(state_sub.received) >= 1)
        running = state_sub.received[-1]
        assert running["kind"] == "open"
        assert running["status"]["phase"] == "running"

        # The status also rides the full stream (snapshot carries it).
        full_sub = await h.watch_channel(reader)
        await _wait_until(lambda: len(full_sub.received) >= 1)
        assert full_sub.received[0]["status"]["phase"] == "running"

        # Finish → the state stream coalesces to done.
        gated.gate.release()
        await _wait_until(lambda: "id" in result_box)
        await _wait_until(
            lambda: bool(state_sub.received) and state_sub.received[-1]["kind"] == "complete"
        )
        done = state_sub.received[-1]
        assert done["status"]["phase"] == "done"
