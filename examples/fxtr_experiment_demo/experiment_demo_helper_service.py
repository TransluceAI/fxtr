"""Host-side ``experiment-demo-helper`` service: the demo's scenario driver.

Implements :class:`experiment_demo_helper.ExperimentDemoHelperProto`
(see its module docstring for the scenario map). Every scenario drives
the *same user capability* the browser (and any RPC client) speaks —
the driver only adds the host-side pieces a browser can't supply:
constructing entities, mounting connector services, and kicking
long-running demands in the background.

**The driver is stable across runner crashes.** It holds a
:class:`experiment_runner.RunnerSupervisor` (not a runner) and looks
the live runner up per call; ``crash_runner`` / ``revive_runner``
expose the supervisor's in-process interruption arc as panel buttons.
While the runner is down, scenario methods fail with a clear message.
Two consequences of surviving the runner:

- Scenario returns carry entity ids that live in the (current) runner
  scope; the driver's own call scopes can't lift them, so every
  returned id is **exported** — loaded from the runner scope and
  re-stored into the active dispatch call scope (content-addressed, so
  the id is unchanged and now coverable).
- Anything cached per-runner (the extra fn's connector mount, the
  slow-demand flag) is invalidated by the supervisor's epoch counter.

Mount via :func:`mount_experiment_demo_helper` (async CM yielding
``(service_ref, scope)`` for a boot-time ``ExternalServiceConfig``).

Grid layout the scenarios build (all under the workflow root):

- ``fns`` — handle grid (axis ``fn``): one key per demo task fn.
- ``inputs`` — data grid (axis ``x``: a/b/c): the source ExpNotes.
- ``shout`` / ``combined`` — the pipeline task grids (axis ``x``):
  uppercase each input; combine each input with its uppercased form.
- ``slow`` — the checkpointed crash/resume task (no axes; input a).
- ``flaky`` — the planted-failure retry task (no axes; input b).
- ``reversed`` — the boundness-arc task (no axes; input c), wired to
  the deliberately unbound ``reverse`` fn.
- ``variants`` — subtree grid (axis ``v``: red/blue) holding ``cfg``
  (data: a per-variant note) + ``styled`` (task: combine input a with
  each variant's cfg) — the dashboard's subtree-shape state.
- ``planned`` — the run-time-creation task (no axes; input a): its run
  creates ``draft`` (data) + ``polish`` (a nested task) inside its own
  internals.
- ``echoed`` — the unsettled-chain task (axis ``x``): reads ONLY
  ``shout``'s output, so an input edit leaves it *unsettled* (its own
  observations still match; its support is stale).
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import anyio.abc
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment.bindings import wrap_singleton
from fxtr_experiment_task.task_adapter import TaskFnAdapter
from fxtr_experiment.user_service import Workflow
from fxtr_experiment_task.views import DataView, HandleView, TaskView

from demo_task_fns import EXTRA_FN_FACTORY, EXTRA_FN_NAME, FlakyNote, fn_key_note
from experiment_demo_helper import EXPERIMENT_DEMO_HELPER_TYPE_ID, ExperimentDemoHelperProto
from experiment_notes import ExpNote
from experiment_runner import ExperimentRunner, RunnerSupervisor

FN_AXES = (m.AxisSpec("fn", "str"),)
X_AXES = (m.AxisSpec("x", "str"),)

#: The pipeline's source notes (rewritten verbatim by ``revert_input``).
INPUT_NOTES = {
    "a": ExpNote(title="Meadow", body="a quiet meadow at dawn"),
    "b": ExpNote(title="Harbor", body="gulls over a grey harbor"),
    "c": ExpNote(title="Attic", body="dust motes in the attic light"),
}

#: ``overwrite_input`` swaps input *b* for this (staleness propagation).
EDITED_INPUT_B = ExpNote(title="Harbor", body="fog rolling into the harbor at dusk")

#: The variants subtree's per-variant config notes (``scenario_subtree``).
VARIANT_NOTES = {
    "red": ExpNote(title="Red", body="crimson banners in the square"),
    "blue": ExpNote(title="Blue", body="a cold blue morning"),
}


def _path(*keys: str) -> str:
    return enc.encode_key_path(list(keys))


@dataclasses.dataclass
class _Views:
    """One scenario call's views (re-opened per call; opens attach)."""

    fns: HandleView
    inputs: DataView
    shout: TaskView
    combined: TaskView


