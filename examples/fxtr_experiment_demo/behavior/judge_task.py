"""The judge task fn: score one transcript against one rubric.

Runs on ``evidence/judgements`` cells (axes ``subj_id, sim_id,
replica, judge_id``). The outer level reads the vmapped transcript
(demanding the rollout) and judge config, resolves the provider's
judge-service key, and runs one checkpointed step for the LLM call.
Two named outputs: ``verdict`` — the full
:class:`behavior_research_types.JudgeResult` entity — and ``data`` — a
one-row :data:`JUDGE_DATA_SCHEMA` frame carrying the judgement's
scalar (the score), which the analysis ``gather``\\ s across the whole
evidence cross product.

``JUDGE_DATA_SCHEMA`` lives here (host-side), not in
``behavior_research_types``: the marker package feeds the workdir's
type-entities venv, which deliberately does not depend on
``fxtr_experiment``.

Scope note: bare ``.store()`` / ``.load()`` — run-scope user-code
style, as in ``rollout_task.py``.
"""

from __future__ import annotations

import dataclasses

import polars as pl
from behavior_research_types import JudgeConfig, JudgeModelProto, JudgeResult, Transcript
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr_experiment_task import model as m
from fxtr_experiment_task import tables
from fxtr_experiment_task.task_api import StepContext, TaskContext
from fxtr_experiment_task.task_adapter import task_fn
from fxtr_experiment_task.view_runtime import step_fn
from fxtr_experiment_task.views import DataView, HandleView

from rollout_task import BEHAVIOR_STEPS_NS

#: The ``data`` frame output's schema: one row per judgement cell.
JUDGE_DATA_SCHEMA: m.FrameSchema = (m.ColumnSpec("score", "float"),)


@step_fn(type_id=define_type(BEHAVIOR_STEPS_NS, "judge_call"))
async def judge_call_step(
    ctx: StepContext,
    *,
    key: FxtrID,
    rubric: str,
    model: str,
    messages: list[FxtrServiceData],
) -> FxtrServiceData:
    """One judge verdict (an LLM call — miss path only)."""
    svc = await ctx.connect(key)
    judge = await svc.replace_service_type(JudgeModelProto).resolve()
    return await judge.judge(rubric, model, messages)


@task_fn
async def judge_task(
    ctx: TaskContext,
    /,
    *,
    transcript: DataView,
    judge_cfg: DataView,
    judge_services: HandleView,
) -> dict[str, FxtrID | pl.DataFrame]:
    """See the module docstring.

    Args (all views, wired by the driver's ``evidence/judgements`` rule):
        transcript: Fully-bound task-output view — this cell's rollout
            :class:`Transcript` (three axes vmap-consumed); reading it
            demands the rollout.
        judge_cfg: Fully-bound data view — this cell's
            :class:`JudgeConfig` (``judge_id`` consumed).
        judge_services: Handle view with axis ``provider`` kept; the
            config's provider picks the key (a data-dependent read).
    """
    # Keep the refs: they feed the JudgeResult's provenance fields below.
    transcript_ref = EntityRef(await transcript.read(), Transcript)
    cfg_ref = EntityRef(await judge_cfg.read(), JudgeConfig)
    t = await transcript_ref.load()
    cfg = await cfg_ref.load()
    judge_key = await judge_services.read_key(provider=cfg.provider)

    await ctx.log(f"judging {len(t.messages)}-message transcript")
    calls = await ctx.own_internals().open_checkpoint("judge_call", axes=())
    verdict = await calls.run_step(
        judge_call_step,
        args={
            "key": judge_key,
            "rubric": cfg.rubric,
            "model": cfg.model,
            "messages": [dataclasses.asdict(msg) for msg in t.messages],
        },
        coords={},
    )
    score = float(verdict["score"])
    result = JudgeResult(
        score=score,
        reasoning=str(verdict["reasoning"]),
        judge_config=cfg_ref,
        transcript=transcript_ref,
    )
    return {
        "verdict": result.store().entity_id,
        "data": tables.make_frame(JUDGE_DATA_SCHEMA, [(score,)]),
    }
