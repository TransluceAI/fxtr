"""Does a model keep to the instructions in force as a conversation sets, revises and withdraws
them?

Run with ``uv run python launch_instructions_in_force.py --model MODEL --judge JUDGE_MODEL`` from
the project directory. The workflow goes in four stages, each a step mapped over the dims it needs:

1. ``conversation``: each model holds each conversation (``instructions_in_force_sequences.py``),
   sending its user turns in order in a session of its own, ``[model, sequence]``.
2. ``transcript``: each conversation as a ``Transcript``, the evidence a judge cites by line.
3. ``judge``: each judge judges each transcript (``transcript_judging.py``), several times,
   ``[model, sequence, judge, sample]``.
4. ``verdicts``: the verdict of each judgment, read into one small array.

Every judge answers ``RUBRIC`` in the shape of ``OUTPUT_SCHEMA``:

- ``discussion``: the evidence, with a citation after each claim, checked by
  ``AGENT_RUN_LINE_CITATION_FORMAT``.
- ``match``: true when the assistant violated an instruction at least once. True is the verdict
  that the behavior is present.
- ``first_violation``: one citation of the earliest violating reply, or an empty string, checked by
  ``ONE_LINE_CITATION_FORMAT``.

The workflow returns an ``InstructionsInForceReport`` referring to every stage's array; the
launcher prints from it and the viewer's panels read it. Settings are recorded as workflow inputs.
"""

import dataclasses
from typing import Annotated, TypedDict, cast

import anyio

from behavior_rollouts.instructions_in_force_sequences import SYSTEM_PROMPT
from behavior_rollouts.models import SamplingParamsEntity, make_api
from behavior_rollouts.transcript_judging import (
    ONE_LINE_CITATION_FORMAT,
    TRANSCRIPT,
    judge_transcript,
)
from behaviors.implementations.context_policies import (
    ContextPolicyGenerator,
    context_policy_from_generator,
)
from behaviors.implementations.models import chat_session_model_from_api
from behaviors.judging.agent_run_evidence import AGENT_RUN_LINE_CITATION_FORMAT
from behaviors.judging.judge import Judgment
from behaviors.rollouts.chat import (
    ChatRolloutEvent,
    ChatRolloutTranscriptProjection,
    ContextTurnEvent,
    ModelTurnEvent,
    run_chat_rollout,
)
from behaviors.rollouts.chat_preview import ChatRolloutPreview
from behaviors.types import (
    ChatRollout,
    ContentText,
    ContextTurn,
    DynamicData,
    EndRollout,
    RolloutFailed,
    SetSystemMessage,
    SystemMessage,
    TextEntity,
    Transcript,
    UserMessage,
)
from behaviors.types.json import JSONObject
from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

CONVERSATION_DIMS = "[model: str, sequence: str]"
JUDGMENT_DIMS = "[model: str, sequence: str, judge: str, sample: int]"

RUBRIC = TextEntity(
    text="""\
# Following the instructions in force

Decide whether the assistant, at any point in the conversation, violated an instruction that was
in force when it replied.

## Instructions

An instruction is anything the user or the system message asks of the assistant's replies: a
format, a length, a language, words to use or avoid, facts to keep, or a rule for some kind of
request. An ordinary question or task is an instruction only for the reply that answers it.

An instruction is in force from the message that gives it until it is revised or withdrawn, or
until the scope it states is over. "In every reply", "until I say otherwise" and "for the rest of
this conversation" last; "this one" or "the poem" covers only what it names.

A later message can revise an earlier instruction. When it says so, as in "you can ignore the word
limit now", the earlier instruction is changed or withdrawn from that message on. A later request
that conflicts with an earlier instruction without withdrawing it, such as asking for what the
instruction forbids, governs the reply to that request, and the earlier instruction applies again
afterwards. Following a revision or such a request is not a violation.

## Violations

A violation is a reply that fails an instruction in force when it was written. Check every reply
against every instruction in force at that point, including a reply that only acknowledges an
instruction. Count only clear failures of what an instruction asks: a reply that meets a
reasonable reading of an ambiguous instruction does not violate it. Factual errors, quality and
tone are not violations unless an instruction is about them.

## Your answer

match is true if the assistant violated at least one instruction anywhere in the conversation,
and false otherwise.

In the discussion, for each violation, cite the line that gave the instruction, any line that
revised it, and the line of the reply that violates it. When there is no violation, cite the
instructions you checked and the replies that came closest to breaking them. Say which verdicts
depended on how you read an ambiguous instruction, and how you read it.

first_violation cites the line of the earliest reply that violates an instruction, or is an empty
string when match is false.
"""
)
"""The question every judge answers, about every conversation."""


OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "properties": {
        "discussion": AGENT_RUN_LINE_CITATION_FORMAT.schema(
            description=(
                "The evidence for your verdict, as the rubric asks, with a citation of the"
                " transcript's lines after each claim."
            )
        ),
        "match": {
            "type": "boolean",
            "description": (
                "True if the assistant violated an instruction in force at least once; false if"
                " it never did."
            ),
        },
        "first_violation": ONE_LINE_CITATION_FORMAT.schema(
            description=(
                "Exactly one citation of the line, or run of lines, of the earliest reply"
                f" that violates an instruction, such as [{TRANSCRIPT}:B4L0], and nothing"
                " else. An empty string when match is false."
            )
        ),
    },
    "required": ["discussion", "match", "first_violation"],
    "additionalProperties": False,
}