class RunnerDownError(RuntimeError):
    pass


class ExperimentDemoHelperImpl(TypedServiceImpl[ExperimentDemoHelperProto]):
    """See the module docstring + the proto's scenario map."""

    def __init__(self, supervisor: RunnerSupervisor) -> None:
        super().__init__()
        self._supervisor = supervisor
        self._slow_running = False
        # Live background demands' cancel scopes + drained signal:
        # crash_runner cancels these first, so running generations are
        # interrupted through the engine's tested cancellation path
        # (raise-time poisoning) rather than by chaotic lease breakage.
        self._demand_scopes: set[anyio.CancelScope] = set()
        self._demands_drained = anyio.Event()
        self._demands_drained.set()
        # Per-runner-epoch state (reset when the runner is revived):
        # the extra fn's connector mount lives on the runner's host
        # scope and its binding release in the runner scope, so both
        # die with the runner.
        self._extra_epoch = -1
        self._extra_connector: ServiceRef | None = None
        self._extra_release: Any | None = None

    def _target_type_annotation(self) -> Any:
        return ExperimentDemoHelperProto

    def _service_type_id(self) -> FxtrID:
        return EXPERIMENT_DEMO_HELPER_TYPE_ID

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncGenerator[anyio.abc.TaskGroup]:
        """A task group for background demands (the slow scenario)."""
        async with anyio.create_task_group() as tg:
            try:
                yield tg
            finally:
                tg.cancel_scope.cancel()

    # -- runner access ------------------------------------------------------------

    def _runner(self) -> ExperimentRunner:
        runner = self._supervisor.current
        if runner is None:
            raise RunnerDownError("the workflow runner is down (crashed) — press 'Revive runner'")
        if self._extra_epoch != self._supervisor.epoch:
            self._extra_epoch = self._supervisor.epoch
            self._extra_connector = None
            self._extra_release = None
        return runner

    def _wf(self) -> Workflow:
        return self._runner().workflow

    async def _export(self, value: Any) -> Any:
        """Re-cover returned ids in THIS call's scope (see module doc).

        Values returned by capability calls are ids loadable in the
        current runner scope (the workflow proxy's home scope); the
        driver's call scope doesn't descend from it, so re-store the
        (content-addressed) entity into the active dispatch scope —
        same id, now liftable for the return packet. Recurses into
        dicts. Non-id values pass through.
        """
        if isinstance(value, dict):
            return {k: await self._export(v) for k, v in value.items()}
        if isinstance(value, FxtrID):
            # Membership: served by the capability, so loadable in the
            # runner scope (ensure_loadable re-attaches served ids).
            note = await EntityRef(value, ExpNote).load(self._runner().scope)
            return note.store(get_active_scope()).entity_id
        return value

    # -- shared setup ----------------------------------------------------------

    async def _write_if_new(self, view: DataView, note: ExpNote, **coords: Any) -> FxtrID:
        """Write ``note`` unless the cell already holds a (possibly
        edited) value — first-write-wins so scenarios are re-pressable."""
        value = note.store(self._runner().scope).entity_id
        try:
            await view.write(value, **coords)
        except Exception:
            # Same-value writes are idempotent (no error); a different
            # existing value means the operator overwrote it — leave it.
            pass
        return value

    async def _ensure_pipeline(self) -> _Views:
        """Open (create-or-attach) the pipeline grids + seed inputs."""
        runner = self._runner()
        wf = runner.workflow
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        for name, key in runner.fn_keys.items():
            await wf.write_handle(fns, key, fn=name)
        inputs = await wf.root.open_data("inputs", X_AXES)
        for x, note in INPUT_NOTES.items():
            await self._write_if_new(inputs, note, x=x)
        shout = await wf.root.open_task(
            "shout",
            axes=X_AXES,
            task_fn=fns.at(fn="uppercase"),
            args={"note": inputs.read_only()},
        )
        combined = await wf.root.open_task(
            "combined",
            axes=X_AXES,
            task_fn=fns.at(fn="combine"),
            args={"original": inputs.read_only(), "shouted": shout.output_entity()},
        )
        return _Views(fns=fns, inputs=inputs, shout=shout, combined=combined)

    async def _cell_state(self, view: Any, **coords: Any) -> dict[str, Any]:
        report = await self._wf().inspect(view, **coords)
        state = {
            "status": report.status,
            "currency": report.currency,
            "value": await self._export(report.value),
        }
        # None entries are noise in the panel (e.g. task cells have no
        # single value at this surface) — drop them.
        return {k: v for k, v in state.items() if v is not None}

    # -- scenarios ---------------------------------------------------------------

    @service_method
    async def scenario_pipeline(self) -> FxtrServiceData:
        """Build + run the three-cell pipeline (targets on the sinks)."""
        v = await self._ensure_pipeline()
        results: dict[str, Any] = {}
        for x in INPUT_NOTES:
            await self._wf().add_target(v.combined, x=x)
            results[x] = await v.combined.output_entity().read(x=x)
        return {
            "grids": [_path("inputs"), _path("shout"), _path("combined")],
            "results": await self._export(results),
        }

    @service_method
    async def clear_and_rerun_pipeline(self) -> FxtrServiceData:
        """The v0 stale-rerun gesture: demands fault on stale by design,
        so clear the pipeline's non-fresh task cells, then re-demand the
        sinks (fresh generations over the edited inputs)."""
        v = await self._ensure_pipeline()
        cleared: list[str] = []
        for view, name in ((v.shout, "shout"), (v.combined, "combined")):
            for x in INPUT_NOTES:
                report = await self._wf().inspect(view, x=x)
                if report.status is not None and report.currency != "fresh":
                    await self._wf().clear(view, x=x)
                    cleared.append(f"{name} x={x}")
        results: dict[str, Any] = {}
        for x in INPUT_NOTES:
            await self._wf().add_target(v.combined, x=x)
            results[x] = await v.combined.output_entity().read(x=x)
        return {"cleared": cleared, "results": await self._export(results)}

    @service_method
    async def overwrite_input(self) -> FxtrServiceData:
        """Overwrite input *b*: the pipeline's b-cells go stale."""
        v = await self._ensure_pipeline()
        edited = EDITED_INPUT_B.store(self._runner().scope).entity_id
        await self._wf().overwrite(v.inputs, edited, x="b")
        return {
            "overwrote": _path("inputs") + " x=b",
            "shout_b": await self._cell_state(v.shout, x="b"),
            "combined_b": await self._cell_state(v.combined, x="b"),
        }

    @service_method
    async def revert_input(self) -> FxtrServiceData:
        """Rewrite the original input *b*: stale cells resurrect fresh."""
        v = await self._ensure_pipeline()
        original = INPUT_NOTES["b"].store(self._runner().scope).entity_id
        await self._wf().overwrite(v.inputs, original, x="b")
        return {
            "reverted": _path("inputs") + " x=b",
            "shout_b": await self._cell_state(v.shout, x="b"),
            "combined_b": await self._cell_state(v.combined, x="b"),
        }

    async def _open_slow(self) -> TaskView:
        v = await self._ensure_pipeline()
        return await self._wf().root.open_task(
            "slow",
            axes=(),
            task_fn=v.fns.at(fn="slow"),
            args={"note": v.inputs.read_only().at(x="a")},
        )

    async def _demand_slow_in_background(self, slow: TaskView) -> FxtrServiceData:
        if self._slow_running:
            return {"grid": _path("slow"), "note": "a demand is already in flight"}
        tg = self._fxtr_get_lifespan_state()
        self._slow_running = True
        self._demands_drained = anyio.Event()

        async def demand() -> None:
            with anyio.CancelScope() as scope:
                self._demand_scopes.add(scope)
                try:
                    await slow.output_entity().read()
                except Exception as e:  # crash-interrupted / faults: visible in state
                    print(f"[experiment-demo-helper] slow demand ended: {e!r}", flush=True)
                finally:
                    self._demand_scopes.discard(scope)
                    self._slow_running = False
                    if not self._demand_scopes:
                        self._demands_drained.set()

        tg.start_soon(demand)
        return {
            "grid": _path("slow"),
            "note": "demand running in the background — watch the cell's status",
        }

    @service_method
    async def scenario_slow(self) -> FxtrServiceData:
        """Start the checkpointed slow task in the background."""
        slow = await self._open_slow()
        await self._wf().add_target(slow)
        return await self._demand_slow_in_background(slow)

    @service_method
    async def rerun_slow(self) -> FxtrServiceData:
        """Re-demand the slow cell (after a crash + revive: resumes the
        interrupted generation, completed steps served from checkpoints)."""
        return await self._demand_slow_in_background(await self._open_slow())

    # -- the crash/revive arc -------------------------------------------------------

    @service_method
    async def crash_runner(self) -> FxtrServiceData:
        """Forcibly tear the runner down mid-whatever-it's-doing
        (in-process cancellation — the browser-drivable interruption
        arc; running generations poison to interrupted, durable
        checkpoints survive).

        Order: cancel our live background demands first (the engine's
        tested cancellation path — the run poisons to interrupted at
        raise time), then tear the runner down.
        """
        for scope in list(self._demand_scopes):
            scope.cancel()
        with anyio.move_on_after(10):
            await self._demands_drained.wait()
        crashed = await self._supervisor.crash()
        return {
            "crashed": crashed,
            "note": (
                "runner torn down — scenario buttons will fail until revived"
                if crashed
                else "runner was already down"
            ),
        }

    @service_method
    async def revive_runner(self) -> FxtrServiceData:
        """Bring a fresh runner up over the same durable state (fresh
        auto-bound fns, re-registered `experiment-workflow`)."""
        revived = await self._supervisor.revive()
        return {
            "revived": revived,
            "note": (
                "runner is up — interrupted cells resume on re-run"
                if revived
                else "runner was already up"
            ),
        }

    async def _open_flaky(self) -> TaskView:
        v = await self._ensure_pipeline()
        return await self._wf().root.open_task(
            "flaky",
            axes=(),
            task_fn=v.fns.at(fn="flaky"),
            args={"note": v.inputs.read_only().at(x="b")},
        )

    @service_method
    async def scenario_flaky(self) -> FxtrServiceData:
        """Demand the flaky task; the planted failure errors the run."""
        flaky = await self._open_flaky()
        await self._wf().add_target(flaky)
        # Re-arm + clear so the scenario is repeatable in one session
        # (the clear refuses while a demand is running — leave it be).
        flaky_fn = self._runner().fn_instances["flaky"]
        assert isinstance(flaky_fn, FlakyNote)
        flaky_fn.failures_remaining = 1
        with contextlib.suppress(Exception):
            await self._wf().clear(flaky)
        error: str | None = None
        try:
            await flaky.output_entity().read()
        except Exception as e:
            error = str(e)
        return {
            "grid": _path("flaky"),
            "error": error,
            "cell": await self._cell_state(flaky),
        }

    @service_method
    async def retry_flaky(self) -> FxtrServiceData:
        """Re-demand the errored flaky cell (the demand policy retries)."""
        flaky = await self._open_flaky()
        result = await flaky.output_entity().read()
        return {
            "grid": _path("flaky"),
            "result": await self._export(result),
            "cell": await self._cell_state(flaky),
        }

    async def _open_reversed(self) -> TaskView:
        v = await self._ensure_pipeline()
        key = fn_key_note(EXTRA_FN_NAME).store(self._runner().scope).entity_id
        await self._wf().write_handle(v.fns, key, fn=EXTRA_FN_NAME)
        return await self._wf().root.open_task(
            "reversed",
            axes=(),
            task_fn=v.fns.at(fn=EXTRA_FN_NAME),
            args={"note": v.inputs.read_only().at(x="c")},
        )

    @service_method
    async def scenario_unbound(self) -> FxtrServiceData:
        """Demand a task wired to the (unbound) ``reverse`` fn: faults.

        Self-resetting so the button always demonstrates the fault:
        releases our binding if held and clears any previous result
        first (otherwise a completed cell from an earlier arc is simply
        served fresh)."""
        reversed_ = await self._open_reversed()
        await self._wf().add_target(reversed_)
        if self._extra_release is not None:
            await self._do_release_extra()
        with contextlib.suppress(Exception):
            await self._wf().clear(reversed_)
        error: str | None = None
        try:
            await reversed_.output_entity().read()
        except Exception as e:
            error = str(e)
        return {
            "grid": _path("reversed"),
            "error": error,
            "cell": await self._cell_state(reversed_),
        }

    @service_method
    async def bind_extra(self) -> FxtrServiceData:
        """Bind the ``reverse`` fn (the boundness arc's fix)."""
        runner = self._runner()
        if self._extra_release is not None:
            return {"note": f"{EXTRA_FN_NAME!r} is already bound"}
        key = fn_key_note(EXTRA_FN_NAME).store(runner.scope).entity_id
        if self._extra_connector is None:
            # On the runner's host scope: dies with the runner, which
            # is correct (bindings are per-runner ephemera); the epoch
            # check in _runner() drops our stale handles after a crash.
            fn_ref = await runner.host_scope.mount(TaskFnAdapter(EXTRA_FN_FACTORY()))
            self._extra_connector = await wrap_singleton(runner.host_scope, fn_ref)
        self._extra_release = await runner.workflow.bind_handle(key, self._extra_connector)
        return {"bound": EXTRA_FN_NAME, "handle": await self._export(key)}

    async def _do_release_extra(self) -> None:
        assert self._extra_release is not None
        release = await self._extra_release.resolve(self._runner().scope)
        await release.release()
        self._extra_release = None

    @service_method
    async def release_extra(self) -> FxtrServiceData:
        """Release the ``reverse`` binding again (boundness flips back)."""
        if self._extra_release is None:
            return {"note": f"{EXTRA_FN_NAME!r} is not bound"}
        await self._do_release_extra()
        return {"released": EXTRA_FN_NAME}

    @service_method
    async def rerun_unbound(self) -> FxtrServiceData:
        """Re-demand the ``reversed`` cell (completes once bound)."""
        reversed_ = await self._open_reversed()
        result = await reversed_.output_entity().read()
        return {
            "grid": _path("reversed"),
            "result": await self._export(result),
            "cell": await self._cell_state(reversed_),
        }

    @service_method
    async def scenario_subtree(self) -> FxtrServiceData:
        """Build + run the ``variants`` subtree workspace: child grids
        spanning the subtree's own axis (the dashboard's subtree rows)."""
        v = await self._ensure_pipeline()
        variants = await self._wf().root.open_subtree("variants", (m.AxisSpec("v", "str"),))
        cfg = await variants.open_data("cfg", ())
        for variant, note in VARIANT_NOTES.items():
            await self._write_if_new(cfg, note, v=variant)
        styled = await variants.open_task(
            "styled",
            axes=(),
            task_fn=v.fns.at(fn="combine"),
            args={"original": v.inputs.read_only().at(x="a"), "shouted": cfg.read_only()},
        )
        results: dict[str, Any] = {}
        for variant in VARIANT_NOTES:
            await self._wf().add_target(styled, v=variant)
            results[variant] = await styled.output_entity().read(v=variant)
        return {
            "grids": [_path("variants"), _path("variants", "cfg"), _path("variants", "styled")],
            "results": await self._export(results),
        }

    @service_method
    async def scenario_nested(self) -> FxtrServiceData:
        """Target + demand the ``planned`` task, whose run creates grids
        (``draft`` data + a nested ``polish`` task) in its own internals."""
        v = await self._ensure_pipeline()
        planned = await self._wf().root.open_task(
            "planned",
            axes=(),
            task_fn=v.fns.at(fn="plan"),
            args={"note": v.inputs.read_only().at(x="a"), "fns": v.fns.read_only()},
        )
        await self._wf().add_target(planned)
        result = await planned.output_entity().read()
        return {
            "grid": _path("planned"),
            "internals": [_path("planned", "draft"), _path("planned", "polish")],
            "result": await self._export(result),
            "cell": await self._cell_state(planned),
        }

    @service_method
    async def scenario_unsettled(self) -> FxtrServiceData:
        """Target + demand the ``echoed`` task (reads ONLY ``shout``'s
        output). Afterwards, switching input *b* leaves ``echoed`` b
        *unsettled*: its own observation of shout's output still
        matches, but that support is stale."""
        v = await self._ensure_pipeline()
        echoed = await self._wf().root.open_task(
            "echoed",
            axes=X_AXES,
            task_fn=v.fns.at(fn="uppercase"),
            args={"note": v.shout.output_entity()},
        )
        cells: dict[str, Any] = {}
        for x in INPUT_NOTES:
            await self._wf().add_target(echoed, x=x)
            await echoed.output_entity().read(x=x)
            cells[x] = await self._cell_state(echoed, x=x)
        return {"grid": _path("echoed"), "cells": cells}

    @service_method
    async def scenario_rail(self) -> FxtrServiceData:
        """Build a task INSIDE a ``sweep`` subtree that vmaps the top-level
        ``inputs`` over ``x``. The ``x`` axis plate then spans two parents
        — root (``inputs``/``shout``/``combined``) and the ``sweep``
        subtree (``sweep/echo``) — so the plate view splits it into two
        boxes tied by a rail (a non-laminar grant-class)."""
        v = await self._ensure_pipeline()
        sweep = await self._wf().root.open_subtree("sweep", ())
        echo = await sweep.open_task(
            "echo",
            axes=X_AXES,
            task_fn=v.fns.at(fn="uppercase"),
            args={"note": v.inputs.read_only()},
        )
        cells: dict[str, Any] = {}
        for x in INPUT_NOTES:
            await self._wf().add_target(echo, x=x)
            await echo.output_entity().read(x=x)
            cells[x] = await self._cell_state(echo, x=x)
        return {"grids": [_path("sweep"), _path("sweep", "echo")], "cells": cells}


@dataclasses.dataclass
class MountedDemoHelper:
    service_ref: ServiceRef
    scope: FxtrScope


@contextlib.asynccontextmanager
async def mount_experiment_demo_helper(
    supervisor: RunnerSupervisor,
) -> AsyncGenerator[MountedDemoHelper]:
    """Mount the driver over ``supervisor`` (yield ``(service_ref,
    scope)`` for a boot-time ``ExternalServiceConfig``).

    The mount scope is a plain stable scope — deliberately independent
    of any runner scope, so the driver (and its panel) survives runner
    crashes; returned ids are exported into call scopes instead of
    relying on scope ancestry (see the module docstring).
    """
    async with FxtrScope(name="experiment-demo-helper") as scope:
        ref = await scope.mount(ExperimentDemoHelperImpl(supervisor))
        yield MountedDemoHelper(service_ref=ref, scope=scope)
