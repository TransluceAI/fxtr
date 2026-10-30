"""The user-sim improvement task fn: an ABR-style content rewrite.

Runs on ``transformations/user_sim_improve``'s ``flow`` cells. The
input is a :class:`behavior_research_types.UserSimImprovementRequest`
— a pinned snapshot carrying the configs and the source evidence by
ref — and the flow trials each candidate persona by **running the
same rollout/judge task fns in its own internals**: state-isolated
from the top-level ``evidence/`` grids, wired to the very same task
logic through the granted ``tasks/rollout`` / ``tasks/judge`` handles.

Structure per run (all inside ``ctx.own_internals()``):

- scalar data grids ``subject_cfg`` / ``rollout_cfg`` / ``judge_cfg``
  (written from the request's refs — the pinned configs, materialized
  where the nested rules can address them);
- ``bios[iteration]`` — the candidate :class:`UserSimConfig` per round;
- ``rollouts[iteration]`` + ``judgements[iteration]`` — nested task
  grids wired exactly like the top-level evidence pipeline
  (``int_coord("iteration")`` rides in as the rollout's replica);
- the ``steps`` checkpoint grid (string ``step`` axis) — every
  improver LLM call is one durable step (``propose_0`` /
  ``propose_1`` / ``final_choice``). Steps own call + parse + retry:
  an unparseable completion is retried with a fresh sample and a
  final failure fails the *step*, so nothing malformed is ever
  checkpointed — a re-demand re-samples instead of replaying garbage.

Two improvement rounds, then a final choice (which may repeat an
earlier proposal). Output: a
:class:`behavior_research_types.UserSimImprovementResult` carrying the
final bio + notes and the whole trajectory (per-candidate bio, notes,
transcript, judgement) as in-data provenance.

Scope note: bare ``.store()`` / ``.load()`` — run-scope user-code
style, as in ``rollout_task.py``.
"""

from __future__ import annotations

import re

from behavior_research_types import (
    ImprovementEvent,
    JudgeResult,
    SubjectModelProto,
    Transcript,
    UserSimConfig,
    UserSimImprovementRequest,
    UserSimImprovementResult,
)
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr_experiment_task import model as m
from fxtr_experiment_task.task_api import StepContext, TaskContext
from fxtr_experiment_task.task_adapter import task_fn
from fxtr_experiment_task.view_runtime import step_fn
from fxtr_experiment_task.views import DataView, HandleView, int_coord

from judge_task import JUDGE_DATA_SCHEMA
from rollout_task import BEHAVIOR_STEPS_NS

_IMPROVE_SYSTEM = (
    "You are an expert designer of simulated-user personas for "
    "AI-assistant evaluations. You will be shown a persona, trial "
    "conversations it produced against a subject assistant, and how a "
    "judge scored them — plus an instruction for how the persona should "
    "be improved. Write an improved persona. Respond in exactly this "
    "format:\n\n"
    "<notes>2-4 sentences: what you changed and why</notes>\n"
    "<persona>the full new persona description</persona>"
)

_FINAL_SYSTEM = (
    "You are an expert designer of simulated-user personas for "
    "AI-assistant evaluations. You will be shown an original persona, "
    "an improvement instruction, and candidate rewrites with their "
    "trial evidence. Choose the FINAL persona — you may repeat any "
    "candidate verbatim if you were happy with it, or write a new "
    "variant. Respond in exactly this format:\n\n"
    "<notes>2-4 sentences: which candidate you chose (or what you "
    "changed) and why</notes>\n"
    "<persona>the full final persona description</persona>"
)

#: Total improver attempts per step before giving up on parseable output.
_MAX_ATTEMPTS = 3

_NOTES_RE = re.compile(r"<notes>\s*(.*?)\s*</notes>", re.DOTALL)
_PERSONA_RE = re.compile(r"<persona>\s*(.*?)\s*</persona>", re.DOTALL)


def parse_improvement(text: str) -> tuple[str, str]:
    """Parse an improver completion into ``(notes, persona)``.

    Raises:
        ValueError: If either tag is missing or empty.
    """
    notes = _NOTES_RE.search(text)
    persona = _PERSONA_RE.search(text)
    if notes is None or not notes.group(1):
        raise ValueError(f"improver reply has no <notes> tag: {text!r}")
    if persona is None or not persona.group(1):
        raise ValueError(f"improver reply has no <persona> tag: {text!r}")
    return notes.group(1), persona.group(1)


