"""Concrete :class:`StreamingSchedulerProto` implementation (nested channels).

Spawned as ``fxtr.services.streaming_scheduler`` (wasm, git-ref
spawn). Structure mirrors ``simple_scheduler`` — a run channel per run
created via ``create_root_channel`` on the mount scope, ``run_channel``
announcements (carrying the run **reader**) on the scheduler's own
queue-mode hub — with the nested-channel twist: **every cell (input ×
step) is a subchannel of the run channel**. The scheduler
``spawn_subchannel``s the cell and hands the un-started **writer** to
the task (caller-passes-writer); the **task owns** the cell channel
(starts it with a tag, emits progress, finishes it). See
:mod:`streaming_tasks` for the message vocabularies.

**Output caching (minimal durable workflows).** Each step is named
(``run_batched`` takes ``(name, task_ref)`` pairs). The scheduler
keeps an in-memory cache from ``(step input id, step name)`` to the
entity that step produced; on a later run, a matching cell is served
from the cache **without running the task** and marked ``cached`` (a
``cell_result`` with ``cached: true`` and no ``cell_channel``). Cached
outputs are retained in a dedicated cache scope so their ids stay
loadable for the scheduler's life. Caching is on unless connected with
``{"cache": False}``; ``clear_cache()`` empties it (held entities free
at scheduler shutdown). The cache is scheduler-lifetime only — it does
not survive a respawn/restart.

**Cell channels are subchannels of the run channel** — tree
bookkeeping managed by the channel system (their data lives in
per-node retention scopes the tree owns), so there is no separate
cells scope and no sink bridging. A cell is reachable through the run
reader's ``open_subchannel_reader(key)`` only while the run is open;
once the run finishes, its cells are reclaimed and the run's final
value (the durable ``SimpleRunResult``) is the complete record.

Per cell (cache miss): ``spawn_subchannel`` off the run writer (minting
a key), emit ``cell_channel`` (input/step + key) into the run channel,
hand the cell writer to ``await task.run(input, cell_writer)`` — the
task starts/emits/finishes it. On return: an idempotent backstop
``cell_writer.abort(...)`` (no-op if the task finished it), retain the
output into the cache scope + record it, and emit ``cell_result``; on
task exception: backstop-abort the cell with the error and emit a
failed ``cell_result`` (the run keeps going — a failed cell stops only
that input's chain, as in the simple scheduler). The ``cache_scope``
(cached-output retention) is the only scheduler-held scope besides the
mount.
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
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from simple_tasks import SimpleRunResult
from streaming_tasks import (
    STREAMING_SCHEDULER_TYPE_ID,
    StreamingRunTag,
    StreamingSchedulerProto,
    StreamingTaskProto,
)


@dataclasses.dataclass
class _RunBookkeeping:
    """The current/most-recent run: its channel plus the encoded
    ``run_channel`` announcement that seeds new scheduler
    subscriptions."""

    run_id: str
    writer: ServiceRef[Any]
    reader: ServiceRef[Any]
    announcement: FxtrServiceData


@dataclasses.dataclass
class _SchedulerState:
    """Per-mount lifespan state."""

    hub: SubscriberHub[FxtrServiceData]
    scope: FxtrScope
    cache_scope: FxtrScope
    cache_enabled: bool
    #: (step input id string, step name) -> output entity id.
    cache: dict[tuple[str, str], FxtrID] = dataclasses.field(default_factory=dict)
    run_lock: anyio.Lock = dataclasses.field(default_factory=anyio.Lock)
    run: _RunBookkeeping | None = None
    errors: list[str] = dataclasses.field(default_factory=list)

    def record_error(self, where: str) -> None:
        self.errors.append(f"{where}: {traceback.format_exc(limit=8)}")
        del self.errors[:-5]


async def _held_scope(
    release: anyio.Event,
    name: str,
    *,
    task_status: anyio.abc.TaskStatus[FxtrScope] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Hold an isolated scope open until ``release`` fires (the
    lifespan teardown). Used for the cache scope (retained cached
    outputs): it pins nothing of the mount's and drains gracefully under
    any leases still bridged into it after the release."""
    async with FxtrScope(name=name, include=[GLOBAL_SCOPE], isolated=True) as scope:
        task_status.started(scope)
        await release.wait()


