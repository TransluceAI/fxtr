"""An example experiment with child workflows and data-dependent control flow.

A *bio* describes a simulated user. :func:`refine_bio` is a child workflow that improves one bio
iteratively: it simulates a conversation between the user the bio describes and a model
(:func:`simulate`), hands the transcript to a rewriter model step (:func:`rewrite`) that returns
a :class:`Review` (a proposed bio, or none, and a message), and, if there is a proposal, repeats
with the proposed bio; when the rewriter has no proposal the workflow returns the bio it last used.
How many rounds this takes depends on the values the steps produce, so the workflow *observes* each
proposal before deciding what to schedule next, feeding an observed proposal back in as a new
source array. The root workflow :func:`refine_bios` maps the child over an array of bios,
broadcasting one model config, one rewriter config, and one set of instructions to every child.

The model config and message types come from :mod:`basic_project.rollout_experiment`; as there,
nothing calls
a model, the steps assemble fake transcripts and proposals (the rewriter adds detail to a bio until
it reaches the level the instructions ask for), and the bodies declare their own array types (see
:mod:`fxtr.experiment.annotations`). The steps' arguments are entities, so their annotations say
everything; a workflow's are handles, whose dims no Python type implies, so those are written out.
Because :func:`rewrite` declares its proposal as a ``BoundID[Bio]``, the workflow observes it as
one and can store it back as a source array without naming the type again.

``launch_bio_refinement.py`` at the project root runs the experiment as a Postgres job and prints
its status and final bios. The stored job records the data-dependent number of rounds per child.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, TypedDict

from basic_project.rollout_experiment import Message, ModelConfig, entity_array
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

# ---- entity types ----

BIO = "org.example.refinement.Bio"
INSTRUCTIONS = "org.example.refinement.RewriteInstructions"
SIM_TRANSCRIPT = "org.example.refinement.SimulatedTranscript"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Bio(DataclassEntity, fxtr_type=BIO):
    """A simulated user: who they are and what they want, at some level of detail."""

    name: str
    persona: str
    detail: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class RewriteInstructions(DataclassEntity, fxtr_type=INSTRUCTIONS):
    """What the rewriter is asked to do to a bio."""

    style: str
    target_detail: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class SimulatedTranscript(DataclassEntity, fxtr_type=SIM_TRANSCRIPT):
    """A conversation between the simulated user of ``bio`` and ``model``."""

    model: BoundID[ModelConfig]
    bio: BoundID[Bio]
    messages: tuple[Message, ...]


class Review(TypedDict):
    """The rewriter's verdict: a proposed bio, or ``None`` when the bio is good enough, and why."""

    proposal: BoundID[Bio] | None
    message: str


# ---- steps ----


# Named explicitly, since this module also runs as a script (as __main__, its functions would
# otherwise have no stable qualified name; see fxtr.experiment.registry).
@step(name="simulate")
async def simulate(
    context: StepContext, model: ModelConfig, bio: BoundID[Bio]
) -> SimulatedTranscript:
    """Simulate (fakely) the user described by ``bio`` talking to ``model``."""
    persona = (await context.load(bio)).persona
    return SimulatedTranscript(
        model=await context.store(model),
        bio=bio,
        messages=(
            Message(role="user", content=f"Hi, {persona}."),
            Message(role="assistant", content=f"Hello! ({model.name})"),
        ),
    )


@step(name="rewrite")
async def rewrite(
    context: StepContext,
    transcript: SimulatedTranscript,
    instructions: RewriteInstructions,
    rewriter: ModelConfig,
) -> Review:
    """Ask the rewriter model (fakely) whether the bio behind ``transcript`` should change.

    The proposal is stored here explicitly and returned as a bound id inside the struct, since
    only a step whose whole output is a scalar reference can return an entity directly.
    """
    bio = await context.load(transcript.bio)
    if bio.detail >= instructions.target_detail:
        return {"proposal": None, "message": f"{rewriter.name}: detailed enough as is"}
    proposal = Bio(
        name=bio.name,
        persona=f"{bio.persona}, {instructions.style} detail {bio.detail + 1}",
        detail=bio.detail + 1,
    )
    return {
        "proposal": await context.store(proposal),
        "message": f"{rewriter.name}: added {instructions.style} detail",
    }


# ---- workflows ----


@workflow(name="refine_bio")
async def refine_bio(
    context: WorkflowContext,
    bio: ScalarHandle[BoundID[Bio]],
    model: ScalarHandle[BoundID[ModelConfig]],
    rewriter: ScalarHandle[BoundID[ModelConfig]],
    instructions: ScalarHandle[BoundID[RewriteInstructions]],
) -> ScalarHandle[BoundID[Bio]]:
    """Refine one bio until the rewriter has no further proposal, returning the final bio.

    Each round schedules a simulation and a review, then *observes* the review's proposal: the
    workflow needs the value to know whether to continue, so it waits for it here. The proposal
    arrives as a ``BoundID[Bio]`` (the review's declared type), and is registered as a new source
    array of that type for the next round. Node names carry the round number, since every
    scheduled node needs a distinct name within the workflow.
    """
    current = bio
    for round in range(1_000):  # bounded only to make the loop obviously finite
        transcript = context.run_step(
            f"simulate_{round}", simulate, {"model": model, "bio": current}
        )
        review = context.run_step(
            f"rewrite_{round}",
            rewrite,
            {"transcript": transcript, "instructions": instructions, "rewriter": rewriter},
        )
        proposal = await review.field("proposal").collect().item()
        if proposal is None:
            return current
        proposed = Array.scalar(proposal, BoundID[Bio])
        current = context.add_source_array(f"bio_{round + 1}", proposed)
    raise RuntimeError("the rewriter never ran out of proposals")


@workflow(name="refine_bios")
async def refine_bios(
    context: WorkflowContext,
) -> Annotated[ArrayHandle[BoundID[Bio]], "[bio: str]"]:
    """Refine every bio in an array with one model, rewriter, and set of instructions."""
    bios = await entity_array(
        context,
        "bios",
        "bio",
        {
            "alice": Bio(name="Alice", persona="a curious student", detail=0),
            "bob": Bio(name="Bob", persona="a busy engineer", detail=1),
        },
    )
    model = context.add_source_array(
        "model",
        Array.scalar(
            await context.store(ModelConfig(name="chat-1", temperature=0.8)), BoundID[ModelConfig]
        ),
    )
    rewriter = context.add_source_array(
        "rewriter",
        Array.scalar(
            await context.store(ModelConfig(name="editor-1", temperature=0.0)),
            BoundID[ModelConfig],
        ),
    )
    instructions = context.add_source_array(
        "instructions",
        Array.scalar(
            await context.store(RewriteInstructions(style="vivid", target_detail=2)),
            BoundID[RewriteInstructions],
        ),
    )
    # One child workflow per bio; the other inputs have no `bio` dim and are broadcast.
    return context.run_child_workflow(
        "refine",
        refine_bio,
        {"bio": bios, "model": model, "rewriter": rewriter, "instructions": instructions},
        map_over=["bio"],
    ).expect_type(("[bio: str]", BoundID[Bio]))