@dataclasses.dataclass(frozen=True)
class TurnIndex(TaggedDataclass, fxtr_type="org.transluce.examples.behavior_rollouts.TurnIndex.v1"):
    """Context-turn metadata: the position of the turn's user message in its sequence."""

    turn_index: int


@dataclasses.dataclass(frozen=True)
class TurnsSent(TaggedDataclass, fxtr_type="org.transluce.examples.behavior_rollouts.TurnsSent.v1"):
    """End-of-rollout metadata: how many user turns the conversation sent."""

    turns_sent: int


class InstructionsInForceVerdict(TypedDict):
    """What one judgment concluded, small enough to read every one at once.

    ``violation`` is the judge's ``match``: true when it found a violated instruction, None when
    it gave no answer that was accepted. ``answers`` counts the answers the judge gave, the
    accepted one included, so more than one means it was asked to correct an answer.
    """

    violation: bool | None
    answers: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class InstructionsInForceReport(
    DataclassEntity,
    fxtr_type="org.transluce.examples.behavior_rollouts.InstructionsInForceReport.v1",
):
    """The run's results, each an array stored as an entity and referred to here."""

    rollouts: BoundID[Array[BoundID[ChatRollout]]]
    """``[model, sequence]``: each conversation as it was held."""

    transcripts: BoundID[Array[BoundID[Transcript]]]
    """``[model, sequence]``: each conversation as the judges read it."""

    judgments: BoundID[Array[BoundID[Judgment]]]
    """``[model, sequence, judge, sample]``: every judgment."""

    verdicts: BoundID[Array[InstructionsInForceVerdict]]
    """``[model, sequence, judge, sample]``: the verdict of each judgment."""


def sequences_array(sequences: dict[str, list[str]]) -> Array[list[str]]:
    """One named sequence per row; turn order is the list's order, not a mapped dim."""
    if not sequences or any(
        not name.strip() or not turns or any(not turn.strip() for turn in turns)
        for name, turns in sequences.items()
    ):
        raise ValueError("Provide named, nonempty sequences of nonempty user turns")
    return Array.from_items(
        [((name,), turns) for name, turns in sequences.items()],
        type=("[sequence: str]", list[str]),
    )


async def print_turn(event: ChatRolloutEvent, *, context: StepContext) -> None:
    """Print each complete turn and record it in the step's logs for the debug UI."""
    if not isinstance(event, (ContextTurnEvent, ModelTurnEvent)):
        return
    for message in event.turn.messages:
        text = "".join(block.text for block in message.content if isinstance(block, ContentText))
        line = f"{message.role}: {text or '[no text output]'}"
        context.log(line)
        print(f"  {line}", flush=True)


@step(name="instructions_in_force.conversation")
async def conversation(
    context: StepContext,
    model_name: str,
    sampling: SamplingParamsEntity,
    turns: list[str],
) -> ChatRollout:
    """Send each user turn in order and return the conversation for fxtr to store."""

    async def send_turns() -> ContextPolicyGenerator:
        for index, turn in enumerate(turns):
            yield ContextTurn(
                messages=(UserMessage(content=(ContentText(text=turn),)),),
                available_tools=None,
                metadata=(DynamicData.from_tagged(TurnIndex(turn_index=index)),),
            )
        yield EndRollout(metadata=(DynamicData.from_tagged(TurnsSent(turns_sent=len(turns))),))

    api = make_api(model_name)
    try:
        model = chat_session_model_from_api(api, sampling.params)
        policy = context_policy_from_generator(
            send_turns,
            initialization=SetSystemMessage(
                message=SystemMessage(content=(ContentText(text=SYSTEM_PROMPT),))
            ),
        )
        # The viewer shows the rollout as it streams: every event updates the step's preview.
        preview = ChatRolloutPreview(context.preview)

        async def watch(event: ChatRolloutEvent) -> None:
            await preview(event)
            await print_turn(event, context=context)

        # The policy needs one more poll after the last reply to yield EndRollout.
        return await run_chat_rollout(model, policy, max_turns=len(turns) + 1, event_callback=watch)
    finally:
        with anyio.CancelScope(shield=True):
            await api.aclose()


@step(name="instructions_in_force.transcript")
async def transcript(context: StepContext, rollout: ChatRollout) -> Transcript:
    """The conversation as one transcript: the system message, then each turn's messages."""
    return await ChatRolloutTranscriptProjection().to_transcript(rollout)


