"""The watch subscriptions (``notes/event_system.md``): snapshot as the
first diff, keyed diff delivery (unchanged state never re-sent; net-zero
changes suppressed), the ``recomputing``/settle choreography, grants in
the ``watch_cells`` meta, ``subject_gone`` tombstones + same-path
resumption, the cell watch's drawer bundle and event-native log stream
(backlog on subscribe, live appends, per-run reset), and the subscriber
lifecycle (unsubscribe, the crash backstop).

Manual runner assembly throughout (the ``test_user_surface`` release
idiom): the tests need the runner scope to resolve unsubscribe handles,
and a host scope for subscriber/connector mounts.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import anyio
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.service_dialect import decode_from_service_data
from fxtr2.services.subscription_types import Subscriber
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

import uuid as _uuid

from fxtr2.data.system_entities import define_type

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment.executor import Executor
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.bindings import BindingHost
from fxtr_experiment.user_api import UserCapabilityProto
from fxtr_experiment.user_service import UserCapabilityImpl, Workflow
from fxtr_experiment_task.view_runtime import step_fn
from fxtr_experiment.watch_api import (
    CellsWatchEvent,
    CellWatchEvent,
    StepRef,
    WorkflowWatchEvent,
)

from .test_introspection import _value
from .test_user_surface import FN_AXES, X_AXES, EchoTask, _bind, _paths


@step_fn(type_id=define_type(_uuid.UUID("b1c2d3e4-0000-4a5b-8c6d-0e1f2a3b4c5d"), "watch_step"))
async def _watch_step(ctx, *, tag: str):
    return f"stepped-{tag}"


class _CheckpointedTask:
    """A task that logs at the outer level then runs one checkpointed
    step into its internals — so a cell watch sees the task-level log
    (no step identity), the step lifecycle, and the streamed checkpoint
    entry."""

    async def __call__(self, ctx, /, *, inp):
        await ctx.log("task starting")
        value = await inp.read(x="a")
        ckpt = await ctx.own_internals().open_checkpoint("steps", axes=())
        await ckpt.run_step(_watch_step, args={"tag": "a"}, coords={})
        return value


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class WatchRecorder(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Records one watch family's typed events; wakes waiters per
    delivery. Decoding through the real union pins that every delivery
    is a legal member of the family's schema."""

    def __init__(self, event_type: Any, *, raise_on_event: bool = False) -> None:
        super().__init__()
        self.event_type = event_type
        self.events: list[Any] = []
        self._raise_on_event = raise_on_event
        self._arrived = anyio.Event()

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-watch-recorder"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        if self._raise_on_event:
            raise RuntimeError("subscriber boom")
        self.events.append(decode_from_service_data(payload, self.event_type))
        self._arrived.set()
        self._arrived = anyio.Event()

    def batches(self) -> list[Any]:
        return [e for e in self.events if getattr(e, "kind", None) == "batch"]

    def patches(self, kind: str, *, skip_batches: int = 0) -> list[Any]:
        """Every delivered patch of ``kind``, optionally skipping the
        first ``skip_batches`` batches (e.g. 1 = diffs only)."""
        return [p for b in self.batches()[skip_batches:] for p in b.patches if p.kind == kind]

    def clear(self) -> None:
        self.events.clear()

    async def wait_for(self, pred: Callable[[list[Any]], bool], timeout: float = 10.0) -> None:
        with anyio.fail_after(timeout):
            while True:
                arrived = self._arrived
                if pred(self.events):
                    return
                await arrived.wait()

    async def settle(self, seconds: float = 0.3) -> None:
        """Give in-flight windows time to flush (no-delivery asserts)."""
        await anyio.sleep(seconds)


