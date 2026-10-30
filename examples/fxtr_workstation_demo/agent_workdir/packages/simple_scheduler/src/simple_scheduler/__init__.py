"""Concrete :class:`SimpleTaskSequenceSchedulerProto` implementation.

Spawned as ``fxtr.services.simple_scheduler`` (wasm, git-ref spawn);
connect config is ignored. One ``run_batched`` at a time (an
``anyio.Lock`` serializes callers); within a run, each input's steps
execute as a sequential chain and the chains run fully concurrently.

**Per-run progress flows through an output channel**
(:func:`fxtr2.common.channels.create_root_channel`): ``run_batched``
stores a :class:`simple_tasks.SchedulerRunTag`, creates a channel for it
on the scheduler's mount scope, ``start``s the writer, and emits the
``run_inputs`` + ``task_*`` messages through it (shapes in
:mod:`simple_tasks`). The scheduler **owns the writer**; consumers get
the **reader**. The channel system owns per-run retention (message refs
are copied into the channel's data scope by ``emit``), snapshot-vs-delta
consistency, and the post-run story (``finish`` publishes the durable
:class:`SimpleRunResult` ref and drops the log — late subscribers get
only the final value). The writer is bridged into the run dispatch's
scope so emits copy the freshly produced output entities from there.

The scheduler's own hub (queue-mode :class:`SubscriberHub`, hosted on
the lifespan task group) carries only **announcements**:
``{"kind": "run_channel", "run_id", "reader": <reader ref>}`` per run,
seeded to new subscribers while that run is the current/most-recent one,
plus ``scheduler_closing`` on teardown. The announcement's origin scope
is the mount scope (where the channel services live); dispatches are
authorized for it even while it drains, which is what lets a
guest-mounted scheduler — whose mount scope starts draining the moment
the ref is handed out — keep creating channels and seeding announcements.

Channel writers + readers mount on the scheduler's mount scope and stay
mounted across runs: complete channels are cheap (log dropped, only the
final result ref retained in the channel's durable scope). Holding a
*reader* ref keeps the mount alive, same as holding the scheduler ref.
When the last holder drops, the mount drains, the lifespan teardown
publishes ``scheduler_closing`` and flushes via ``hub.aclose()``; any
still-open channel aborts via the writer's mount-lifespan teardown (the
release fallback — the normal path is ``finish``).

Event shapes are documented on :mod:`simple_tasks`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import traceback
import uuid
from collections.abc import AsyncIterator
from typing import Any

import anyio
import anyio.abc
from fxtr2.common.channels import create_root_channel
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from simple_tasks import (
    SIMPLE_SCHEDULER_TYPE_ID,
    RunStatus,
    SchedulerRunTag,
    SimpleRunResult,
    SimpleTaskProto,
    SimpleTaskSequenceSchedulerProto,
)


@dataclasses.dataclass
class _RunBookkeeping:
    """The current/most-recent run: its channel writer + reader refs plus
    the encoded ``run_channel`` announcement that seeds new scheduler
    subscriptions. The channel state itself lives in the channel tree
    (mounted on the lifespan scope); these are just the service refs."""

    run_id: str
    writer: ServiceRef[Any]
    reader: ServiceRef[Any]
    announcement: FxtrServiceData


@dataclasses.dataclass
class _SchedulerState:
    """Per-mount lifespan state (the scheduler stashes nothing on
    ``self`` — see ``_fxtr_mount_lifespan``)."""

    hub: SubscriberHub[FxtrServiceData]
    scope: FxtrScope
    run_lock: anyio.Lock = dataclasses.field(default_factory=anyio.Lock)
    run: _RunBookkeeping | None = None
    # Last few publish failures, for debug_state.
    errors: list[str] = dataclasses.field(default_factory=list)

    def record_error(self, where: str) -> None:
        self.errors.append(f"{where}: {traceback.format_exc(limit=8)}")
        del self.errors[:-5]


class _SchedulerImpl(TypedServiceImpl[SimpleTaskSequenceSchedulerProto]):
    def _target_type_annotation(self) -> Any:
        return SimpleTaskSequenceSchedulerProto

    def _service_type_id(self) -> FxtrID:
        return SIMPLE_SCHEDULER_TYPE_ID

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[_SchedulerState]:
        # ``scope`` is the mount scope — kept in state: run channels
        # mount on it (their lifespans attach to it, so they abort on
        # its teardown if still open), and announcements publish/seed
        # with it as the origin.
        async with anyio.create_task_group() as tg:
            state = _SchedulerState(
                hub=SubscriberHub[FxtrServiceData](task_group=tg, mode="queue"), scope=scope
            )
            try:
                yield state
            finally:
                # Runs unauthorized, after the mount token drained (the
                # ref's holders are gone; subscriptions don't pin us).
                # Polite shutdown: tell subscribers, then flush. The run
                # channels' own lifespan teardowns (concurrent with this
                # one) abort any still-open channel.
                state.hub.publish({"kind": "scheduler_closing"}, origin_scope=None)
                await state.hub.aclose()

    # ----- events -----

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to run announcements; seeded with the
        current/most-recent run's ``run_channel`` event (contract on
        the proto / in :mod:`simple_tasks`)."""
        state = self._fxtr_get_lifespan_state()
        assert isinstance(state, _SchedulerState)
        run = state.run
        if run is None:
            return await state.hub.subscribe(subscriber, get_active_scope())
        # seed_origin is the mount scope: the announced storage ref is
        # mounted there. This dispatch is authorized for it (override),
        # even when the mount is draining.
        return await state.hub.subscribe(
            subscriber,
            get_active_scope(),
            seed=[run.announcement],
            seed_origin=state.scope,
        )

    @service_method
    async def debug_state(self) -> FxtrServiceData:
        """Demo introspection: hub-backed counts + recent errors."""
        state = self._fxtr_get_lifespan_state()
        assert isinstance(state, _SchedulerState)
        return {
            "live_subscriptions": state.hub.live_subscription_count,
            "has_run": state.run is not None,
            "run_in_flight": state.run_lock.locked(),
            "errors": list(state.errors),
        }

    # ----- the run -----

    @service_method
    async def run_batched(
        self,
        steps: list[ServiceRef[SimpleTaskProto]],
        inputs: dict[str, FxtrID],
        labels: list[str] | None = None,
    ) -> FxtrID:
        step_labels = (
            tuple(labels)
            if labels is not None
            else tuple(f"step {i + 1}" for i in range(len(steps)))
        )
        if len(step_labels) != len(steps):
            raise ValueError(f"labels has {len(step_labels)} entries for {len(steps)} steps")
        state = self._fxtr_get_lifespan_state()
        assert isinstance(state, _SchedulerState)

        async with state.run_lock:
            run_id = uuid.uuid4().hex[:12]
            input_names = tuple(sorted(inputs))
            call_scope = get_active_scope()

            # The tag carries the run's static metadata. Stored on the
            # call scope: ``start`` copies its closure into the channel's
            # durable scope, so it outlives this dispatch.
            tag_ref = SchedulerRunTag(
                run_id=run_id, step_labels=step_labels, input_names=input_names
            ).store(call_scope)
            # Create the channel on the lifespan scope (durable across runs).
            # S/V/R are inferred from the tag type (ChannelTag base). The
            # scheduler owns the writer; consumers get the reader.
            writer_ref, reader_ref = await create_root_channel(
                tag_type=SchedulerRunTag, scope=state.scope
            )
            # Drive the writer from this run dispatch's scope: bridge it in
            # (the bridge dies with the dispatch) so emits copy the message
            # refs (outputs produced here) from `call_scope`.
            await call_scope.bridge_service_from_scope(
                writer_ref, state.scope, keepalive_via="independent_lease"
            )
            writer = await writer_ref.resolve(call_scope)
            await writer.start(
                tag_ref, RunStatus(phase="running", detail=f"{len(input_names)} inputs")
            )

            announcement: FxtrServiceData = {
                "kind": "run_channel",
                "run_id": run_id,
                "reader": reader_ref,
            }
            # Install as current run, then announce. (Sync between the
            # two: a scheduler subscriber can never observe a gap where
            # neither the seed nor the live event covers this run.)
            state.run = _RunBookkeeping(
                run_id=run_id, writer=writer_ref, reader=reader_ref, announcement=announcement
            )
            try:
                state.hub.publish(announcement, origin_scope=state.scope)
            except Exception:
                state.record_error("publish run_channel")
                raise

            try:
                # First message: the per-input entity ids (data-only — the
                # step service refs can't ride a channel message; the step
                # display labels live on the tag).
                await writer.emit(events=[{"kind": "run_inputs", "inputs": dict(inputs)}])

                # Resolve each task once for the run, in this dispatch
                # scope (proxies die with the call).
                proxies = [await step.resolve() for step in steps]

                cells: dict[str, dict[str, FxtrID]] = {name: {} for name in input_names}
                failures: dict[str, tuple[int, str]] = {}

                async def _chain(name: str, first: FxtrID) -> None:
                    current = first
                    for i, proxy in enumerate(proxies):
                        await writer.emit(
                            events=[
                                {
                                    "kind": "task_started",
                                    "run_id": run_id,
                                    "input_name": name,
                                    "step_index": i,
                                }
                            ]
                        )
                        try:
                            out = await proxy.run(current)
                        except Exception as exc:
                            message = "".join(
                                traceback.format_exception_only(type(exc), exc)
                            ).strip()
                            failures[name] = (i, message)
                            await writer.emit(
                                events=[
                                    {
                                        "kind": "task_failed",
                                        "run_id": run_id,
                                        "input_name": name,
                                        "step_index": i,
                                        "error": message,
                                    }
                                ]
                            )
                            return
                        cells[name][str(i)] = out
                        # The output entity lives in this dispatch's scope;
                        # `emit` copies its closure into the channel's data
                        # scope (the writer was bridged in from `call_scope`).
                        await writer.emit(
                            events=[
                                {
                                    "kind": "task_result",
                                    "run_id": run_id,
                                    "input_name": name,
                                    "step_index": i,
                                    "output": out,
                                }
                            ]
                        )
                        current = out

                async with anyio.create_task_group() as tg:
                    for name in input_names:
                        tg.start_soon(_chain, name, inputs[name])

                result = SimpleRunResult(
                    run_id=run_id,
                    step_labels=step_labels,
                    input_names=input_names,
                    inputs=dict(inputs),
                    cells=cells,
                    failures=failures,
                    completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                ).store()
                # A failed cell is still a *finished* run — the result
                # entity records the per-cell failures. ``finish`` retains
                # the result's closure in the channel's durable scope,
                # publishes the complete event, and drops the message log.
                done_detail = (
                    "all inputs ok" if not failures else f"{len(failures)} input(s) failed"
                )
                await writer.finish(result, RunStatus(phase="done", detail=done_detail))
                return result.entity_id
            except Exception as exc:
                # Run-level failure (not a per-cell one): don't leave the
                # channel dangling open until scheduler shutdown — tell
                # subscribers. (Cancellation skips this; the writer-release
                # mount-lifespan teardown abort covers that path.)
                with contextlib.suppress(Exception):
                    await writer.abort(f"run failed: {exc!r}")
                raise


async def run(config: FxtrServiceData) -> ServiceRef[SimpleTaskSequenceSchedulerProto]:
    """``fxtr.service`` entry point. ``config`` is ignored (pass
    ``None``)."""
    del config
    return await get_active_scope().mount(_SchedulerImpl())