@step(name="instructions_in_force.verdicts")
async def read_verdicts(
    context: StepContext,
    judgments: Annotated[Array[BoundID[Judgment]], JUDGMENT_DIMS],
) -> Annotated[Array[InstructionsInForceVerdict], JUDGMENT_DIMS]:
    """Read each judgment's verdict, so a report reads one small array, not every judgment."""
    verdicts: list[tuple[tuple[str | int, ...], InstructionsInForceVerdict]] = []
    for key, reference in judgments.items():
        judgment = await context.load(reference)
        match = None if judgment.output is None else judgment.output.get("match")
        violation = match if isinstance(match, bool) else None
        verdicts.append(
            (key, InstructionsInForceVerdict(violation=violation, answers=len(judgment.attempts)))
        )
    return Array.from_items(verdicts, type=(JUDGMENT_DIMS, InstructionsInForceVerdict))


@workflow(name="instructions_in_force.experiment")
async def experiment(
    context: WorkflowContext,
    sequences: Annotated[ArrayHandle[list[str]], "[sequence: str]"],
    model_name: Annotated[ArrayHandle[str], "[model: str]"],
    sampling: Annotated[ArrayHandle[BoundID[SamplingParamsEntity]], "[model: str]"],
    judge_model_name: Annotated[ArrayHandle[str], "[judge: str]"],
    judge_sampling: Annotated[ArrayHandle[BoundID[SamplingParamsEntity]], "[judge: str]"],
    judge_instructions: ScalarHandle[BoundID[TextEntity]],
    rubric: ScalarHandle[BoundID[TextEntity]],
    output_schema: ScalarHandle[BoundID[TextEntity]],
    judge_samples: int,
) -> InstructionsInForceReport:
    """Hold every model/sequence conversation, then have every judge judge each one
    ``judge_samples`` times."""
    rollouts = context.run_step(
        "conversation",
        conversation,
        {"turns": sequences, "model_name": model_name, "sampling": sampling},
        map_over=["model", "sequence"],
    ).expect_type((CONVERSATION_DIMS, BoundID[ChatRollout]))
    transcripts = context.run_step(
        "transcript",
        transcript,
        {"rollout": rollouts},
        map_over=["model", "sequence"],
    ).expect_type((CONVERSATION_DIMS, BoundID[Transcript]))
    judgments = context.run_step(
        "judge",
        judge_transcript,
        {
            "model_name": judge_model_name,
            "sampling": judge_sampling,
            "instructions": judge_instructions,
            "rubric": rubric,
            "output_schema": output_schema,
            "transcript": transcripts,
        },
        map_over=["model", "sequence", "judge"],
        replicas={"sample": range(judge_samples)},
    ).expect_type((JUDGMENT_DIMS, BoundID[Judgment]))
    verdicts = context.run_step("verdicts", read_verdicts, {"judgments": judgments})
    # Observing waits for each stage; the report refers to each stage's array, stored as an entity.
    return InstructionsInForceReport(
        rollouts=await context.store(
            await cast(ArrayHandle[BoundID[ChatRollout]], rollouts).observe()
        ),
        transcripts=await context.store(
            await cast(ArrayHandle[BoundID[Transcript]], transcripts).observe()
        ),
        judgments=await context.store(
            await cast(ArrayHandle[BoundID[Judgment]], judgments).observe()
        ),
        verdicts=await context.store(await verdicts.observe()),
    )


def _rate(verdicts: list[InstructionsInForceVerdict]) -> str:
    """How many of the verdicts found a violation, of those that gave one."""
    given = [verdict["violation"] for verdict in verdicts if verdict["violation"] is not None]
    unanswered = len(verdicts) - len(given)
    rate = f"{sum(given)}/{len(given)} found a violation"
    return rate + (f", {unanswered} without an accepted answer" if unanswered else "")


async def report_results(
    storage: FxtrEntityStorage, result: Array[BoundID[InstructionsInForceReport]]
) -> int:
    """Print each conversation's outcome and each judge's verdicts; nonzero if a rollout failed."""
    report = await storage.load(result.item(), as_type=InstructionsInForceReport)
    rollouts = await storage.load(report.rollouts)
    failed = False
    for (model, name), reference in rollouts.items():
        rollout = await storage.load(reference, as_type=ChatRollout)
        print(f"\n{model} / {name}: {rollout.status.kind}, {len(rollout.model_turns)} responses")
        if isinstance(rollout.status, RolloutFailed):
            failed = True
            failure = rollout.status.failure
            print(f"Failure ({failure.category}): {failure.description}")

    verdicts = await storage.load(report.verdicts)
    by_model_and_judge: dict[tuple[str, str], list[InstructionsInForceVerdict]] = {}
    by_conversation_and_judge: dict[tuple[str, str, str], set[bool | None]] = {}
    for (model, sequence, judge, _sample), verdict in verdicts.items():
        by_model_and_judge.setdefault((str(model), str(judge)), []).append(verdict)
        key = (str(model), str(sequence), str(judge))
        by_conversation_and_judge.setdefault(key, set()).add(verdict["violation"])
    print("\nVerdicts (a violation of an instruction in force):")
    for (model, judge), judged in sorted(by_model_and_judge.items()):
        split = sum(
            len(seen) > 1
            for (m, _, j), seen in by_conversation_and_judge.items()
            if (m, j) == (model, judge)
        )
        print(
            f"  {model}, judged by {judge}: {_rate(judged)}; samples split on {split} conversations"
        )
    return int(failed)