@asynccontextmanager
async def _wired(
    tmp_path: Path,
) -> AsyncIterator[tuple[Workflow, FxtrScope, FxtrScope, WorkflowSession]]:
    """A running workflow runner: ``(wf, host scope, runner scope,
    session)``. Watches are subscribed per test."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with WorkflowSession.open(state, ents) as session:
            async with FxtrScope(
                name="wf3-runner", isolated=True, include=[session.entity_scope, GLOBAL_SCOPE]
            ) as runner_scope:
                bindings = BindingHost(runner_scope, session)
                executor = Executor(session, bindings, runner_scope)
                cap_ref = await runner_scope.mount(
                    UserCapabilityImpl(session, executor, bindings, runner_scope)
                )
                proxy = await cap_ref.replace_service_type(UserCapabilityProto).resolve(
                    runner_scope
                )
                wf = await Workflow.connect(proxy)
                yield wf, host, runner_scope, session


def _first_batch_after_recomputing(sub: WatchRecorder) -> Any:
    """The snapshot choreography for a slow-flagged family: the first
    delivery is ``recomputing``, the batch that settles it follows."""
    assert sub.events[0].kind == "recomputing", sub.events
    return sub.batches()[0]


# -- 1. the workflow watch: snapshot, diffs, suppression --------------------------


@pytest.mark.anyio
async def test_workflow_snapshot_then_diffs_only(tmp_path: Path) -> None:
    """The first batch is the snapshot (the diff from the empty mirror:
    every existing row + the scalar sub-topics, even empty ones); later
    batches carry ONLY what changed — an unchanged summary row is never
    re-sent. The slow-flagged pass announces itself (`recomputing`
    precedes the settle batch)."""
    async with _wired(tmp_path) as (wf, host, _runner, _session):
        d_path = enc.encode_key_path(["inputs"])
        fns_path = enc.encode_key_path(["fns"])
        await wf.open_handle(wf.root, "fns", FN_AXES)
        inputs = await wf.root.open_data("inputs", X_AXES)
        await inputs.write(_value("v1"), x="a")

        sub = WatchRecorder(WorkflowWatchEvent)
        await wf.watch_workflow(await host.mount(sub))
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
        snapshot = _first_batch_after_recomputing(sub)
        kinds = [p.kind for p in snapshot.patches]
        # Everything the mirror needs, in one batch: rows + every scalar
        # sub-topic's current value (empty lists included — the client
        # must learn "targets: none", not stay unknowing).
        assert {"summary_upsert", "lint", "targets", "sourcing", "bindings"} <= set(kinds)
        rows = {r.path for p in snapshot.patches if p.kind == "summary_upsert" for r in p.rows}
        assert {d_path, fns_path} <= rows

        # A new grid arrives as a diff naming ONLY the new row.
        await wf.root.open_data("extra", X_AXES)
        extra_path = enc.encode_key_path(["extra"])
        await sub.wait_for(
            lambda es: any(
                any(r.path == extra_path for r in p.rows)
                for p in sub.patches("summary_upsert", skip_batches=1)
            )
        )
        diff_rows = [r.path for p in sub.patches("summary_upsert", skip_batches=1) for r in p.rows]
        assert fns_path not in diff_rows, "an unchanged row was re-sent"


@pytest.mark.anyio
async def test_no_delivery_for_idempotent_or_net_zero_mutations(tmp_path: Path) -> None:
    """Diff suppression is stronger than the old hint silence: an
    idempotent write emits nothing (no hints at all), a refused mutation
    emits nothing (rollback drops its hints), AND a net-zero change —
    revoke + re-adopt sourcing within one window — recomputes but diffs
    empty, so nothing is delivered (intent is not slow-flagged: no
    settle marker either)."""
    async with _wired(tmp_path) as (wf, host, _runner, _session):
        sub_tree = await wf.root.open_subtree("box", ())
        await sub_tree.open_data("inner", X_AXES)
        d = await wf.root.open_data("d", X_AXES)
        v = _value("v")
        await d.write(v, x="a")

        sub = WatchRecorder(WorkflowWatchEvent)
        await wf.watch_workflow(await host.mount(sub))
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
        sub.clear()

        await d.write(v, x="a")  # idempotent: same value, same pin — silent
        with pytest.raises(Exception):
            await wf.delete_grid(wf.root, "box")  # refused: rollback, silent
        # Net-zero: two intent mutations cancel within one window.
        await wf.revoke_grid_sourcing(wf.root, "d", force=True)
        await wf.source_grid(wf.root, "d")
        await sub.settle()
        # The sourcing arc may straddle windows (two deliveries) or land
        # in one (zero) — but the FINAL state must equal the initial one,
        # so the last sourcing update, if any, must contain d's record.
        sourcing_updates = [e for b in sub.batches() for e in b.patches if e.kind == "sourcing"]
        if sourcing_updates:
            d_path = enc.encode_key_path(["d"])
            assert any(r.grid_path == d_path for r in sourcing_updates[-1].records)
        # The idempotent write and the refusal contributed nothing:
        # no cells-driven summary diffs for d's row beyond sourcing echo.
        assert not any(
            r.path == enc.encode_key_path(["box", "inner"])
            for p in sub.patches("summary_upsert")
            for r in p.rows
        )


# -- 2. the cells watch: meta grants, rows, tombstones ------------------------------


@pytest.mark.anyio
async def test_cells_watch_meta_grants_rows_and_removal(tmp_path: Path) -> None:
    """The grid watch serves its meta (config id + the minted grants —
    write mode on an ordinary grid, parent token present) and its keyed
    rows; a write upserts exactly the written cell, a delete removes it.
    Mutations built from the served grants work (the grants-ride-the-
    payload contract: the client needs no attach)."""
    async with _wired(tmp_path) as (wf, host, _runner, _session):
        d = await wf.root.open_data("d", X_AXES)
        await d.write(_value("v1"), x="a")
        d_path = enc.encode_key_path(["d"])

        sub = WatchRecorder(CellsWatchEvent)
        await wf.watch_cells(await host.mount(sub), path=d_path)
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
        snapshot = sub.batches()[0]
        meta = next(p for p in snapshot.patches if p.kind == "meta")
        assert meta.path == d_path and meta.config_id is not None
        assert meta.grant.mode == "write"
        assert [a.name for a in meta.grant.axes] == ["x"]
        assert meta.parent_token is not None
        (snap_rows,) = [p.cells for p in snapshot.patches if p.kind == "cells_upsert"]
        assert {c.coords for c in snap_rows} == {("a",)}

        await d.write(_value("v2"), x="b")
        await sub.wait_for(
            lambda es: any(
                any(c.coords == ("b",) for c in p.cells)
                for p in sub.patches("cells_upsert", skip_batches=1)
            )
        )
        # Unchanged row "a" was not re-sent with "b"'s upsert.
        diff_coords = [
            c.coords for p in sub.patches("cells_upsert", skip_batches=1) for c in p.cells
        ]
        assert ("a",) not in diff_coords

        # A mutation built from the SERVED grant (no attach anywhere):
        # delete cell "b" through the watch's own token.
        from frozendict import frozendict

        from fxtr_experiment_task.errors import typed_wire_call
        from fxtr_experiment_task.runner_api import ViewSel

        sel = ViewSel(token=meta.grant.token, bound=frozendict({0: "b"}))
        assert await typed_wire_call(wf.capability.delete_cell(sel)) is True
        await sub.wait_for(lambda es: any(("b",) in p.coords for p in sub.patches("cells_remove")))


@pytest.mark.anyio
async def test_cells_watch_tombstone_and_same_path_resumption(tmp_path: Path) -> None:
    """Path addressing: deleting the watched grid delivers
    ``subject_gone`` (the watch stays live); re-creating the path
    resumes the SAME watch with a fresh snapshot batch (fresh grants —
    the mirror restarted from empty). A watch on a never-existing path
    starts with the tombstone."""
    async with _wired(tmp_path) as (wf, host, _runner, _session):
        ghost = WatchRecorder(CellsWatchEvent)
        await wf.watch_cells(await host.mount(ghost), path=enc.encode_key_path(["ghost"]))
        await ghost.wait_for(lambda es: any(e.kind == "subject_gone" for e in es))

        d = await wf.root.open_data("doomed", X_AXES)
        await d.write(_value("v"), x="a")
        d_path = enc.encode_key_path(["doomed"])
        sub = WatchRecorder(CellsWatchEvent)
        await wf.watch_cells(await host.mount(sub), path=d_path)
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
        first_meta = next(p for p in sub.batches()[0].patches if p.kind == "meta")

        await wf.delete_grid(wf.root, "doomed")
        await sub.wait_for(lambda es: any(e.kind == "subject_gone" for e in es))

        # While the path stays gone, hint churn that dirties the watch
        # (a `grids` hint) delivers NOTHING on it — in particular no
        # `recomputing` marker: a known-gone pass is a cheap probe with
        # no settling event, and the tombstone already said everything.
        n_at_gone = len(sub.events)
        await wf.root.open_data("other", X_AXES)
        await sub.settle()
        assert len(sub.events) == n_at_gone, sub.events[n_at_gone:]

        await wf.root.open_data("doomed", X_AXES)
        await sub.wait_for(
            lambda es: any(
                p.kind == "meta"
                for e in es[next(i for i, x in enumerate(es) if x.kind == "subject_gone") + 1 :]
                if getattr(e, "kind", None) == "batch"
                for p in e.patches
            )
        )
        gone_at = next(i for i, e in enumerate(sub.events) if e.kind == "subject_gone")
        resumed = [
            p
            for e in sub.events[gone_at + 1 :]
            if e.kind == "batch"
            for p in e.patches
            if p.kind == "meta"
        ]
        # A fresh mirror: fresh meta (and a fresh grant token — the old
        # vocabulary died with the old grid).
        assert resumed and resumed[0].grant.token != first_meta.grant.token


# -- 3. the cell watch: the drawer bundle + the log stream --------------------------


@pytest.mark.anyio
async def test_cell_watch_run_arc_reports_and_logs(tmp_path: Path) -> None:
    """One task-cell arc through a live cell watch: the pending
    snapshot, the run's report/generation updates arriving without any
    refetch, engine lifecycle log entries streaming DURING the run, and
    a rerun restarting the log stream (reset with the new generation).
    Then a late subscriber gets the finished run's buffered entries
    right after its first batch (the backlog contract)."""
    async with _wired(tmp_path) as (wf, host, _runner, _session):
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        inputs = await wf.root.open_data("inputs", X_AXES)
        await inputs.write(_value("v1"), x="a")
        await _bind(wf, host, fns, "echo", EchoTask())
        t = await wf.root.open_task(
            "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
        )
        await wf.add_target(t)
        t_path = enc.encode_key_path(["t"])

        sub = WatchRecorder(CellWatchEvent)
        await wf.watch_cell(await host.mount(sub), path=t_path, coords="[]")
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
        # Pre-run: the report exists (never-fault-on-empty), no status.
        first_reports = [p for p in sub.batches()[0].patches if p.kind == "report"]
        assert first_reports and first_reports[0].report.status is None

        await t.output_entity().read()  # the demand runs the task
        await sub.wait_for(
            lambda es: any(p.report.status == "complete" for p in sub.patches("report"))
        )
        # The generation report arrived too, and the log stream carries
        # the run's lifecycle trail (reset first, then appends with the
        # prologue's fn read + the input read).
        assert any(
            p.report.status == "complete" and p.report.generation is not None
            for p in sub.patches("generation")
        )
        resets = [e for e in sub.events if e.kind == "log_reset"]
        appends = [e for e in sub.events if e.kind == "log_append"]
        assert resets and appends
        entry_kinds = [x.entry_kind for e in appends for x in e.entries]
        assert "read_cell" in entry_kinds
        first_reset_at = next(i for i, e in enumerate(sub.events) if e.kind == "log_reset")
        first_append_at = next(i for i, e in enumerate(sub.events) if e.kind == "log_append")
        assert first_reset_at < first_append_at

        # A rerun restarts the stream: clear + re-demand → a NEW reset
        # with a different generation id.
        gen1 = resets[0].generation if resets[0].generation else None
        await wf.clear(t)
        await t.output_entity().read()
        await sub.wait_for(
            lambda es: any(e.kind == "log_reset" and e.generation != gen1 for e in es)
        )

        # The backlog contract: a LATE subscriber (the run is long over)
        # still gets the buffered entries, right after its first batch.
        late = WatchRecorder(CellWatchEvent)
        await wf.watch_cell(await host.mount(late), path=t_path, coords="[]")
        await late.wait_for(lambda es: any(e.kind == "log_append" for e in es))
        batch_at = next(i for i, e in enumerate(late.events) if e.kind == "batch")
        reset_at = next(i for i, e in enumerate(late.events) if e.kind == "log_reset")
        assert batch_at < reset_at
        late_kinds = [
            x.entry_kind for e in late.events if e.kind == "log_append" for x in e.entries
        ]
        assert "read_cell" in late_kinds


@pytest.mark.anyio
async def test_cell_watch_streams_step_lifecycle_and_checkpoints(tmp_path: Path) -> None:
    """A checkpointed task through a live cell watch: the step's
    lifecycle notes carry a structured `StepRef` (no stringified
    identity), and the checkpoint entry streams into the generation
    report AS the step completes — the owning-cell hint, not a
    run-end-only refresh."""
    async with _wired(tmp_path) as (wf, host, _runner, _session):
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        inputs = await wf.root.open_data("inputs", X_AXES)
        await inputs.write(_value("v1"), x="a")
        await _bind(wf, host, fns, "ckpt", _CheckpointedTask())
        t = await wf.root.open_task(
            "t", axes=(), task_fn=fns.at(fn="ckpt"), args={"inp": inputs.read_only()}
        )
        await wf.add_target(t)
        t_path = enc.encode_key_path(["t"])

        sub = WatchRecorder(CellWatchEvent)
        await wf.watch_cell(await host.mount(sub), path=t_path, coords="[]")
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))

        await t.output_entity().read()  # the demand runs the task + its step
        # The step's lifecycle notes carry a structured StepRef pointing
        # at the checkpoint cell (the same (grid_path, coords) identity
        # the checkpoint entries use — a client keys them by tuple).
        await sub.wait_for(
            lambda es: any(
                x.entry_kind == "step_started"
                for e in es
                if e.kind == "log_append"
                for x in e.entries
            )
        )
        started = [
            x
            for e in sub.events
            if e.kind == "log_append"
            for x in e.entries
            if x.entry_kind == "step_started"
        ]
        assert isinstance(started[0].step, StepRef)
        assert started[0].step.grid_path == enc.encode_key_path(["t", "steps"])
        # The checkpoint entry streamed into the generation report (its
        # write hinted the owning task cell): a generation patch carries
        # a checkpoint whose location matches the StepRef.
        await sub.wait_for(lambda es: any(p.report.checkpoints for p in sub.patches("generation")))
        ckpts = [c for p in sub.patches("generation") for c in p.report.checkpoints]
        assert any(
            c.grid_path == started[0].step.grid_path and c.coords == started[0].step.coords
            for c in ckpts
        )
        # The task's outer-level `ctx.log()` streamed too, as a `log`
        # entry with NO step identity (distinct from the step-tagged
        # notes) — the task-level logging pulled forward from chunk 7.
        task_logs = [
            x
            for e in sub.events
            if e.kind == "log_append"
            for x in e.entries
            if x.entry_kind == "log" and x.step is None
        ]
        assert any(x.message == "task starting" for x in task_logs)


# -- 4. subscriber lifecycle ---------------------------------------------------------


@pytest.mark.anyio
async def test_unsubscribe_stops_delivery_and_crashers_are_reaped(tmp_path: Path) -> None:
    """A raising subscriber is dropped (the crash backstop) without
    disturbing its sibling or the mutation path; an explicit unsubscribe
    stops delivery for good."""
    async with _wired(tmp_path) as (wf, host, runner_scope, _session):
        sub = WatchRecorder(WorkflowWatchEvent)
        handle_ref = await wf.watch_workflow(await host.mount(sub))
        crasher = WatchRecorder(WorkflowWatchEvent, raise_on_event=True)
        await wf.watch_workflow(await host.mount(crasher))

        await wf.root.open_data("d", X_AXES)
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
        assert crasher.events == []  # its first delivery raised: reaped

        handle = await handle_ref.resolve(runner_scope)
        await handle.unsubscribe()
        sub.clear()
        await wf.root.open_data("d2", X_AXES)
        await sub.settle()
        assert sub.events == []
