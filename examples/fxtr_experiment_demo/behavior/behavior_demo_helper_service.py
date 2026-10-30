"""Host-side ``behavior-demo-helper`` service: the behavior scenario's driver.

Implements :class:`behavior_demo_helper.BehaviorDemoHelperProto` (see
its module docstring for the stage map). Every method drives the *same
user capability* the browser (and any RPC client) speaks —
``behavior-workflow`` — adding only the host-side pieces a browser
can't supply: constructing config entities, holding the runner's key
table, and kicking the long-running analysis demand in the background.

Grid layout the stages build (all under the workflow root; the plan's
overview table is the reference — this is its executable form):

- ``tasks/{rollout,judge,analyze}`` — scalar handle grids, one per
  task fn.
- ``services/subject_model_services[provider]`` /
  ``services/judge_services[provider]`` / a scalar
  ``services/user_simulator_service`` — the LLM service handles; cells
  hold whichever mode's keys the current runner mount serves, so a
  mode switch + re-pressing stage 1 overwrites them (staleness fans
  through everything computed against the other mode).
- ``configs/subject_models[subj_id]`` / ``configs/user_sims[sim_id]``
  / ``configs/judges[judge_id]`` / a scalar ``configs/rollout_config``.
- ``domain/active_*`` + ``domain/sweep_config`` — the domain cells the
  analysis enumerates (open-map principle: expanding the domain is an
  ordinary write).
- ``evidence/rollouts[subj_id, sim_id, replica]`` →
  ``evidence/judgements[subj_id, sim_id, replica, judge_id]`` — the
  task grids (vmapped configs; provider-kept handle grants; the
  replica coord view).
- ``analyses/sales_report`` — the aggregate task, opened by
  ``stage_analysis``.

Like the pipeline driver, this service holds a
:class:`experiment_runner.RunnerSupervisor` (not a runner) and looks
the live runner up per call; returned entity ids live in the runner
scope, so every returned id is **exported** — copied into the active
dispatch call scope (content-addressed: same id, now coverable).
Scenario content (prompts, personas, rubrics, sweep sizes) lives in
:mod:`scenario_content`, read through the module object so tests can
monkeypatch it.

Mount via :func:`mount_behavior_demo_helper` (async CM yielding
``(service_ref, scope)`` for a boot-time ``ExternalServiceConfig``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable
from typing import Any

import anyio
import anyio.abc
from behavior_demo_helper import BEHAVIOR_DEMO_HELPER_TYPE_ID, BehaviorDemoHelperProto
from behavior_research_types import (
    ANTHROPIC_PROVIDER,
    JudgeConfig,
    JudgeResult,
    RolloutConfig,
    SubjectModelConfig,
    SweepConfig,
    Transcript,
    UserSimConfig,
    UserSimImprovementRequest,
)
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task import tables
from fxtr_experiment_task.errors import WriteConflictError, typed_wire_call
from fxtr_experiment_task.views import (
    DataFrameView,
    DataView,
    GridView,
    HandleView,
    SubtreeView,
    TaskView,
    int_coord,
)

import scenario_content as content
from analyze_task import EVIDENCE_SCHEMA
from behavior_runner import BehaviorRunner
from experiment_runner import RunnerSupervisor
from judge_task import JUDGE_DATA_SCHEMA

PROVIDER_AXES = (m.AxisSpec("provider", "str"),)
SUBJ_AXES = (m.AxisSpec("subj_id", "str"),)
SIM_AXES = (m.AxisSpec("sim_id", "str"),)
JUDGE_AXES = (m.AxisSpec("judge_id", "str"),)
ROLLOUT_AXES = (*SUBJ_AXES, *SIM_AXES, m.AxisSpec("replica", "int"))
JUDGEMENT_AXES = (*ROLLOUT_AXES, *JUDGE_AXES)
REQUEST_AXES = (m.AxisSpec("request_id", "str"),)

# The domain cells' frame schemas (zero-axis dataframe cells whose
# single column enumerates the semantic axis; column names deliberately
# match the corresponding axis names, so the analysis's gather `on=`
# map reads as identity).
SUBJECT_DOMAIN_SCHEMA: m.FrameSchema = (m.ColumnSpec("subj_id", "str"),)
SIM_DOMAIN_SCHEMA: m.FrameSchema = (m.ColumnSpec("sim_id", "str"),)
JUDGE_DOMAIN_SCHEMA: m.FrameSchema = (m.ColumnSpec("judge_id", "str"),)

#: The analysis task grid's key under ``analyses/``.
ANALYSIS_KEY = "sales_report"


def _path(*keys: str) -> str:
    return enc.encode_key_path(list(keys))


@dataclasses.dataclass
class _Views:
    """One driver call's views (re-opened per call; opens attach)."""

    rollout_h: HandleView
    judge_h: HandleView
    analyze_h: HandleView
    improve_h: HandleView
    subject_svcs: HandleView
    user_sim_svc: HandleView
    judge_svcs: HandleView
    subject_models: DataView
    user_sims: DataView
    judges: DataView
    rollout_config: DataView
    active_subjects: DataFrameView
    active_sims: DataFrameView
    active_judges: DataFrameView
    sweep: DataView
    rollouts: TaskView
    judgements: TaskView
    analyses: SubtreeView
    improve_request: DataView
    improve_flow: TaskView


