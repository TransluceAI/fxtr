"""An example experiment: roll out model configs against scenarios and grade the transcripts.

This module shows how entity types, steps, and workflows fit together in a small but realistically
shaped experiment, and in particular how a step body asks for its arguments by *type*:

- Three arrays of entities are the inputs: model configs, scenarios, and judge rubrics. Each is an
  array of references to entities of one type (``BoundID[ModelConfig]`` and so on), one dim each.
- :func:`rollout` maps over models and scenarios, replicated ``n_samples`` times, producing one
  transcript entity per (model, scenario, sample). Its body asks for its inputs as
  ``BoundID[ModelConfig]`` and ``BoundID[Scenario]`` (bound ids, since the transcript refers back
  to them) and returns a :class:`Transcript`; the step decorator binds the inputs and stores the
  result (see :func:`fxtr.experiment.steps.step`).
- :func:`grade` maps over the transcripts and the rubrics, producing an array of :class:`Grade`
  structs.
- :func:`rollout_experiment` is the workflow tying these together and returning the grade array.

Array types are declared on the bodies rather than in the decorators, as Python types: an entity
class or ``BoundID[T]`` is a reference to that type, a ``TypedDict`` is a struct, and the one thing
a Python type never says, an array's dims, is spelled out in a string inside
:data:`~typing.Annotated` (see :mod:`fxtr.experiment.annotations`). Inside :func:`_experiment`,
where mapping and replicas build the dims up, ``expect_type`` asserts what each handle came out as.

:func:`rollout_desugared` is :func:`rollout` written against the plain calling convention, with
the loads spelled out and the signature declared in the decorator, to show what the
annotation-driven conveniences stand for; :func:`rollout_experiment_desugared` runs it in place of
:func:`rollout`.

Nothing here calls a model: the steps assemble fake transcripts and scores. The types are the
shape a real experiment would have; they are declared in this module for self-containment.
:mod:`basic_project.bio_refinement` builds on this module with child workflows and data-dependent
control flow.

``launch_rollout_experiment.py`` at the project root runs the experiment as a Postgres job and
prints its status and grade array.
"""

from __future__ import annotations

import dataclasses
import random
from typing import Annotated, Any, TypedDict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ScalarArray
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.schema import id_of
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import Step, StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

# ---- entity types ----

MODEL_CONFIG = "org.example.rollout.ModelConfig"
SCENARIO = "org.example.rollout.Scenario"
RUBRIC = "org.example.rollout.Rubric"
MESSAGE = "org.example.rollout.Message"
TRANSCRIPT = "org.example.rollout.Transcript"


@dataclasses.dataclass(frozen=True, kw_only=True)
class ModelConfig(DataclassEntity, fxtr_type=MODEL_CONFIG):
    """How to sample from a model."""

    name: str
    temperature: float


