"""Host-side tests for the ``streaming_scheduler`` impl (nested channels).

Same harness layout as ``test_simple_scheduler_impl.py`` (scheduler
mounted guest-style; subscribers/tasks in a sibling caller scope).
Focus areas (the stage-13post1 part-4 list): the caller-passes-**writer**
pattern (``word_shout`` run directly against a hand-created channel whose
writer it owns), ``cell_channel`` **key** announcements on the run channel
+ ``open_subchannel_reader(key)`` reaching a cell whose mid-task replay
composes with the live partials, a failing task aborting its cell channel
while the run still finishes, and a late run-channel subscriber seeing only
the final value.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import anyio.abc
import pytest
from fxtr2.common.channels import (
    ChannelUpdate,
    FxtrChannelReader,
    create_root_channel,
    start_channel_and_infer_types,
)
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
from fxtr2.services.subscription_types import Subscriber
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

import streaming_task_lib
from simple_tasks import NoteWithLinks, SimpleRunResult
from streaming_scheduler import _SchedulerImpl
from streaming_task_lib import _WordShoutImpl
from streaming_tasks import (
    STREAMING_TASK_TYPE_ID,
    StreamingRunTag,
    StreamingTaskProto,
    TaskProgressTag,
)

pytestmark = pytest.mark.anyio

#: Fully parameterized run-channel reader + its full-stream subscriber.
_RUN_READER_TYPE = FxtrChannelReader[
    StreamingRunTag, None, FxtrServiceData, EntityRef[SimpleRunResult]
]
_RUN_UPDATE_SUB = Subscriber[ChannelUpdate[None, FxtrServiceData, EntityRef[SimpleRunResult]]]
#: Fully parameterized cell-channel reader + its full-stream subscriber.
_CELL_READER_TYPE = FxtrChannelReader[TaskProgressTag, None, FxtrServiceData, FxtrID]
_CELL_UPDATE_SUB = Subscriber[ChannelUpdate[None, FxtrServiceData, FxtrID]]


@pytest.fixture(autouse=True)
def _fast_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink word_shout's inter-word delay: enough of a window to
    subscribe mid-task, without dragging the suite out."""
    monkeypatch.setattr(streaming_task_lib, "_WORD_DELAY_SECONDS", 0.05)


# ---------------------------------------------------------------------------
# Test impls
# ---------------------------------------------------------------------------


class _FailingStreamingTask(TypedServiceImpl[StreamingTaskProto]):
    """Owns its cell channel: starts it, emits one partial, then raises."""

    def _target_type_annotation(self) -> Any:
        return StreamingTaskProto

    def _service_type_id(self) -> FxtrID:
        return STREAMING_TASK_TYPE_ID

    @service_method
    async def run(self, input: FxtrID, channel: ServiceRef[Any]) -> FxtrID:
        del input
        scope = get_active_scope()
        tag = TaskProgressTag(task_label="boom").store(scope)
        writer_ref = await start_channel_and_infer_types(channel, tag, None, scope=scope)
        writer = await writer_ref.resolve(scope)
        await writer.emit(events=[{"kind": "partial", "text": "about to"}])
        raise ValueError("streaming boom")


