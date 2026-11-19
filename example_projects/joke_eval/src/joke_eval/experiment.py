"""A joke evaluation: models write jokes about topics, and a judge model grades each joke.

This module defines the experiment's entity types, its steps, and the workflow that connects them.
It is listed in `[tool.fxtr] modules` in pyproject.toml, so fxtr imports it to find the steps and
the workflow when it resumes a job or shows one in the viewer.
"""

import json
from dataclasses import dataclass
from typing import Annotated, TypedDict, cast

import anyio

from behaviors.implementations.context_policies import (
    ScriptedUserSimulator,
    context_policy_from_user_simulator,
)
from behaviors.implementations.models import chat_session_model_from_api
from behaviors.implementations.models.anthropic import AnthropicMessagesAPI
from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI
from behaviors.judging.defaults import DEFAULT_ENTITY_TO_PROMPT_TEXT
from behaviors.judging.judge import Judgment, judge
from behaviors.rollouts.chat import ChatRolloutTranscriptProjection, run_chat_rollout
from behaviors.types import (
    ChatRollout,
    ContentText,
    RetryConfig,
    RolloutCompleted,
    SamplingParams,
    SystemMessage,
    TextEntity,
    Transcript,
    UserMessage,
)
from behaviors.types.json import JSONObject
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

# --- Entities ------------------------------------------------------------------------------------
#
# An entity is a record that fxtr stores in its database and can show in the viewer. A frozen
# dataclass becomes one by subclassing DataclassEntity, with a type ID (`fxtr_type`) that names it
# uniquely. A `BoundID[T]` field is a reference to another stored entity of type T.


@dataclass(frozen=True)
class SamplingParamsEntity(DataclassEntity, fxtr_type="local.joke_eval.SamplingParamsEntity.v1"):
    """Sampling settings, stored so a step can take them as an input. An array can hold a
    reference to an entity, but not a `SamplingParams` itself."""

    params: SamplingParams


class ModelSummary(TypedDict):
    """How one model's jokes were graded. A TypedDict isn't an entity: it's stored inline, as a
    value in an array."""

    jokes: int
    graded: int
    mean_score: float | None  # None when no joke was graded


@dataclass(frozen=True)
class JokeReport(DataclassEntity, fxtr_type="local.joke_eval.JokeReport.v1"):
    """The evaluation's results: the jokes, their judgments, and each model's summary. Each field
    references a stored array; the comments give each array's dimensions."""

    jokes: BoundID[Array[BoundID[ChatRollout]]]  # [model, topic, sample]
    judgments: BoundID[Array[BoundID[Judgment]]]  # [model, topic, sample]
    summary: BoundID[Array[ModelSummary]]  # [model]


# --- Model calls ---------------------------------------------------------------------------------


def make_api(model_name: str) -> AnthropicMessagesAPI | OpenAIResponsesAPI:
    """A client for a model named `anthropic/MODEL` or `openai/MODEL`. Both steps build their
    clients here, so a test can replace this one function with a fake model."""
    provider, _, model_id = model_name.partition("/")
    if provider == "anthropic":
        return AnthropicMessagesAPI(model_id)
    if provider == "openai":
        return OpenAIResponsesAPI(model_id)
    raise ValueError(f"Name a model as anthropic/MODEL or openai/MODEL, not {model_name!r}")


# The messages the judge is sent. `judge` fills in each {placeholder}: the judge's instructions,
# the transcript and the rubric under their names, and the output schema it must answer in.
JUDGE_TEMPLATE = (
    SystemMessage(content=(ContentText(text="{instructions}"),)),
    UserMessage(content=(ContentText(text="{transcript}\n\n{rubric}\n\n{output_schema}"),)),
)


# --- Steps ---------------------------------------------------------------------------------------
#
# A step is an async function that does one unit of work. `@step` registers it under a name. fxtr
# caches each call's result, and reuses it when the same step is called with the same inputs.
# An argument annotated with an entity type, such as `sampling: SamplingParamsEntity`, receives the
# loaded entity; the `context` gives access to fxtr, such as storing and loading entities.


@step(name="joke_eval.tell_joke")
async def tell_joke(
    context: StepContext,
    model_name: str,
    sampling: SamplingParamsEntity,
    prompt_template: str,
    topic: str,
) -> ChatRollout:
    """Ask a model for a joke about the topic."""
    prompt = prompt_template.format(topic=topic)
    api = make_api(model_name)
    try:
        # The model to talk to, which retries rate limits and server errors.
        model = chat_session_model_from_api(api, sampling.params, retry=RetryConfig())
        # The user's side of the conversation: one scripted message.
        user = ScriptedUserSimulator(messages=(UserMessage(content=(ContentText(text=prompt),)),))
        policy = context_policy_from_user_simulator(user, initialization=None)
        # Run the conversation. One more turn than there are scripted messages, so the rollout
        # can end as completed. The returned ChatRollout is an entity, so fxtr stores it.
        return await run_chat_rollout(model, policy, max_turns=2)
    finally:
        with anyio.CancelScope(shield=True):
            await api.aclose()