def format_trial(label: str, persona: str, transcript: Transcript, judgement: JudgeResult) -> str:
    """One trial (persona + conversation + verdict) as prompt XML."""
    messages = "\n".join(
        f'    <message role="{msg.role}">{msg.text}</message>' for msg in transcript.messages
    )
    return (
        f'<trial label="{label}">\n'
        f"  <persona>{persona}</persona>\n"
        f"  <transcript>\n{messages}\n  </transcript>\n"
        f'  <judgement score="{judgement.score:.2f}">{judgement.reasoning}</judgement>\n'
        f"</trial>"
    )


def improve_message(instruction: str, original_persona: str, trials_xml: list[str]) -> str:
    """The improver's user message: instruction + persona + evidence."""
    trials = "\n\n".join(trials_xml)
    return (
        f"<instruction>{instruction}</instruction>\n\n"
        f"<original_persona>{original_persona}</original_persona>\n\n"
        f"{trials}"
    )


@step_fn(type_id=define_type(BEHAVIOR_STEPS_NS, "improver_call"))
async def improver_call_step(
    ctx: StepContext,
    *,
    key: FxtrID,
    model: str,
    system_prompt: str,
    user_message: str,
) -> FxtrServiceData:
    """One improver LLM call → ``{"notes", "persona"}``.

    The step owns call + parse + retry (module docstring): retries up
    to ``_MAX_ATTEMPTS`` fresh samples on unparseable output, then
    raises — failing the step withholds the checkpoint entry, so a
    re-demand re-runs it rather than replaying a recorded failure.
    """
    svc = await ctx.connect(key)
    improver = await svc.replace_service_type(SubjectModelProto).resolve()
    last_error: ValueError | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        completion = await improver.reply(
            system_prompt, model, [{"role": "user", "text": user_message}]
        )
        try:
            notes, persona = parse_improvement(completion)
        except ValueError as e:
            last_error = e
            continue
        return {"notes": notes, "persona": persona}
    assert last_error is not None
    raise ValueError(f"improver reply unparseable after {_MAX_ATTEMPTS} attempts") from last_error