@dataclasses.dataclass(frozen=True, kw_only=True)
class Scenario(DataclassEntity, fxtr_type=SCENARIO):
    """A situation to put the model in."""

    title: str
    prompt: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Rubric(DataclassEntity, fxtr_type=RUBRIC):
    """What a judge looks for: a transcript scores by the fraction of criteria it mentions."""

    name: str
    criteria: tuple[str, ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Message(TaggedDataclass, fxtr_type=MESSAGE):
    """One turn of a transcript; inline (not separately stored)."""

    role: str
    content: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Transcript(DataclassEntity, fxtr_type=TRANSCRIPT):
    """A rollout: the config and scenario it came from, by reference, and the turns."""

    model: BoundID[ModelConfig]
    scenario: BoundID[Scenario]
    messages: tuple[Message, ...]

    def text(self) -> str:
        return " ".join(m.content for m in self.messages)


class Grade(TypedDict):
    """A judge's verdict on one transcript: the value type of the grade array."""

    score: float
    rationale: str


# ---- steps ----

_REPLIES = ["Certainly.", "Let me think.", "Here is what I would do.", "I would rather not."]


# Named explicitly, since this module also runs as a script (as __main__, its functions would
# otherwise have no stable qualified name; see fxtr.experiment.registry).
@step(name="rollout")
async def rollout(
    context: StepContext, model: BoundID[ModelConfig], scenario: BoundID[Scenario]
) -> Transcript:
    """Sample one (fake) transcript of ``model`` in ``scenario``.

    The transcript refers back to the config and scenario it came from, so the body asks for them
    as bound ids (the step decorator binds the incoming references to their types) and loads them
    itself; it returns an entity, which the decorator stores so the step's value is its id.
    Compare :func:`rollout_desugared`. A step that only needed the values would ask for
    ``ModelConfig`` and ``Scenario`` directly and receive them loaded.

    Every one of those annotations names an entity type, which is enough to say the argument is a
    reference to it, so ``@step`` declares no signature of its own.
    """
    model_config = await context.load(model)
    scenario_entity = await context.load(scenario)
    reply = random.choice(_REPLIES)  # the nondeterministic part a real rollout would have
    return Transcript(
        model=model,
        scenario=scenario,
        messages=(
            Message(role="user", content=scenario_entity.prompt),
            Message(
                role="assistant",
                content=f"{reply} ({model_config.name} at temperature {model_config.temperature})",
            ),
        ),
    )


@step(
    name="rollout_desugared",
    inputs={"model": id_of(MODEL_CONFIG), "scenario": id_of(SCENARIO)},
    output=id_of(TRANSCRIPT),
)
async def rollout_desugared(
    context: StepContext,
    model: ScalarArray[EntityID],
    scenario: ScalarArray[EntityID],
) -> ScalarArray[BoundID[Transcript]]:
    """:func:`rollout` with every convenience spelled out. The arguments arrive as the scalar
    arrays the runner actually passes (not unwrapped to values), so the body reads their single
    ids, binds and loads them, stores its result, and returns a scalar array itself. Same
    signature, same behaviour.

    This one declares its signature in the decorator, the other way of giving a step its types,
    with :func:`~fxtr.entity_defns.schema.id_of` on the type ids; the body's array annotations
    are then only checked against it. ``ScalarArray[X]`` is just ``Annotated[Array[X], "[]"]``."""
    model_id, scenario_id = model.item(), scenario.item()
    model_config = await context.load(model_id, as_type=ModelConfig)
    scenario_entity = await context.load(scenario_id, as_type=Scenario)
    reply = random.choice(_REPLIES)
    transcript = Transcript(
        model=model_id.bind_type(ModelConfig),
        scenario=scenario_id.bind_type(Scenario),
        messages=(
            Message(role="user", content=scenario_entity.prompt),
            Message(
                role="assistant",
                content=f"{reply} ({model_config.name} at temperature {model_config.temperature})",
            ),
        ),
    )
    return Array.scalar(await context.store(transcript), id_of(Transcript))


@step(name="grade")
async def grade(context: StepContext, transcript: Transcript, rubric: Rubric) -> Grade:
    """Score a transcript against a rubric: the fraction of criteria mentioned in it.

    The result is a struct, declared by the :class:`Grade` TypedDict.
    """
    text = transcript.text().lower()
    hits = [c for c in rubric.criteria if c.lower() in text]
    return {
        "score": len(hits) / len(rubric.criteria),
        "rationale": f"{rubric.name}: mentions {hits or 'none'} of {list(rubric.criteria)}",
    }


# ---- the workflow ----


async def entity_array[T: DataclassEntity](
    context: WorkflowContext, name: str, dim: str, entities: dict[str, T]
) -> ArrayHandle[BoundID[T]]:
    """Store ``entities`` and register the one-dim array of references to them under ``name``.

    The array's type is inferred from its rows: the dim's kind from the keys and the value schema
    from the bound ids, whose loaders name the entity type.
    """
    rows = [((key,), await context.store(entity)) for key, entity in entities.items()]
    return context.add_source_array(name, Array.infer_from_items(rows, dims=[dim]))


async def _experiment(
    context: WorkflowContext,
    n_samples: ArrayHandle[int],
    rollout_step: Step[Any, Any, BoundID[Transcript]],
) -> ArrayHandle[Grade]:
    models = await entity_array(
        context,
        "models",
        "model",
        {
            "small": ModelConfig(name="small-1", temperature=0.7),
            "large": ModelConfig(name="large-1", temperature=0.2),
        },
    )
    scenarios = await entity_array(
        context,
        "scenarios",
        "scenario",
        {
            "greeting": Scenario(title="Greeting", prompt="Say hello politely."),
            "refusal": Scenario(title="Refusal", prompt="Decline an unreasonable request."),
        },
    )
    rubrics = await entity_array(
        context,
        "rubrics",
        "rubric",
        {
            "politeness": Rubric(name="politeness", criteria=("certainly", "rather")),
            "brevity": Rubric(name="brevity", criteria=("here is",)),
        },
    )
    # One transcript per (model, scenario), replicated n_samples times: the replica dim `sample`
    # repeats the run under distinct cache keys without the step seeing it.
    transcripts = context.run_step(
        "rollout",
        rollout_step,
        {"model": models, "scenario": scenarios},
        map_over=["model", "scenario"],
        replicas={"sample": range(await n_samples.item())},
    ).expect_type(("[model: str, scenario: str, sample: int]", BoundID[Transcript]))
    # Every transcript against every rubric.
    return context.run_step(
        "grade",
        grade,
        {"transcript": transcripts, "rubric": rubrics},
        map_over=["model", "scenario", "sample", "rubric"],
    ).expect_type(("[model: str, scenario: str, sample: int, rubric: str]", Grade))


@workflow(name="rollout_experiment")
async def rollout_experiment(
    context: WorkflowContext, n_samples: ScalarHandle[int]
) -> Annotated[ArrayHandle[Grade], "[model: str, scenario: str, sample: int, rubric: str]"]:
    """Roll out every model config against every scenario ``n_samples`` times and grade each
    transcript against every rubric, returning the array of grades.

    The handles' type parameters give the value schemas; their dims, which no Python type
    implies, are the strings in ``Annotated``. That is the whole signature, and ``@workflow``
    has nothing left to declare.
    """
    return await _experiment(context, n_samples, rollout)


@workflow(name="rollout_experiment_desugared")
async def rollout_experiment_desugared(
    context: WorkflowContext, n_samples: ScalarHandle[int]
) -> Annotated[ArrayHandle[Grade], "[model: str, scenario: str, sample: int, rubric: str]"]:
    """:func:`rollout_experiment` with :func:`rollout_desugared` in place of :func:`rollout`."""
    return await _experiment(context, n_samples, rollout_desugared)
