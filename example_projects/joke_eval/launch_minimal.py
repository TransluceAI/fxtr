import json

import anyio

from behaviors.judging.defaults import DEFAULT_SYSTEM_INSTRUCTIONS_ENTITY
from behaviors.judging.schemas import continuous_schema
from behaviors.types import SamplingParams, TextEntity
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.project.running import open_local_client, run_job
from joke_eval.experiment import SamplingParamsEntity, evaluate


async def main() -> None:
    async with open_local_client(__file__) as client:
        # Store the entities the workflow takes, getting back references to them.
        sampling = await client.store(SamplingParamsEntity(params=SamplingParams(max_tokens=1000)))
        instructions = await client.store(DEFAULT_SYSTEM_INSTRUCTIONS_ENTITY)
        rubric = await client.store(TextEntity(text="Rate how funny the joke is, from 1 to 10."))
        schema = continuous_schema(score_description="How funny it is.", minimum=1, maximum=10)
        output_schema = await client.store(TextEntity(text=json.dumps(schema)))
        # Build the workflow's inputs, as arrays with named dimensions, and run it as a job.
        inputs = {
            "model_name": Array.from_items(
                [(("haiku-4.5",), "anthropic/claude-haiku-4-5-20251001")], ("[model: str]", str)
            ),
            "sampling": Array.scalar(sampling, BoundID[SamplingParamsEntity]),
            "prompt_template": Array.scalar('Write one joke about "{topic}". Just the joke.', str),
            "topics": Array.from_items([(("coffee",), "coffee")], ("[topic: str]", str)),
            "judge_model_name": Array.scalar("anthropic/claude-opus-5-5", str),
            "judge_sampling": Array.scalar(sampling, BoundID[SamplingParamsEntity]),
            "judge_instructions": Array.scalar(instructions, BoundID[TextEntity]),
            "rubric": Array.scalar(rubric, BoundID[TextEntity]),
            "output_schema": Array.scalar(output_schema, BoundID[TextEntity]),
            "samples": Array.scalar(1, int),
        }
        await run_job(client, evaluate, inputs, root="jokes-minimal")


if __name__ == "__main__":
    anyio.run(main)