class _SchedulerImpl(TypedServiceImpl[StreamingSchedulerProto]):
    def __init__(self, cache_enabled: bool) -> None:
        self._cache_enabled = cache_enabled

    def _target_type_annotation(self) -> Any:
        return StreamingSchedulerProto

    def _service_type_id(self) -> FxtrID:
        return STREAMING_SCHEDULER_TYPE_ID

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[_SchedulerState]:
        async with anyio.create_task_group() as tg:
            cache_release = anyio.Event()
            cache_scope: FxtrScope = await tg.start(
                _held_scope, cache_release, "streaming_scheduler_cache"
            )
            state = _SchedulerState(
                hub=SubscriberHub[FxtrServiceData](task_group=tg, mode="queue"),
                scope=scope,
                cache_scope=cache_scope,
                cache_enabled=self._cache_enabled,
            )
            try:
                yield state
            finally:
                try:
                    state.hub.publish({"kind": "scheduler_closing"}, origin_scope=None)
                    await state.hub.aclose()
                finally:
                    cache_release.set()

    # ----- events -----

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to run announcements; seeded with the
        current/most-recent run's ``run_channel`` event."""
        state = self._fxtr_get_lifespan_state()
        assert isinstance(state, _SchedulerState)
        run = state.run
        if run is None:
            return await state.hub.subscribe(subscriber, get_active_scope())
        return await state.hub.subscribe(
            subscriber,
            get_active_scope(),
            seed=[run.announcement],
            seed_origin=state.scope,
        )

    @service_method
    async def clear_cache(self) -> int:
        """Drop every cached cell output; returns the number of
        entries cleared. The next run recomputes everything. (The
        underlying entities stay retained in the cache scope until the
        scheduler shuts down — minimal version.)"""
        state = self._fxtr_get_lifespan_state()
        assert isinstance(state, _SchedulerState)
        n = len(state.cache)
        state.cache.clear()
        return n

    @service_method
    async def debug_state(self) -> FxtrServiceData:
        """Demo introspection: hub-backed counts + recent errors."""
        state = self._fxtr_get_lifespan_state()
        assert isinstance(state, _SchedulerState)
        return {
            "live_subscriptions": state.hub.live_subscription_count,
            "has_run": state.run is not None,
            "run_in_flight": state.run_lock.locked(),
            "cache_enabled": state.cache_enabled,
            "cache_entries": len(state.cache),
            "errors": list(state.errors),
        }

    # ----- the run -----

    @service_method
    async def run_batched(
        self,
        steps: list[tuple[str, ServiceRef[StreamingTaskProto]]],
        inputs: dict[str, FxtrID],
    ) -> FxtrID:
        # Each step is (name, task_ref); the name is the cache identity
        # + column label. Index rather than unpack so wire-decoded
        # 2-element lists work as well as tuples.
        step_names = tuple(str(s[0]) for s in steps)
        step_refs = [s[1] for s in steps]
        state = self._fxtr_get_lifespan_state()
        assert isinstance(state, _SchedulerState)

        async with state.run_lock:
            run_id = uuid.uuid4().hex[:12]
            input_names = tuple(sorted(inputs))
            call_scope = get_active_scope()

            # Tag stored on the call scope (start copies it into the
            # channel's durable scope, so it outlives this dispatch).
            tag_ref = StreamingRunTag(
                run_id=run_id, step_labels=step_names, input_names=input_names
            ).store(call_scope)
            # Run channel on the lifespan scope; scheduler owns the writer.
            writer_ref, reader_ref = await create_root_channel(
                tag_type=StreamingRunTag, scope=state.scope
            )
            await call_scope.bridge_service_from_scope(
                writer_ref, state.scope, keepalive_via="independent_lease"
            )
            run_writer = await writer_ref.resolve(call_scope)
            await run_writer.start(tag_ref, None)

            announcement: FxtrServiceData = {
                "kind": "run_channel",
                "run_id": run_id,
                "reader": reader_ref,
            }
            state.run = _RunBookkeeping(
                run_id=run_id, writer=writer_ref, reader=reader_ref, announcement=announcement
            )
            try:
                state.hub.publish(announcement, origin_scope=state.scope)
            except Exception:
                state.record_error("publish run_channel")
                raise

            try:
                # Data-only first message: step names (cache/column labels)
                # + per-input ids. The step task refs can't ride a channel
                # message; we hold them here and resolve below.
                await run_writer.emit(
                    events=[
                        {
                            "kind": "run_inputs",
                            "step_names": list(step_names),
                            "inputs": dict(inputs),
                        }
                    ]
                )

                proxies = [await ref.resolve() for ref in step_refs]

                cells: dict[str, dict[str, FxtrID]] = {name: {} for name in input_names}
                failures: dict[str, tuple[int, str]] = {}
                cached: dict[str, dict[str, bool]] = {name: {} for name in input_names}

                async def _run_cell(name: str, i: int, current: FxtrID) -> tuple[FxtrID, bool]:
                    """One cell. On a cache hit: emit a ``cached``
                    ``cell_result`` (no subchannel, no task) and return the
                    stored output. On a miss: spawn the cell subchannel,
                    announce its key, hand the writer to the task (which
                    starts/emits/finishes it), backstop-abort, cache +
                    report. Returns ``(output id, was_cached)``; raises on
                    task failure (after aborting the cell + emitting the
                    failed ``cell_result``)."""
                    key = (current.id_string(), step_names[i])
                    if state.cache_enabled and key in state.cache:
                        out = state.cache[key]
                        # Bring the cached output into THIS dispatch's scope so
                        # both the cell_result emit and the final run-channel
                        # finish (whose SimpleRunResult closure references every
                        # cell output) can copy it from call_scope, like a
                        # freshly produced output.
                        await call_scope.copy_from_scope(out, state.cache_scope)
                        await run_writer.emit(
                            events=[
                                {
                                    "kind": "cell_result",
                                    "input_name": name,
                                    "step_index": i,
                                    "ok": True,
                                    "output": out,
                                    "error": None,
                                    "cached": True,
                                }
                            ]
                        )
                        return out, True

                    # Spawn the cell as a subchannel of the run channel; the
                    # returned writer is handed to the task, which owns the
                    # cell channel (tag + start/emit/finish).
                    cell_key, cell_writer_ref = await run_writer.spawn_subchannel(
                        name=f"{name}:{step_names[i]}"
                    )
                    await run_writer.emit(
                        events=[
                            {
                                "kind": "cell_channel",
                                "input_name": name,
                                "step_index": i,
                                "key": cell_key,
                            }
                        ]
                    )
                    cell_writer = await cell_writer_ref.resolve(call_scope)
                    try:
                        out = await proxies[i].run(current, cell_writer_ref)
                    except Exception as exc:
                        message = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                        # Backstop: the task may have started but not finished
                        # the cell channel — abort it with the error.
                        with contextlib.suppress(Exception):
                            await cell_writer.abort(message)
                        await run_writer.emit(
                            events=[
                                {
                                    "kind": "cell_result",
                                    "input_name": name,
                                    "step_index": i,
                                    "ok": False,
                                    "output": None,
                                    "error": message,
                                    "cached": False,
                                }
                            ]
                        )
                        raise
                    # Idempotent backstop: a no-op if the task finished it,
                    # otherwise closes a cell the task left dangling open.
                    with contextlib.suppress(Exception):
                        await cell_writer.abort("scheduler backstop")
                    # Retain the output into the cache scope before caching.
                    if state.cache_enabled:
                        try:
                            await state.cache_scope.copy_from_scope(out, call_scope)
                        except Exception:
                            state.record_error("cache retain")
                        else:
                            state.cache[key] = out
                    await run_writer.emit(
                        events=[
                            {
                                "kind": "cell_result",
                                "input_name": name,
                                "step_index": i,
                                "ok": True,
                                "output": out,
                                "error": None,
                                "cached": False,
                            }
                        ]
                    )
                    return out, False

                async def _chain(name: str, first: FxtrID) -> None:
                    current = first
                    for i in range(len(proxies)):
                        try:
                            current, was_cached = await _run_cell(name, i, current)
                        except Exception as exc:
                            message = "".join(
                                traceback.format_exception_only(type(exc), exc)
                            ).strip()
                            failures[name] = (i, message)
                            return
                        cells[name][str(i)] = current
                        cached[name][str(i)] = was_cached

                async with anyio.create_task_group() as tg:
                    for name in input_names:
                        tg.start_soon(_chain, name, inputs[name])

                result = SimpleRunResult(
                    run_id=run_id,
                    step_labels=step_names,
                    input_names=input_names,
                    inputs=dict(inputs),
                    cells=cells,
                    failures=failures,
                    cached=cached,
                    completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                ).store()
                await run_writer.finish(result)
                return result.entity_id
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await run_writer.abort(f"run failed: {exc!r}")
                raise


async def run(config: FxtrServiceData) -> ServiceRef[StreamingSchedulerProto]:
    """``fxtr.service`` entry point. ``config`` is an optional dict; an
    optional ``"cache"`` bool toggles output caching (default on,
    ``{"cache": False}`` disables)."""
    cache_enabled = True
    if isinstance(config, dict):
        cache_enabled = bool(config.get("cache", True))
    return await get_active_scope().mount(_SchedulerImpl(cache_enabled=cache_enabled))