@step(name="joke_eval.transcript")
async def transcript(context: StepContext, rollout: ChatRollout) -> Transcript:
    """The conversation as a transcript, the form a judge reads."""
    if not isinstance(rollout.status, RolloutCompleted):
        # Raise rather than return: there is no joke to grade, and the job stops here so you can
        # see why the conversation didn't complete.
        raise ValueError(f"the conversation did not complete: {rollout.status.kind}")
    return await ChatRolloutTranscriptProjection().to_transcript(rollout)


@step(name="joke_eval.grade_joke")
async def grade_joke(
    context: StepContext,
    model_name: str,
    sampling: SamplingParamsEntity,
    instructions: TextEntity,
    rubric: TextEntity,
    output_schema: TextEntity,
    transcript: Transcript,
) -> Judgment:
    """Grade the joke in the transcript, from 1 to 10."""
    api = make_api(model_name)
    try:
        # `judge` sends the template filled in, checks the answer against the output schema, and
        # asks again when it doesn't fit. A Judgment records the answer, the entities the judge
        # was given, and every request and response.
        judgment = await judge(
            api,
            context_template=JUDGE_TEMPLATE,
            entities={"instructions": instructions, "rubric": rubric, "transcript": transcript},
            entity_to_prompt_text=DEFAULT_ENTITY_TO_PROMPT_TEXT,
            output_schema=cast(JSONObject, json.loads(output_schema.text)),
            sampling=sampling.params,
        )
    finally:
        with anyio.CancelScope(shield=True):
            await api.aclose()
    # Log lines show on the step's card in the viewer.
    context.log(f"score: {None if judgment.output is None else judgment.output['score']}")
    return judgment


@step(name="joke_eval.summarize")
async def summarize(
    context: StepContext,
    # An array, and the dimensions each call receives: all of one model's judgments. Its values
    # are references to the judgments, which the step loads itself.
    judgments: Annotated[Array[BoundID[Judgment]], "[topic: str, sample: int]"],
) -> ModelSummary:
    """Average one model's scores."""
    scores: list[float] = []
    for ref in judgments.values():
        judgment = await context.load(ref)
        # A judgment's output is None when the judge never gave an answer that fit the schema.
        if judgment.output is not None:
            scores.append(float(cast(float, judgment.output["score"])))
    return ModelSummary(
        jokes=len(judgments),
        graded=len(scores),
        mean_score=sum(scores) / len(scores) if scores else None,
    )


# --- Workflow ------------------------------------------------------------------------------------
#
# The workflow connects the steps. It doesn't call them directly: `context.run_step` schedules a
# step and returns a handle to its future results, which can be passed on to later steps. fxtr
# runs each step's calls in parallel, as soon as their inputs are ready.


@workflow(name="joke_eval.evaluate")
async def evaluate(
    context: WorkflowContext,
    # The inputs the launcher supplies, with their dimensions. Each model is a name and the
    # `[model]` dimension; everything else has no dimensions, so every call gets the same one.
    model_name: Annotated[ArrayHandle[str], "[model: str]"],
    sampling: ScalarHandle[BoundID[SamplingParamsEntity]],
    prompt_template: ScalarHandle[str],
    topics: Annotated[ArrayHandle[str], "[topic: str]"],
    judge_model_name: ScalarHandle[str],
    judge_sampling: ScalarHandle[BoundID[SamplingParamsEntity]],
    judge_instructions: ScalarHandle[BoundID[TextEntity]],
    rubric: ScalarHandle[BoundID[TextEntity]],
    output_schema: ScalarHandle[BoundID[TextEntity]],
    samples: int,
) -> JokeReport:
    """Have each model write jokes about each topic, grade them, and summarize each model."""
    # One call per model and topic, repeated `samples` times along a new `sample` dimension.
    # The result is indexed by [model, topic, sample].
    jokes = context.run_step(
        "tell_jokes",
        tell_joke,
        {
            "model_name": model_name,
            "sampling": sampling,
            "prompt_template": prompt_template,
            "topic": topics,
        },
        map_over=["model", "topic"],
        replicas={"sample": range(samples)},
    )
    # One call per joke: its conversation as a transcript.
    transcripts = context.run_step(
        "transcripts", transcript, {"rollout": jokes}, map_over=["model", "topic", "sample"]
    )
    # One call per joke. The judge's settings go to every call, and each call gets its transcript.
    judgments = context.run_step(
        "grade_jokes",
        grade_joke,
        {
            "model_name": judge_model_name,
            "sampling": judge_sampling,
            "instructions": judge_instructions,
            "rubric": rubric,
            "output_schema": output_schema,
            "transcript": transcripts,
        },
        map_over=["model", "topic", "sample"],
    )
    # One call per model, which receives that model's judgments over [topic, sample].
    summary = context.run_step("summarize", summarize, {"judgments": judgments}, map_over=["model"])
    # Wait for the results (`observe`), store each array, and return a report that references
    # them.
    return JokeReport(
        jokes=await context.store(await jokes.observe()),
        judgments=await context.store(await judgments.observe()),
        summary=await context.store(await summary.observe()),
    )