class _RecordingSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Records every payload; optionally bridges any ``run_channel``
    announcement's **reader** ref into ``bridge_channels_into`` inside the
    delivery dispatch (the ``iter_channel`` pattern), so the test can
    resolve the run reader (and open its subchannels) afterwards."""

    def __init__(self, *, bridge_channels_into: FxtrScope | None = None) -> None:
        super().__init__()
        self.received: list[Any] = []
        self._bridge_into = bridge_channels_into

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-recording-subscriber"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        self.received.append(payload)
        if self._bridge_into is None or not isinstance(payload, dict):
            return
        reader = payload.get("reader")
        if isinstance(reader, ServiceRef):
            key = ServiceKey(handle=reader.handle, type_id=reader.service_type_id)
            if key not in self._bridge_into._known_handles:  # noqa: SLF001 — iter_channel's pattern
                await self._bridge_into.bridge_service_from_scope(
                    reader, self._fxtr_get_input_scope(), keepalive_via="independent_lease"
                )


# ---------------------------------------------------------------------------
# Harness (same layout as test_simple_scheduler_impl.py)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Harness:
    caller: FxtrScope
    work: FxtrScope
    proxy: Any
    tg: anyio.abc.TaskGroup

    async def mount(self, impl: TypedServiceImpl[Any]) -> ServiceRef[Any]:
        return await self.caller.mount(impl)

    def store_input(self, title: str, body: str) -> FxtrID:
        return NoteWithLinks(title=title, body=body).store(self.caller).entity_id

    async def watch_scheduler(self) -> _RecordingSubscriberImpl:
        sub = _RecordingSubscriberImpl(bridge_channels_into=self.work)
        await self.proxy.subscribe(await self.mount(sub))
        return sub

    async def watch_run_channel(self, reader_ref: ServiceRef[Any]) -> _RecordingSubscriberImpl:
        """Subscribe a recorder to a run channel reader (``subscribe_full``)."""
        sub = _RecordingSubscriberImpl()
        reader_proxy = await reader_ref.replace_service_type(_RUN_READER_TYPE).resolve(self.work)
        await reader_proxy.subscribe_full(
            (await self.mount(sub)).replace_service_type(_RUN_UPDATE_SUB)
        )
        return sub

    async def open_and_watch_cell(
        self, run_reader_ref: ServiceRef[Any], key: str
    ) -> _RecordingSubscriberImpl:
        """``open_subchannel_reader(key)`` on the run reader → subscribe a
        recorder to the cell reader (``subscribe_full``)."""
        run_proxy = await run_reader_ref.replace_service_type(_RUN_READER_TYPE).resolve(self.work)
        cell_reader_ref = await run_proxy.open_subchannel_reader(key)
        sub = _RecordingSubscriberImpl()
        cell_proxy = await cell_reader_ref.replace_service_type(_CELL_READER_TYPE).resolve(
            self.work
        )
        await cell_proxy.subscribe_full(
            (await self.mount(sub)).replace_service_type(_CELL_UPDATE_SUB)
        )
        return sub


@asynccontextmanager
async def _scheduler_harness() -> AsyncGenerator[_Harness]:
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
                        ref = await mount.mount(_SchedulerImpl(cache_enabled=True))
                        await ref_scope.bridge_service_from_scope(
                            ref, mount, keepalive_via="independent_lease"
                        )
                        task_status.started(ref)

                sched_ref = await tg.start(_host)
                async with FxtrScope(
                    name="work", include=[ref_scope, caller], isolated=True
                ) as work:
                    proxy = await sched_ref.resolve(work)
                    yield _Harness(caller=caller, work=work, proxy=proxy, tg=tg)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 15.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


async def _settle() -> None:
    await anyio.sleep(0.1)


# ---------------------------------------------------------------------------
# Folds over channel wire events
# ---------------------------------------------------------------------------


def _messages(events: list[Any]) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for ev in events:
        kind = ev.get("kind")
        if kind == "snapshot_open":
            msgs = list(ev["values"])
        elif kind == "update":
            msgs.extend(ev["events"])
    return msgs


def _result(events: list[Any]) -> Any:
    for ev in events:
        if ev.get("kind") in ("complete", "snapshot_complete"):
            return ev["result"]
    return None


def _partial_texts(events: list[Any]) -> list[str]:
    return [m["text"] for m in _messages(events) if m.get("kind") == "partial"]


def _of_kind(events: list[Any], kind: str) -> list[dict[str, Any]]:
    return [m for m in _messages(events) if m.get("kind") == kind]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_word_shout_streams_through_a_handed_writer() -> None:
    """Plan item 5: the caller-passes-writer pattern, no scheduler — create
    a root channel, hand its (un-started) writer into ``run``; the task owns
    it (starts it with a TaskProgressTag, streams partials, finishes with the
    output). A reader subscriber watches the monotonically growing partials
    then the final value."""
    async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
        async with FxtrScope(name="home", include=[GLOBAL_SCOPE], isolated=True) as home:
            async with FxtrScope(name="work", include=[caller, home], isolated=True) as work:
                task_ref = await caller.mount(_WordShoutImpl())
                sub = _RecordingSubscriberImpl()
                sub_ref = await caller.mount(sub)
                input_id = (
                    NoteWithLinks(title="x", body="one two three four").store(caller).entity_id
                )

                # Un-started root channel; the task will start it.
                writer_ref, reader_ref = await create_root_channel(scope=home)
                reader_proxy = await reader_ref.replace_service_type(_CELL_READER_TYPE).resolve(
                    work
                )
                await reader_proxy.subscribe_full(sub_ref.replace_service_type(_CELL_UPDATE_SUB))

                task_proxy = await task_ref.resolve(work)
                out = await task_proxy.run(input_id, writer_ref)

                await _wait_until(lambda: _result(sub.received) is not None)
                partials = _partial_texts(sub.received)
                assert partials == [
                    "ONE",
                    "ONE TWO",
                    "ONE TWO THREE",
                    "ONE TWO THREE FOUR",
                ]
                assert _result(sub.received) == out
                loaded = await EntityRef(entity_id=out, target_type=NoteWithLinks).load(work)
                assert loaded.body == "ONE TWO THREE FOUR"


async def test_run_channel_announces_cells_and_mid_task_replay_composes() -> None:
    """Plan items 1+2: ``cell_channel`` key announcements ride the run
    channel; ``open_subchannel_reader(key)`` reaches the cell, and a mid-task
    cell subscriber's snapshot replay + live deltas equal a from-start
    subscriber's stream."""
    async with _scheduler_harness() as h:
        sched_sub = await h.watch_scheduler()
        task_ref = await h.mount(_WordShoutImpl())
        inputs = {"a": h.store_input("a", "alpha beta gamma delta epsilon zeta eta theta")}
        result_box: dict[str, FxtrID] = {}

        async def _run() -> None:
            result_box["id"] = await h.proxy.run_batched([("shout", task_ref)], inputs)

        h.tg.start_soon(_run)

        # Follow the run channel as soon as it's announced.
        await _wait_until(lambda: len(sched_sub.received) == 1)
        run_reader = sched_sub.received[0]["reader"]
        run_sub = await h.watch_run_channel(run_reader)

        # The cell announcement (a key) arrives before any of its partials.
        await _wait_until(lambda: len(_of_kind(run_sub.received, "cell_channel")) == 1)
        (cell_msg,) = _of_kind(run_sub.received, "cell_channel")
        assert cell_msg["input_name"] == "a"
        assert cell_msg["step_index"] == 0
        assert isinstance(cell_msg["key"], str)

        # From-start cell subscriber (opened by key); then a mid-task one
        # once some partials have flowed.
        early = await h.open_and_watch_cell(run_reader, cell_msg["key"])
        await _wait_until(lambda: len(_partial_texts(early.received)) >= 2)
        late = await h.open_and_watch_cell(run_reader, cell_msg["key"])
        await _wait_until(lambda: len(late.received) >= 1)
        assert late.received[0]["kind"] == "snapshot_open"
        assert len(late.received[0]["values"]) >= 2  # the replay

        await _wait_until(lambda: "id" in result_box)
        await _wait_until(lambda: _result(early.received) is not None)
        await _wait_until(lambda: _result(late.received) is not None)

        # Replay + deltas compose to the same stream.
        assert _partial_texts(late.received) == _partial_texts(early.received)
        full = "ALPHA BETA GAMMA DELTA EPSILON ZETA ETA THETA"
        assert _partial_texts(early.received)[-1] == full
        # The cell channel's final value is the task's output id, and the
        # run channel's cell_result agrees.
        cell_out = _result(early.received)
        await _wait_until(lambda: len(_of_kind(run_sub.received, "cell_result")) == 1)
        (cell_result,) = _of_kind(run_sub.received, "cell_result")
        assert cell_result["ok"] is True
        assert cell_result["output"] == cell_out

        # The run finishes with the durable result entity, which strongly
        # links the output — loading the result makes the output id loadable
        # from `work`.
        await _wait_until(lambda: _result(run_sub.received) is not None)
        assert _result(run_sub.received) == result_box["id"]
        result = await EntityRef(entity_id=result_box["id"], target_type=SimpleRunResult).load(
            h.work
        )
        assert result.cells["a"]["0"] == cell_out
        loaded = await EntityRef(entity_id=cell_out, target_type=NoteWithLinks).load(h.work)
        assert loaded.body == full


