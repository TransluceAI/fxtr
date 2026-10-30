"""The rollout task fn: one subject-model x user-sim conversation.

Runs on ``evidence/rollouts`` cells (axes ``subj_id, sim_id, replica``).
The outer level reads the vmapped configs, resolves the service handle
keys, and drives a deterministic turn loop; **every LLM call happens
inside a checkpointed step**, so a killed run resumes mid-conversation
with completed turns served from durable checkpoints. Output: a
:class:`behavior_research_types.Transcript` entity.

Inside task fns the active scope is the run scope set by the framework
(the sanctioned user-code style), so bare ``.store()`` / ``.load()``
are correct here — this is demo *user code*, not library code.
"""

from __future__ import annotations

import uuid

from behavior_research_types import (
    ChatMessage,
    RolloutConfig,
    SubjectModelConfig,
    SubjectModelProto,
    Transcript,
    UserSimConfig,
    UserSimProto,
)
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr_experiment_task import model as m
from fxtr_experiment_task.task_api import StepContext, TaskContext
from fxtr_experiment_task.task_adapter import task_fn
from fxtr_experiment_task.view_runtime import step_fn
from fxtr_experiment_task.views import CoordView, DataView, HandleView

#: Stable namespace UUID for the behavior task fns' step type ids.
BEHAVIOR_STEPS_NS = uuid.UUID("b3e9d5a7-14c2-4f8b-9d60-8a27c1e4f5b9")


@step_fn(type_id=define_type(BEHAVIOR_STEPS_NS, "sim_turn"))
async def sim_turn_step(
    ctx: StepContext,
    *,
    key: FxtrID,
    task_prompt: str,
    persona: str,
    messages: list[FxtrServiceData],
) -> str:
    """One simulated-user turn (an LLM call — miss path only)."""
    svc = await ctx.connect(key)
    sim = await svc.replace_service_type(UserSimProto).resolve()
    return await sim.next_message(task_prompt, persona, messages)


@step_fn(type_id=define_type(BEHAVIOR_STEPS_NS, "subject_turn"))
async def subject_turn_step(
    ctx: StepContext,
    *,
    key: FxtrID,
    system_prompt: str,
    model: str,
    messages: list[FxtrServiceData],
) -> str:
    """One subject-model turn (an LLM call — miss path only)."""
    svc = await ctx.connect(key)
    subject = await svc.replace_service_type(SubjectModelProto).resolve()
    return await subject.reply(system_prompt, model, messages)


@task_fn
async def rollout_task(
    ctx: TaskContext,
    /,
    *,
    subject: DataView,
    sim: DataView,
    rollout_cfg: DataView,
    replica: CoordView,
    subject_services: HandleView,
    sim_service: HandleView,
) -> FxtrID:
    """See the module docstring.

    Args (all views, wired by the driver's ``evidence/rollouts`` rule):
        subject: Fully-bound data view — this cell's
            :class:`SubjectModelConfig` (``subj_id`` vmap-consumed).
        sim: Fully-bound data view — this cell's :class:`UserSimConfig`.
        rollout_cfg: Fully-bound data view — the shared
            :class:`RolloutConfig`.
        replica: Zero-axis coord view — this cell's replica index.
        subject_services: Handle view with axis ``provider`` kept; the
            config's provider picks the key (a data-dependent read).
        sim_service: Zero-axis handle view — the one user simulator.
    """
    # Keep the refs: they feed the Transcript's provenance fields below.
    subj_ref = EntityRef(await subject.read(), SubjectModelConfig)
    sim_ref = EntityRef(await sim.read(), UserSimConfig)
    run_ref = EntityRef(await rollout_cfg.read(), RolloutConfig)
    subj_cfg = await subj_ref.load()
    sim_cfg = await sim_ref.load()
    run_cfg = await run_ref.load()
    rep = replica.value()
    subject_key = await subject_services.read_key(provider=subj_cfg.provider)
    sim_key = await sim_service.read_key()

    turns = await ctx.own_internals().open_checkpoint("turns", axes=(m.AxisSpec("i", "int"),))
    messages: list[FxtrServiceData] = []  # wire shape, subject-model perspective
    for turn in range(run_cfg.max_turns):
        await ctx.log(f"replica {rep}, turn {turn + 1}/{run_cfg.max_turns}: user simulator")
        user_text = await turns.run_step(
            sim_turn_step,
            args={
                "key": sim_key,
                "task_prompt": sim_cfg.task_prompt,
                "persona": sim_cfg.persona,
                "messages": messages,
            },
            coords={"i": 2 * turn},
        )
        messages.append({"role": "user", "text": user_text})
        await ctx.log(f"replica {rep}, turn {turn + 1}/{run_cfg.max_turns}: subject model")
        assistant_text = await turns.run_step(
            subject_turn_step,
            args={
                "key": subject_key,
                "system_prompt": subj_cfg.system_prompt,
                "model": subj_cfg.model,
                "messages": messages,
            },
            coords={"i": 2 * turn + 1},
        )
        messages.append({"role": "assistant", "text": assistant_text})

    transcript = Transcript(
        messages=tuple(ChatMessage(role=msg["role"], text=msg["text"]) for msg in messages),
        subject_config=subj_ref,
        sim_config=sim_ref,
        rollout_config=run_ref,
    )
    return transcript.store().entity_id