@task_fn
async def improve_sim_task(
    ctx: TaskContext,
    /,
    *,
    request: DataView,
    subject_services: HandleView,
    sim_service: HandleView,
    judge_services: HandleView,
    rollout_fn: HandleView,
    judge_fn: HandleView,
) -> FxtrID:
    """See the module docstring.

    Args (all views, wired by the driver's ``flow`` rule):
        request: Fully-bound data view — this cell's
            :class:`UserSimImprovementRequest` (the subtree's
            ``request_id`` axis vmap-consumed).
        subject_services: Handle view, ``provider`` kept — serves both
            the improver (data-dependent ``read_key`` at the request's
            provider) and, forwarded onward, the nested rollouts.
        sim_service: Zero-axis handle view (forwarded onward).
        judge_services: Handle view, ``provider`` kept (forwarded).
        rollout_fn: Zero-axis handle view of ``tasks/rollout`` — the
            nested trial grids run the SAME task fns as the top-level
            evidence pipeline (holding is not observing; the grant is
            what lets this flow wire them into its internals).
        judge_fn: Zero-axis handle view of ``tasks/judge``.
    """
    request_ref = EntityRef(await request.read(), UserSimImprovementRequest)
    req = await request_ref.load()
    sim_cfg = await req.sim_config.load()
    improver_key = await subject_services.read_key(provider=req.improver_provider)
    await ctx.log(
        f"improving persona over {len(req.rollouts)} source trials "
        f"(instruction: {req.instruction!r})"
    )

    # The isolated trial pipeline, in our own internals: pinned configs
    # materialized into data grids, then the SAME rollout/judge task fns
    # wired over them per iteration.
    internals = ctx.own_internals()
    iteration_axes = (m.AxisSpec("iteration", "int"),)
    subject_cfg = await internals.open_data("subject_cfg", ())
    await subject_cfg.write(req.subject_config.entity_id)
    rollout_cfg = await internals.open_data("rollout_cfg", ())
    await rollout_cfg.write(req.rollout_config.entity_id)
    judge_cfg = await internals.open_data("judge_cfg", ())
    await judge_cfg.write(req.judge_config.entity_id)
    bios = await internals.open_data("bios", iteration_axes)
    trial_rollouts = await internals.open_task(
        "rollouts",
        axes=iteration_axes,
        task_fn=rollout_fn,
        args={
            "subject": subject_cfg.read_only(),
            "sim": bios.read_only(),
            "rollout_cfg": rollout_cfg.read_only(),
            "replica": int_coord("iteration"),
            "subject_services": subject_services.read_only(),
            "sim_service": sim_service.read_only(),
        },
    )
    trial_judgements = await internals.open_task(
        "judgements",
        axes=iteration_axes,
        task_fn=judge_fn,
        args={
            "transcript": trial_rollouts.output_entity(),
            "judge_cfg": judge_cfg.read_only(),
            "judge_services": judge_services.read_only(),
        },
        # The judge fn's returned output set must match the grid's
        # declared outputs, so any grid running it declares both (the
        # per-iteration score frames just come along for the ride).
        outputs={
            "verdict": m.EntityOutput(),
            "data": m.FrameOutput(schema=JUDGE_DATA_SCHEMA),
        },
    )
    steps = await internals.open_checkpoint("steps", axes=(m.AxisSpec("step", "str"),))

    # The source evidence, formatted once (pure outer-level code over
    # the request's closure).
    trials_xml: list[str] = []
    for i, (transcript_ref, judgement_ref) in enumerate(zip(req.rollouts, req.judgements)):
        transcript = await transcript_ref.load()
        judgement = await judgement_ref.load()
        trials_xml.append(format_trial(f"original_{i}", sim_cfg.persona, transcript, judgement))

    # The chronological event log the result carries (module docstring:
    # request → per round: proposal, trial transcript, trial judgement →
    # final choice).
    events: list[ImprovementEvent] = [
        ImprovementEvent(
            kind="request",
            text=req.instruction,
            subject=request_ref.replace_target_type(UnknownEntity),
        )
    ]
    for i in range(2):
        await ctx.log(f"round {i}: asking the improver for a candidate persona")
        proposal = await steps.run_step(
            improver_call_step,
            args={
                "key": improver_key,
                "model": req.improver_model,
                "system_prompt": _IMPROVE_SYSTEM,
                "user_message": improve_message(req.instruction, sim_cfg.persona, trials_xml),
            },
            coords={"step": f"propose_{i}"},
        )
        candidate_cfg = UserSimConfig(
            task_prompt=sim_cfg.task_prompt, persona=str(proposal["persona"])
        )
        bio_ref = candidate_cfg.store()
        await bios.write(bio_ref.entity_id, iteration=i)
        await ctx.log(f"round {i}: trialing the candidate (nested rollout + judgement)")
        judgement_ref = EntityRef(
            await trial_judgements.output_entity("verdict").read(iteration=i), JudgeResult
        )
        judgement = await judgement_ref.load()
        # The judgement's own provenance ref IS the trial transcript.
        transcript = await judgement.transcript.load()
        events.extend(
            (
                ImprovementEvent(
                    kind="proposal",
                    text=str(proposal["notes"]),
                    subject=bio_ref.replace_target_type(UnknownEntity),
                ),
                ImprovementEvent(
                    kind="trial_transcript",
                    text="",
                    subject=judgement.transcript.replace_target_type(UnknownEntity),
                ),
                ImprovementEvent(
                    kind="trial_judgement",
                    text="",
                    subject=judgement_ref.replace_target_type(UnknownEntity),
                ),
            )
        )
        trials_xml.append(
            format_trial(f"candidate_{i}", candidate_cfg.persona, transcript, judgement)
        )

    await ctx.log("asking the improver for its final choice")
    final = await steps.run_step(
        improver_call_step,
        args={
            "key": improver_key,
            "model": req.improver_model,
            "system_prompt": _FINAL_SYSTEM,
            "user_message": improve_message(req.instruction, sim_cfg.persona, trials_xml),
        },
        coords={"step": "final_choice"},
    )
    final_cfg = UserSimConfig(task_prompt=sim_cfg.task_prompt, persona=str(final["persona"]))
    final_bio_ref = final_cfg.store()
    events.append(
        ImprovementEvent(
            kind="final_choice",
            text=str(final["notes"]),
            subject=final_bio_ref.replace_target_type(UnknownEntity),
        )
    )
    result = UserSimImprovementResult(
        request=request_ref,
        events=tuple(events),
        final_bio=final_bio_ref,
        final_notes=str(final["notes"]),
    )
    return result.store().entity_id