async def _run_cached_flag(
    h: _Harness, task_ref: ServiceRef[Any], inputs: dict[str, FxtrID]
) -> bool:
    """Run once and return whether the (single) cell was served from cache,
    read off the durable ``SimpleRunResult.cached`` record (post-finish-safe,
    unlike the run channel's message log)."""
    rid = await h.proxy.run_batched([("shout", task_ref)], inputs)
    result = await EntityRef(entity_id=rid, target_type=SimpleRunResult).load(h.work)
    return result.cached["a"]["0"]


async def test_cache_hit_skips_subchannel() -> None:
    """Plan item 3: a second identical run serves the cell from cache (no
    task invocation, hence no subchannel) — recorded as ``cached: true`` in
    the result; ``clear_cache`` forces a re-miss."""
    async with _scheduler_harness() as h:
        task_ref = await h.mount(_WordShoutImpl())
        inputs = {"a": h.store_input("a", "one two")}

        assert await _run_cached_flag(h, task_ref, inputs) is False  # first run computes
        assert await _run_cached_flag(h, task_ref, inputs) is True  # second hits the cache

        assert await h.proxy.clear_cache() == 1
        assert await _run_cached_flag(h, task_ref, inputs) is False  # re-miss after clear


async def test_failing_task_aborts_cell_and_run_finishes() -> None:
    """Plan item 4: a failing task's cell channel aborts (the scheduler
    backstop), ``cell_result`` records the error, the run still finishes with
    the failure in the result entity, and — pinning "cells are reachable
    only while the run is open" — opening the failed cell by key *after* the
    run finished raises (the run-finish teardown reclaimed its subchannels)."""
    async with _scheduler_harness() as h:
        sched_sub = await h.watch_scheduler()
        ok_ref = await h.mount(_WordShoutImpl())
        fail_ref = await h.mount(_FailingStreamingTask())
        inputs = {"a": h.store_input("a", "just two")}

        run_id_box: dict[str, FxtrID] = {}

        async def _run() -> None:
            run_id_box["id"] = await h.proxy.run_batched(
                [("shout", ok_ref), ("boom", fail_ref)], inputs
            )

        h.tg.start_soon(_run)
        await _wait_until(lambda: len(sched_sub.received) == 1)
        run_reader = sched_sub.received[0]["reader"]
        run_sub = await h.watch_run_channel(run_reader)
        await _wait_until(lambda: "id" in run_id_box)
        await _wait_until(lambda: _result(run_sub.received) is not None)

        cell_results = {m["step_index"]: m for m in _of_kind(run_sub.received, "cell_result")}
        assert cell_results[0]["ok"] is True
        assert cell_results[1]["ok"] is False
        assert "streaming boom" in cell_results[1]["error"]
        assert cell_results[1]["output"] is None

        result = await EntityRef(entity_id=run_id_box["id"], target_type=SimpleRunResult).load(
            h.work
        )
        assert result.failures["a"][0] == 1
        assert "streaming boom" in result.failures["a"][1]
        assert set(result.cells["a"]) == {"0"}

        # The run has finished: its subchannels are reclaimed, so opening the
        # failing cell by key now raises (cells are reachable only while the
        # run is open).
        failed_cell = next(
            m for m in _of_kind(run_sub.received, "cell_channel") if m["step_index"] == 1
        )
        run_proxy = await run_reader.replace_service_type(_RUN_READER_TYPE).resolve(h.work)
        with pytest.raises(Exception):
            await run_proxy.open_subchannel_reader(failed_cell["key"])


async def test_late_run_subscriber_sees_final_value_only() -> None:
    """After the run finishes, the run channel's message log (including the
    ``cell_channel`` announcements) is gone — a late subscriber gets only the
    ``SimpleRunResult`` ref."""
    async with _scheduler_harness() as h:
        task_ref = await h.mount(_WordShoutImpl())
        result_id = await h.proxy.run_batched(
            [("shout", task_ref)], {"x": h.store_input("x", "one two")}
        )

        sub = await h.watch_scheduler()
        await _wait_until(lambda: len(sub.received) == 1)
        run_sub = await h.watch_run_channel(sub.received[0]["reader"])
        await _wait_until(lambda: len(run_sub.received) == 1)
        (snap,) = run_sub.received
        assert snap["kind"] == "snapshot_complete"
        assert snap["result"] == result_id

        await _settle()
        assert len(run_sub.received) == 1

        result = await EntityRef(entity_id=result_id, target_type=SimpleRunResult).load(h.work)
        assert result.failures == {}
        assert set(result.cells["x"]) == {"0"}