@dataclasses.dataclass
class _Domains:
    """The domain cells' current contents (loaded via labeled inspection)."""

    subjects: tuple[str, ...]
    sims: tuple[str, ...]
    judges: tuple[str, ...]
    replicas: int


class RunnerDownError(RuntimeError):
    pass


class BehaviorDemoHelperImpl(TypedServiceImpl[BehaviorDemoHelperProto]):
    """See the module docstring + the proto's stage map."""

    def __init__(self, supervisor: RunnerSupervisor) -> None:
        super().__init__()
        self._supervisor = supervisor
        # Background demands: label → state ("running" / "completed" /
        # "failed: …" / "interrupted"), plus each live demand's cancel
        # scope — crash_runner cancels these FIRST so running
        # generations interrupt through the engine's tested
        # cancellation path (raise-time poisoning), not lease breakage.
        self._background: dict[str, str] = {}
        self._demand_scopes: set[anyio.CancelScope] = set()
        self._demands_drained = anyio.Event()
        self._demands_drained.set()

    def _target_type_annotation(self) -> Any:
        return BehaviorDemoHelperProto

    def _service_type_id(self) -> FxtrID:
        return BEHAVIOR_DEMO_HELPER_TYPE_ID

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncGenerator[anyio.abc.TaskGroup]:
        """A task group for the background analysis demand."""
        async with anyio.create_task_group() as tg:
            try:
                yield tg
            finally:
                tg.cancel_scope.cancel()

    # -- runner access ------------------------------------------------------------

    def _runner(self) -> BehaviorRunner:
        runner = self._supervisor.current
        if runner is None:
            raise RunnerDownError(
                "the behavior workflow runner is down (crashed) — press 'Revive runner'"
            )
        assert isinstance(runner, BehaviorRunner)  # this supervisor mounts BehaviorRunners
        return runner

    def _wf(self):
        return self._runner().workflow

    async def _export(self, value: Any) -> Any:
        """Re-cover returned ids in THIS call's scope (see module doc).

        Ids returned by capability calls are loadable in the runner
        scope (``ensure_loadable`` re-attaches served ids); the driver's
        call scope doesn't descend from it, so copy the entity closure
        into the active dispatch scope — same id, now liftable for the
        return packet. Recurses into dicts; non-id values pass through.
        """
        if isinstance(value, dict):
            return {k: await self._export(v) for k, v in value.items()}
        if isinstance(value, FxtrID):
            return await get_active_scope().copy_from_scope(value, self._runner().scope)
        return value

    # -- write helpers -------------------------------------------------------------

    async def _write_value(self, view: DataView, entity: DataclassEntity, **coords: Any) -> FxtrID:
        """Plain write: rewriting the identical value is a no-op success
        (buttons stay re-pressable), while a *different* existing value
        raises ``WriteConflictError`` out of the button — surfaced in
        the panel rather than silently resolved. To replace an edited
        cell, overwrite explicitly through the capability/UI."""
        value = entity.store(self._runner().scope).entity_id
        await typed_wire_call(view.write(value, **coords))
        return value

    async def _write_ids_frame(self, view: DataFrameView, ids: Iterable[str]) -> None:
        """Write a domain cell's single-column id frame (canonical:
        sorted, deduplicated). Same button semantics as `_write_value`:
        rewriting the identical multiset is a no-op success; a
        *different* existing value raises ``WriteConflictError``."""
        table = tables.make_frame(view.schema, [(i,) for i in sorted(set(ids))])
        await typed_wire_call(view.write_table(table))

    async def _write_handle_current(self, view: HandleView, key: FxtrID, **coords: Any) -> None:
        """Write a handle cell to the current mode's key, overwriting a
        different mode's key if present (the deliberate mode-switch
        gesture: dependents of the old key go stale)."""
        wf = self._wf()
        try:
            await typed_wire_call(wf.write_handle(view, key, **coords))
        except WriteConflictError:
            await typed_wire_call(wf.overwrite(view, key, **coords))

    # -- shared structure ----------------------------------------------------------

    async def _ensure_structure(self) -> _Views:
        """Open (create-or-attach) the whole namespace + write handle keys."""
        runner = self._runner()
        wf = runner.workflow

        tasks = await wf.root.open_subtree("tasks", ())
        rollout_h = await wf.open_handle(tasks, "rollout", ())
        judge_h = await wf.open_handle(tasks, "judge", ())
        analyze_h = await wf.open_handle(tasks, "analyze", ())
        improve_h = await wf.open_handle(tasks, "improve_sim", ())
        await self._write_handle_current(rollout_h, runner.fn_keys["rollout"])
        await self._write_handle_current(judge_h, runner.fn_keys["judge"])
        await self._write_handle_current(analyze_h, runner.fn_keys["analyze"])
        await self._write_handle_current(improve_h, runner.fn_keys["improve_sim"])

        services = await wf.root.open_subtree("services", ())
        subject_svcs = await wf.open_handle(services, "subject_model_services", PROVIDER_AXES)
        user_sim_svc = await wf.open_handle(services, "user_simulator_service", ())
        judge_svcs = await wf.open_handle(services, "judge_services", PROVIDER_AXES)
        await self._write_handle_current(
            subject_svcs, runner.svc_keys["subject_model"], provider=ANTHROPIC_PROVIDER
        )
        await self._write_handle_current(user_sim_svc, runner.svc_keys["user_sim"])
        await self._write_handle_current(
            judge_svcs, runner.svc_keys["judge_model"], provider=ANTHROPIC_PROVIDER
        )

        configs = await wf.root.open_subtree("configs", ())
        subject_models = await configs.open_data("subject_models", SUBJ_AXES)
        user_sims = await configs.open_data("user_sims", SIM_AXES)
        judges = await configs.open_data("judges", JUDGE_AXES)
        rollout_config = await configs.open_data("rollout_config", ())

        domain = await wf.root.open_subtree("domain", ())
        active_subjects = await domain.open_dataframe(
            "active_subject_models", (), SUBJECT_DOMAIN_SCHEMA
        )
        active_sims = await domain.open_dataframe("active_user_sims", (), SIM_DOMAIN_SCHEMA)
        active_judges = await domain.open_dataframe("active_judges", (), JUDGE_DOMAIN_SCHEMA)
        sweep = await domain.open_data("sweep_config", ())

        evidence = await wf.root.open_subtree("evidence", ())
        rollouts = await evidence.open_task(
            "rollouts",
            axes=ROLLOUT_AXES,
            task_fn=rollout_h,
            args={
                "subject": subject_models.read_only(),
                "sim": user_sims.read_only(),
                "rollout_cfg": rollout_config.read_only(),
                "replica": int_coord("replica"),
                "subject_services": subject_svcs.read_only(),
                "sim_service": user_sim_svc.read_only(),
            },
        )
        judgements = await evidence.open_task(
            "judgements",
            axes=JUDGEMENT_AXES,
            task_fn=judge_h,
            args={
                "transcript": rollouts.output_entity(),
                "judge_cfg": judges.read_only(),
                "judge_services": judge_svcs.read_only(),
            },
            outputs={
                "verdict": m.EntityOutput(),
                "data": m.FrameOutput(schema=JUDGE_DATA_SCHEMA),
            },
        )

        analyses = await wf.root.open_subtree("analyses", ())

        # The transformation workspace: one cell per improvement request
        # (regular by construction — every request_id cell holds the same
        # request+flow pair; the flow's rule is fixed here, and per-request
        # variation travels inside the request ENTITY, never the rule).
        transformations = await wf.root.open_subtree("transformations", ())
        improve = await transformations.open_subtree("user_sim_improve", REQUEST_AXES)
        improve_request = await improve.open_data("request", ())
        improve_flow = await improve.open_task(
            "flow",
            axes=(),
            task_fn=improve_h,
            args={
                "request": improve_request.read_only(),
                "subject_services": subject_svcs.read_only(),
                "sim_service": user_sim_svc.read_only(),
                "judge_services": judge_svcs.read_only(),
                "rollout_fn": rollout_h.read_only(),
                "judge_fn": judge_h.read_only(),
            },
        )

        return _Views(
            rollout_h=rollout_h,
            judge_h=judge_h,
            analyze_h=analyze_h,
            improve_h=improve_h,
            subject_svcs=subject_svcs,
            user_sim_svc=user_sim_svc,
            judge_svcs=judge_svcs,
            subject_models=subject_models,
            user_sims=user_sims,
            judges=judges,
            rollout_config=rollout_config,
            active_subjects=active_subjects,
            active_sims=active_sims,
            active_judges=active_judges,
            sweep=sweep,
            rollouts=rollouts,
            judgements=judgements,
            analyses=analyses,
            improve_request=improve_request,
            improve_flow=improve_flow,
        )

    async def _open_analysis(self, v: _Views) -> TaskView:
        """Create-or-attach ``analyses/sales_report`` (the stage-3 open)."""
        return await v.analyses.open_task(
            ANALYSIS_KEY,
            axes=(),
            task_fn=v.analyze_h,
            args={
                "judgements": v.judgements.output_entity("verdict"),
                "judgement_data": v.judgements.output_frame("data"),
                "active_subjects": v.active_subjects.read_only(),
                "active_sims": v.active_sims.read_only(),
                "active_judges": v.active_judges.read_only(),
                "sweep": v.sweep.read_only(),
            },
            outputs={
                "report": m.EntityOutput(),
                "evidence": m.FrameOutput(schema=EVIDENCE_SCHEMA),
            },
        )

    async def _load_domain_entity(self, view: DataView, cls: type) -> Any | None:
        """A domain cell's content via labeled inspection (never demands);
        ``None`` if unpopulated."""
        report = await self._wf().inspect(view)
        if report.value is None:
            return None
        # Membership: served by the capability, so loadable in the
        # runner scope (ensure_loadable re-attaches served ids).
        return await EntityRef(report.value, cls).load(self._runner().scope)

    async def _load_domain_ids(self, view: DataFrameView) -> tuple[str, ...] | None:
        """A domain frame's id column; ``None`` if unpopulated. The
        populated check goes through labeled inspection (never
        executes anything); the read then serves the user-sourced —
        hence always-fresh — frame."""
        report = await self._wf().inspect(view)
        if report.value is None:
            return None
        table = await view.read_table()
        # Value-sorted: the canonical frame row order is lex on encoded
        # row BYTES (CBOR length prefix first), not on string values.
        return tuple(sorted(table[view.schema[0].name].to_list()))

    async def _domains(self, v: _Views) -> _Domains | None:
        """The current domain contents, or None until stage_inputs ran."""
        subjects = await self._load_domain_ids(v.active_subjects)
        sims = await self._load_domain_ids(v.active_sims)
        judges = await self._load_domain_ids(v.active_judges)
        sweep = await self._load_domain_entity(v.sweep, SweepConfig)
        if subjects is None or sims is None or judges is None or sweep is None:
            return None
        return _Domains(subjects=subjects, sims=sims, judges=judges, replicas=sweep.replicas)

    def _evidence_coords(self, doms: _Domains) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The domain cross product as (rollout coords, judgement coords)."""
        rollout_coords = [
            {"subj_id": s, "sim_id": u, "replica": r}
            for s in doms.subjects
            for u in doms.sims
            for r in range(doms.replicas)
        ]
        judgement_coords = [{**c, "judge_id": j} for c in rollout_coords for j in doms.judges]
        return rollout_coords, judgement_coords

    async def _cell_state(self, view: GridView, **coords: Any) -> dict[str, Any]:
        report = await self._wf().inspect(view, **coords)
        state = {
            "status": report.status,
            "currency": report.currency,
            "value": await self._export(report.value),
        }
        return {k: v for k, v in state.items() if v is not None}

    # -- the stages ---------------------------------------------------------------

    @service_method
    async def stage_structure(self) -> FxtrServiceData:
        """Open every grid + write the current mode's handle keys."""
        runner = self._runner()
        await self._ensure_structure()
        return {
            "llm_mode": runner.llm_mode,
            "grids": [
                _path("tasks"),
                _path("services"),
                _path("configs"),
                _path("domain"),
                _path("evidence", "rollouts"),
                _path("evidence", "judgements"),
                _path("analyses"),
            ],
            "note": (
                "structure is in place — handle cells name the "
                f"{runner.llm_mode!r}-mode services"
            ),
        }

    @service_method
    async def stage_inputs(self) -> FxtrServiceData:
        """Write the stage-2 configs + the matching domain cells."""
        v = await self._ensure_structure()
        for subj_id, cfg in content.STAGE2_SUBJECTS.items():
            await self._write_value(v.subject_models, cfg, subj_id=subj_id)
        for sim_id, sim in content.STAGE2_SIMS.items():
            await self._write_value(v.user_sims, sim, sim_id=sim_id)
        for judge_id, judge in content.STAGE2_JUDGES.items():
            await self._write_value(v.judges, judge, judge_id=judge_id)
        await self._write_value(v.rollout_config, content.ROLLOUT)
        await self._write_ids_frame(v.active_subjects, content.STAGE2_SUBJECTS)
        await self._write_ids_frame(v.active_sims, content.STAGE2_SIMS)
        await self._write_ids_frame(v.active_judges, content.STAGE2_JUDGES)
        await self._write_value(v.sweep, content.STAGE2_SWEEP)
        return {
            "subjects": sorted(content.STAGE2_SUBJECTS),
            "sims": sorted(content.STAGE2_SIMS),
            "judges": sorted(content.STAGE2_JUDGES),
            "replicas": content.STAGE2_SWEEP.replicas,
            "max_turns": content.ROLLOUT.max_turns,
        }

    @service_method
    async def stage_analysis(self) -> FxtrServiceData:
        """Open the analysis task grid (target/run are separate buttons)."""
        v = await self._ensure_structure()
        analysis = await self._open_analysis(v)
        return {
            "grid": _path("analyses", ANALYSIS_KEY),
            "cell": await self._cell_state(analysis),
            "note": "opened — press 'Target analysis' then 'Run analysis'",
        }

    @service_method
    async def stage_augment(self) -> FxtrServiceData:
        """Write the stage-4 configs; OVERWRITE the domain cells (the
        analysis goes stale; existing evidence stays fresh)."""
        v = await self._ensure_structure()
        wf = self._wf()
        for subj_id, cfg in content.STAGE4_SUBJECTS.items():
            await self._write_value(v.subject_models, cfg, subj_id=subj_id)
        for sim_id, sim in content.STAGE4_SIMS.items():
            await self._write_value(v.user_sims, sim, sim_id=sim_id)
        for judge_id, judge in content.STAGE4_JUDGES.items():
            await self._write_value(v.judges, judge, judge_id=judge_id)

        runner = self._runner()

        async def overwrite_domain(view: DataFrameView, ids: Iterable[str]) -> None:
            table = tables.make_frame(view.schema, [(i,) for i in sorted(set(ids))])
            await typed_wire_call(wf.overwrite_table(view, table))

        await overwrite_domain(
            v.active_subjects, {**content.STAGE2_SUBJECTS, **content.STAGE4_SUBJECTS}
        )
        await overwrite_domain(v.active_sims, {**content.STAGE2_SIMS, **content.STAGE4_SIMS})
        await overwrite_domain(v.active_judges, {**content.STAGE2_JUDGES, **content.STAGE4_JUDGES})
        await typed_wire_call(
            wf.overwrite(v.sweep, content.STAGE4_SWEEP.store(runner.scope).entity_id)
        )

        out: dict[str, Any] = {
            "added_subjects": sorted(content.STAGE4_SUBJECTS),
            "added_sims": sorted(content.STAGE4_SIMS),
            "added_judges": sorted(content.STAGE4_JUDGES),
            "replicas": content.STAGE4_SWEEP.replicas,
        }
        with contextlib.suppress(Exception):  # absent until stage_analysis ran
            analysis = await self._wf().attach(v.analyses, ANALYSIS_KEY)
            out["analysis"] = await self._cell_state(analysis)
        out["note"] = "domain widened — press 'Clear stale' then 'Run analysis'"
        return out

    # -- the run controls -----------------------------------------------------------

    @service_method
    async def target_analysis(self) -> FxtrServiceData:
        """Durably target the analysis cell (declarative DAG-root intent)."""
        v = await self._ensure_structure()
        analysis = await self._open_analysis(v)
        await self._wf().add_target(analysis)
        return {"targeted": _path("analyses", ANALYSIS_KEY)}

    def _spawn_demand(self, label: str, read: Callable[[], Coroutine[Any, Any, Any]]) -> bool:
        """Run ``read()`` on the mount's task group under a tracked
        cancel scope (class docstring on `_background`). Returns False
        if ``label`` already has a demand in flight."""
        if self._background.get(label) == "running":
            return False
        tg = self._fxtr_get_lifespan_state()
        self._background[label] = "running"
        self._demands_drained = anyio.Event()

        async def demand() -> None:
            with anyio.CancelScope() as scope:
                self._demand_scopes.add(scope)
                try:
                    await read()
                    self._background[label] = "completed"
                except Exception as e:  # faults/staleness: visible via status + dashboard
                    self._background[label] = f"failed: {e}"
                    print(f"[behavior-demo-helper] {label} demand ended: {e!r}", flush=True)
                finally:
                    self._demand_scopes.discard(scope)
                    if not self._demand_scopes:
                        self._demands_drained.set()
            if scope.cancel_called:
                self._background[label] = "interrupted (runner crash)"

        tg.start_soon(demand)
        return True

    @service_method
    async def run_analysis(self) -> FxtrServiceData:
        """Demand the analysis in the background (pulls the whole
        evidence pipeline; watch the dashboard or press 'Status')."""
        v = await self._ensure_structure()
        analysis = await self._open_analysis(v)
        await self._wf().add_target(analysis)
        started = self._spawn_demand("analysis", lambda: analysis.output_entity("report").read())
        return {
            "grid": _path("analyses", ANALYSIS_KEY),
            "note": (
                "analysis demand running in the background"
                if started
                else "an analysis run is already in flight"
            ),
        }

    @service_method
    async def clear_stale(self) -> FxtrServiceData:
        """The v0 stale-rerun gesture: clear every non-fresh task cell
        across the evidence grids + the analysis (demands fault on stale
        by design; cleared cells recompute on the next run)."""
        v = await self._ensure_structure()
        wf = self._wf()
        doms = await self._domains(v)
        if doms is None:
            return {"note": "domain cells are unpopulated — press 'Stage 2: inputs' first"}
        rollout_coords, judgement_coords = self._evidence_coords(doms)

        cleared: list[str] = []

        async def sweep_grid(label: str, view: GridView, coords_list: list[dict[str, Any]]) -> None:
            for coords in coords_list:
                report = await wf.inspect(view, **coords)
                if report.status is not None and report.currency != "fresh":
                    await wf.clear(view, **coords)
                    cleared.append(f"{label} {coords}" if coords else label)

        await sweep_grid(_path("evidence", "rollouts"), v.rollouts, rollout_coords)
        await sweep_grid(_path("evidence", "judgements"), v.judgements, judgement_coords)
        with contextlib.suppress(Exception):  # absent until stage_analysis ran
            analysis = await wf.attach(v.analyses, ANALYSIS_KEY)
            await sweep_grid(_path("analyses", ANALYSIS_KEY), analysis, [{}])
        # Transformation flows (enumerated by presence query — the flow
        # grid's cells are per-request coordinates the domain doesn't know).
        flow_coords = [
            {"request_id": cell.coords[0]} for cell in await wf.list_cells(v.improve_flow)
        ]
        await sweep_grid(
            _path("transformations", "user_sim_improve", "flow"), v.improve_flow, flow_coords
        )
        return {"cleared": cleared, "count": len(cleared)}

    @service_method
    async def status(self) -> FxtrServiceData:
        """One labeled snapshot: domain sizes, per-grid currency counts
        over the domain cross product, the analysis cell's state."""
        runner = self._runner()
        v = await self._ensure_structure()
        wf = self._wf()
        out: dict[str, Any] = {"llm_mode": runner.llm_mode}
        if self._background:
            out["background"] = dict(self._background)

        doms = await self._domains(v)
        if doms is None:
            out["note"] = "domain cells are unpopulated — press 'Stage 2: inputs' first"
            return out
        out["domain"] = {
            "subjects": list(doms.subjects),
            "sims": list(doms.sims),
            "judges": list(doms.judges),
            "replicas": doms.replicas,
        }

        async def counts(view: GridView, coords_list: Iterable[dict[str, Any]]) -> dict[str, int]:
            buckets: dict[str, int] = {}
            for coords in coords_list:
                report = await wf.inspect(view, **coords)
                if report.status is None:
                    bucket = "missing"
                elif report.status != "complete":
                    bucket = report.status
                else:
                    bucket = report.currency or report.status
                buckets[bucket] = buckets.get(bucket, 0) + 1
            return buckets

        rollout_coords, judgement_coords = self._evidence_coords(doms)
        out["rollouts"] = await counts(v.rollouts, rollout_coords)
        out["judgements"] = await counts(v.judgements, judgement_coords)
        try:
            analysis = await wf.attach(v.analyses, ANALYSIS_KEY)
        except Exception:
            out["analysis"] = "not created — press 'Stage 3: analysis'"
        else:
            out["analysis"] = await self._cell_state(analysis)
        return out

    # -- the transformation ----------------------------------------------------------

    @service_method
    async def improve_sim(
        self, sim_id: str, subj_id: str, judge_id: str, instruction: str
    ) -> FxtrServiceData:
        """Build a pinned-snapshot improvement request from the selected
        evidence and demand its flow in the background (proto doc)."""
        v = await self._ensure_structure()
        wf = self._wf()
        runner = self._runner()
        doms = await self._domains(v)
        if doms is None:
            return {"error": "domain cells are unpopulated — press 'Stage 2: inputs' first"}

        # The pinned configs, via labeled inspection (never demands).
        config_reports = {
            "sim": await wf.inspect(v.user_sims, sim_id=sim_id),
            "subject": await wf.inspect(v.subject_models, subj_id=subj_id),
            "judge": await wf.inspect(v.judges, judge_id=judge_id),
            "rollout": await wf.inspect(v.rollout_config),
        }
        missing = sorted(name for name, rep in config_reports.items() if rep.value is None)
        if missing:
            return {"error": f"missing configs for: {', '.join(missing)}"}

        # The source evidence: every replica whose rollout AND judgement
        # are complete — ANY currency (the refs pin exact versions; a
        # stale-but-real data point is an honest input).
        rollout_refs: list[EntityRef[Transcript]] = []
        judgement_refs: list[EntityRef[JudgeResult]] = []
        for rep_i in range(doms.replicas):
            coords = {"subj_id": subj_id, "sim_id": sim_id, "replica": rep_i}
            rollout_rep = await wf.inspect(v.rollouts, **coords)
            judgement_rep = await wf.inspect(v.judgements, **coords, judge_id=judge_id)
            if rollout_rep.results is not None and judgement_rep.results is not None:
                # Membership: inspection-served ids are re-attached, so
                # loadable in the runner scope (where the request stores).
                rollout_refs.append(EntityRef(rollout_rep.results["result"], Transcript))
                judgement_refs.append(EntityRef(judgement_rep.results["verdict"], JudgeResult))
        if not rollout_refs:
            return {
                "error": (
                    f"no completed rollout+judgement pairs for sim={sim_id!r} "
                    f"subj={subj_id!r} judge={judge_id!r} — run the analysis first"
                )
            }

        request = UserSimImprovementRequest(
            instruction=instruction,
            improver_provider=ANTHROPIC_PROVIDER,
            improver_model=content.DEFAULT_MODEL,
            sim_config=EntityRef(config_reports["sim"].value, UserSimConfig),
            subject_config=EntityRef(config_reports["subject"].value, SubjectModelConfig),
            judge_config=EntityRef(config_reports["judge"].value, JudgeConfig),
            rollout_config=EntityRef(config_reports["rollout"].value, RolloutConfig),
            rollouts=tuple(rollout_refs),
            judgements=tuple(judgement_refs),
        )
        # Minted driver-side, where nondeterminism is legal; a re-press
        # makes a NEW request cell rather than colliding.
        request_id = f"{sim_id}-{time.strftime('%Y%m%dT%H%M%S')}"
        request_value = await self._write_value(v.improve_request, request, request_id=request_id)
        await wf.add_target(v.improve_flow, request_id=request_id)
        started = self._spawn_demand(
            f"improve:{request_id}",
            lambda: v.improve_flow.output_entity().read(request_id=request_id),
        )
        assert started  # the request_id is fresh, so the label is too
        return {
            "request_id": request_id,
            "grid": _path("transformations", "user_sim_improve", "flow"),
            "request": await self._export(request_value),
            "sources": len(rollout_refs),
            "note": "improvement flow running in the background — press 'Status' for progress",
        }

    # -- the crash/revive arc ---------------------------------------------------------

    @service_method
    async def crash_runner(self) -> FxtrServiceData:
        """Forcibly tear the runner down mid-whatever-it's-doing (the
        in-process interruption arc: running generations poison to
        interrupted; durable checkpoints + completed nested generations
        survive).

        Order: cancel our live background demands first (the engine's
        tested cancellation path), then tear the runner down.
        """
        for scope in list(self._demand_scopes):
            scope.cancel()
        with anyio.move_on_after(10):
            await self._demands_drained.wait()
        crashed = await self._supervisor.crash()
        return {
            "crashed": crashed,
            "note": (
                "runner torn down — buttons will fail until revived"
                if crashed
                else "runner was already down"
            ),
        }

    @service_method
    async def revive_runner(self) -> FxtrServiceData:
        """Bring a fresh runner up over the same durable state (fresh
        bindings under the same keys, re-registered capability)."""
        revived = await self._supervisor.revive()
        return {
            "revived": revived,
            "note": (
                "runner is up — re-run interrupted work to resume through checkpoints"
                if revived
                else "runner was already up"
            ),
        }


@dataclasses.dataclass
class MountedBehaviorHelper:
    service_ref: ServiceRef
    scope: FxtrScope


@contextlib.asynccontextmanager
async def mount_behavior_demo_helper(
    supervisor: RunnerSupervisor,
) -> AsyncGenerator[MountedBehaviorHelper]:
    """Mount the driver over ``supervisor`` (yield ``(service_ref,
    scope)`` for a boot-time ``ExternalServiceConfig``).

    The mount scope is a plain stable scope, deliberately independent of
    any runner scope (the driver survives runner teardown; returned ids
    are exported into call scopes instead of relying on scope ancestry
    — see the module docstring).
    """
    async with FxtrScope(name="behavior-demo-helper") as scope:
        ref = await scope.mount(BehaviorDemoHelperImpl(supervisor))
        yield MountedBehaviorHelper(service_ref=ref, scope=scope)
