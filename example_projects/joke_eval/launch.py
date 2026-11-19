"""Launch the joke evaluation: `uv run python launch.py pilot` or `uv run python launch.py full`."""

import json
import sys
from typing import Any

import anyio

from behaviors.judging.defaults import DEFAULT_SYSTEM_INSTRUCTIONS_ENTITY
from behaviors.judging.schemas import continuous_schema
from behaviors.types import SamplingParams, TextEntity
from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array
from fxtr.project.client import JobNotFinishedError, ProjectClient
from fxtr.project.running import open_local_client, run_job
from joke_eval.experiment import JokeReport, SamplingParamsEntity, evaluate

# The name of this run. Every cached result's address starts with it, so launching again under the
# same root reuses earlier results, and a new root starts fresh.
ROOT = "jokes-v1"

# How many topics (from the start of TOPICS) and samples per model and topic each size uses.
SIZES = {"pilot": (3, 1), "full": (10, 3)}

# Each model's short name (its key in the `model` dimension), and its provider and model ID.
MODELS = {
    "haiku-4.5": "anthropic/claude-haiku-4-5-20251001",
    "sonnet-5.5": "anthropic/claude-sonnet-5-5",
}
JUDGE = "anthropic/claude-opus-5-5"

# The sampling settings of every model call, the jokes' and the judge's.
SAMPLING = SamplingParams(max_tokens=1000)

TOPICS = [
    "coffee",
    "dentist",
    "printer",
    "penguin",
    "Monday",
    "airport",
    "houseplant",
    "spreadsheet",
    "cat",
    "elevator",
]

JOKE_PROMPT = (
    'Write one original joke about "{topic}". Reply with the joke only, '
    "with no preamble or explanation."
)

RUBRIC = """\
Rate how funny the assistant's joke about the user's topic is, from 1 (not funny at all) to 10 (a
joke a professional comedian would be proud of). A typical competent joke is around 5. Score a
well-known joke, or a stock format with the topic swapped in, low. If the assistant gave no joke,
score 1."""

# The shape of the judge's answer: a `discussion` of its reasons first, then a `score`.
OUTPUT_SCHEMA = continuous_schema(
    score_description="How funny the joke is, from 1 to 10.", minimum=1, maximum=10
)


async def build_inputs(client: ProjectClient, size: str) -> dict[str, Array[Any]]:
    """The workflow's inputs, as arrays keyed by the names of its arguments."""
    topic_count, samples = SIZES[size]

    # `client.store` stores an entity and returns a reference to it (a BoundID). The jokes and the
    # judge share one sampling entity.
    sampling = await client.store(SamplingParamsEntity(params=SAMPLING))

    # `Array.from_items` builds an array from (key, value) pairs, given its dimensions and value
    # type; `Array.scalar` builds one with no dimensions.
    return {
        "model_name": Array.from_items(
            [((name,), model_name) for name, model_name in MODELS.items()], ("[model: str]", str)
        ),
        "sampling": Array.scalar(sampling, BoundID[SamplingParamsEntity]),
        "prompt_template": Array.scalar(JOKE_PROMPT, str),
        "topics": Array.from_items(
            [((topic,), topic) for topic in TOPICS[:topic_count]], ("[topic: str]", str)
        ),
        "judge_model_name": Array.scalar(JUDGE, str),
        "judge_sampling": Array.scalar(sampling, BoundID[SamplingParamsEntity]),
        "judge_instructions": Array.scalar(
            await client.store(DEFAULT_SYSTEM_INSTRUCTIONS_ENTITY), BoundID[TextEntity]
        ),
        "rubric": Array.scalar(await client.store(TextEntity(text=RUBRIC)), BoundID[TextEntity]),
        "output_schema": Array.scalar(
            await client.store(TextEntity(text=json.dumps(OUTPUT_SCHEMA))), BoundID[TextEntity]
        ),
        "samples": Array.scalar(samples, int),
    }


async def print_summary(storage: FxtrEntityStorage, result: Array[Any]) -> None:
    """Print each model's mean score from the report the workflow returned."""
    report = await storage.load(result.item(), as_type=JokeReport)
    summary = await storage.load(report.summary)
    for (model,), row in summary.items():
        mean = "-" if row["mean_score"] is None else f"{row['mean_score']:.2f}"
        print(f"{model}: mean score {mean} ({row['graded']} of {row['jokes']} jokes graded)")


async def main(size: str) -> None:
    # Connect to the project's database. `max_concurrency` caps how many steps run at once.
    async with open_local_client(__file__, max_concurrency=8) as client:
        inputs = await build_inputs(client, size)
        # Submit the workflow as a job, show it in the viewer, wait for it to finish, and then
        # print the summary.
        await run_job(client, evaluate, inputs, root=ROOT, on_result=print_summary)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in SIZES:
        sys.exit(f"usage: python launch.py {{{'|'.join(SIZES)}}}")
    try:
        anyio.run(main, sys.argv[1])
    except JobNotFinishedError:
        sys.exit(1)  # the job's status and how to resume it are already printed
