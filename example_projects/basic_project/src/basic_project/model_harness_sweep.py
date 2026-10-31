"""A model × harness × task experiment with ragged, model-specific context.

The root records models [m], harnesses [h], tasks [t], and stuff [s, m]. Two child
workflows compare the full Cartesian sweep (12 calls) with a sparse selection of
cross-provider model/harness pairs (6 calls). Each run_task receives one model,
harness, and task, but the model's WHOLE [s] collection: s is a core dimension,
not another axis of the sweep. The root stacks both results along selection.

Everything is local mock data: provider labels illustrate alignment and selection;
no model API is called. Answers come from the tasks' canned responses, and the
harness controls their presentation. Stored transcripts retain references to the
exact model, harness, task, and stuff used by a call.

``launch_model_harness_sweep.py`` at the project root runs it as a job on the project's database,
to inspect the child workflows and result arrays in the frontend.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Annotated, TypedDict

from basic_project.rollout_experiment import Message, entity_array
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow


@dataclasses.dataclass(frozen=True, kw_only=True)
class Model(DataclassEntity, fxtr_type="org.example.sweep.Model"):
    """A mock subject model configuration."""

    name: str
    provider: str
    temperature: float


@dataclasses.dataclass(frozen=True, kw_only=True)
class Harness(DataclassEntity, fxtr_type="org.example.sweep.Harness"):
    """The instructions and output format used around a subject model."""

    name: str
    provider: str
    system_prompt: str
    response_format: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Task(DataclassEntity, fxtr_type="org.example.sweep.Task"):
    """A prompt and canned response for a local demonstration."""

    title: str
    prompt: str
    mock_answer: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Stuff(DataclassEntity, fxtr_type="org.example.sweep.Stuff"):
    """One model-specific context item, with a reference to the model it belongs to."""

    model: BoundID[Model]
    label: str
    kind: str
    content: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Transcript(DataclassEntity, fxtr_type="org.example.sweep.Transcript"):
    """One mock conversation and the complete provenance of its context."""

    model: BoundID[Model]
    harness: BoundID[Harness]
    task: BoundID[Task]
    stuff: tuple[BoundID[Stuff], ...]
    stuff_keys: tuple[int, ...]
    messages: tuple[Message, ...]


class Pair(TypedDict):
    """A permitted model/harness pair, indexed by both m and h."""

    model: BoundID[Model]
    harness: BoundID[Harness]


class TaskResult(TypedDict):
    """Inspectable outputs: a conversation reference plus the context slice actually used."""

    transcript: BoundID[Transcript]
    answer: str
    model_provider: str
    harness_provider: str
    stuff_count: int
    stuff_keys: list[int]
    stuff_labels: list[str]


@step(name="run_task")
async def run_task(
    context: StepContext,
    model: Model,
    harness: Harness,
    task: Task,
    stuff: Annotated[Array[BoundID[Stuff]], "[s: int]"],
) -> TaskResult:
    """Run one mock task with the model's entire [s] collection of context.

    Mapping removes m, h, and t; it leaves s inside this argument. For the
    Anthropic-labelled model this is {0: A_tools, 2: A_examples}; for the
    OpenAI-labelled model it is {1: O_tools}. The same slice broadcasts across
    every harness and task. The harness formats a canned answer as JSON or Markdown.
    """
    model_ref = await context.store(model)
    items = [await context.load(ref) for ref in stuff.values()]
    if any(item.model != model_ref for item in items):
        raise ValueError("Received context belonging to a different model")
    keys = [int(key[0]) for key in stuff.keys()]
    labels = [item.label for item in items]
    context_text = "\n\n".join(f"{item.label} ({item.kind}): {item.content}" for item in items)
    if harness.response_format == "json":
        answer = json.dumps({"answer": task.mock_answer, "context": labels}, indent=2)
    else:
        answer = f"## {task.title}\n\n{task.mock_answer}\n\nContext: {', '.join(labels)}."
    transcript = await context.store(
        Transcript(
            model=model_ref,
            harness=await context.store(harness),
            task=await context.store(task),
            stuff=tuple(stuff.values()),
            stuff_keys=tuple(keys),
            messages=(
                Message(role="system", content=f"{harness.system_prompt}\n\n{context_text}"),
                Message(role="user", content=task.prompt),
                Message(role="assistant", content=answer),
            ),
        )
    )
    context.log(f"{model.name} / {harness.name} / {task.title}: s={keys}, context={labels}")
    return {
        "transcript": transcript,
        "answer": answer,
        "model_provider": model.provider,
        "harness_provider": harness.provider,
        "stuff_count": len(stuff),
        "stuff_keys": keys,
        "stuff_labels": labels,
    }


@workflow(name="full_grid")
async def full_grid(
    context: WorkflowContext,
    models: Annotated[ArrayHandle[BoundID[Model]], "[m: str]"],
    harnesses: Annotated[ArrayHandle[BoundID[Harness]], "[h: str]"],
    tasks: Annotated[ArrayHandle[BoundID[Task]], "[t: str]"],
    stuff: Annotated[ArrayHandle[BoundID[Stuff]], "[s: int, m: str]"],
) -> Annotated[ArrayHandle[TaskResult], "[m: str, h: str, t: str]"]:
    """Cross 2 models, 2 harnesses, and 3 tasks: 12 calls, each with one whole [s] slice.

    Models and stuff align on m. Harnesses and tasks introduce independent h and t
    dimensions, across which the context slice broadcasts. No s coordinate appears
    in the result keys: the context items do not multiply the number of calls.
    """
    return context.run_step(
        "run_tasks",
        run_task,
        {"model": models, "harness": harnesses, "task": tasks, "stuff": stuff},
        map_over=["m", "h", "t"],
    )


@workflow(name="cross_provider")
async def cross_provider(
    context: WorkflowContext,
    pairs: Annotated[ArrayHandle[Pair], "[m: str, h: str]"],
    tasks: Annotated[ArrayHandle[BoundID[Task]], "[t: str]"],
    stuff: Annotated[ArrayHandle[BoundID[Stuff]], "[s: int, m: str]"],
) -> Annotated[ArrayHandle[TaskResult], "[m: str, h: str, t: str]"]:
    """Cross only the 2 permitted model/harness pairs with 3 tasks: 6 calls.

    Projecting model and harness preserves the joint (m, h) keys, so mapping never
    fills in the missing same-provider pairs. The inner join also permits context
    for unselected models; each selected model still gets its whole [s] slice.
    """
    return context.run_step(
        "run_tasks",
        run_task,
        {
            "model": pairs.field("model").collect(),
            "harness": pairs.field("harness").collect(),
            "task": tasks,
            "stuff": stuff,
        },
        map_over=["m", "h", "t"],
        join="inner",
    )


@workflow(name="model_harness_experiment")
async def model_harness_experiment(
    context: WorkflowContext,
) -> Annotated[ArrayHandle[TaskResult], "[selection: str, m: str, h: str, t: str]"]:
    """Compare a full model/harness/task grid and a sparse cross-provider selection.

    Open either child workflow in the viewer to inspect its run_tasks key picker
    and outputs. The final array stacks 12 full-grid and 6 cross-provider rows;
    transcript chips link to the exact inputs and mock conversation for each row.
    """
    model_defs = {
        "anthropic-model": Model(name="Anthropic demo", provider="anthropic", temperature=0.7),
        "openai-model": Model(name="OpenAI demo", provider="openai", temperature=0.2),
    }
    harness_defs = {
        "anthropic-harness": Harness(
            name="Markdown harness",
            provider="anthropic",
            system_prompt="Answer with a heading, a clear result, and the context used.",
            response_format="markdown",
        ),
        "openai-harness": Harness(
            name="JSON harness",
            provider="openai",
            system_prompt="Return a JSON object containing answer and context.",
            response_format="json",
        ),
    }
    models = await entity_array(context, "models", "m", model_defs)
    harnesses = await entity_array(context, "harnesses", "h", harness_defs)
    tasks = await entity_array(
        context,
        "tasks",
        "t",
        {
            "task-1": Task(title="Arithmetic", prompt="What is 6 × 7?", mock_answer="42"),
            "task-2": Task(
                title="Summarize a note",
                prompt="Summarize: The launch was planned for Thursday. It is now Friday.",
                mock_answer="The launch moved from Thursday to Friday.",
            ),
            "task-3": Task(
                title="Plan a morning",
                prompt="Plan a morning with breakfast before a museum visit.",
                mock_answer="09:00 — breakfast.\n10:00 — visit the museum.",
            ),
        },
    )
    stuff = context.add_source_array(
        "stuff",
        Array.from_items(
            [
                (
                    (s, m),
                    await context.store(
                        Stuff(
                            model=await context.store(model_defs[m]),
                            label=label,
                            kind=kind,
                            content=text,
                        )
                    ),
                )
                for s, m, label, kind, text in (
                    (0, "anthropic-model", "A_tools", "tools", "calculator; notes search"),
                    (1, "openai-model", "O_tools", "tools", "calculator; calendar"),
                    (
                        2,
                        "anthropic-model",
                        "A_examples",
                        "examples",
                        "2 × 3 → 6; keep plans ordered",
                    ),
                )
            ],
            ("[s: int, m: str]", BoundID[Stuff]),
        ),
    )
    pairs = context.add_source_array(
        "model_harness_combinations",
        Array.from_items(
            [
                (
                    (m, h),
                    {"model": await context.store(model), "harness": await context.store(harness)},
                )
                for m, model in model_defs.items()
                for h, harness in harness_defs.items()
                if model.provider != harness.provider
            ],
            ("[m: str, h: str]", Pair),
        ),
    )
    all_results = context.run_child_workflow(
        "full_grid",
        full_grid,
        {"models": models, "harnesses": harnesses, "tasks": tasks, "stuff": stuff},
    )
    selected_results = context.run_child_workflow(
        "cross_provider", cross_provider, {"pairs": pairs, "tasks": tasks, "stuff": stuff}
    )
    return context.stack(
        {"full_grid": all_results, "cross_provider": selected_results}, dim="selection"
    )
