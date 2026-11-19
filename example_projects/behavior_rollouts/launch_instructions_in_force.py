"""Hold conversations with models, and judge whether they kept to the instructions in force."""

import argparse
import json
import os
import uuid

import anyio

from behavior_rollouts.instructions_in_force import (
    OUTPUT_SCHEMA,
    RUBRIC,
    experiment,
    report_results,
    sequences_array,
)
from behavior_rollouts.instructions_in_force_sequences import INSTRUCTION_SEQUENCES
from behavior_rollouts.models import parse_model_spec, split_provider, store_model_specs
from behavior_rollouts.transcript_judging import JUDGE_INSTRUCTIONS
from behaviors.types import TextEntity
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.project.client import JobNotFinishedError
from fxtr.project.running import open_local_client, run_job


async def run(args: argparse.Namespace, sequences: dict[str, list[str]]) -> int:
    turns = len(args.model) * sum(len(sequence) for sequence in sequences.values())
    judgments = len(args.model) * len(sequences) * len(args.judge) * args.judge_samples
    print(
        f"Sending {turns} user turns across {len(sequences)} sequences × {len(args.model)} models,"
        f" then making {judgments} judgments with {len(args.judge)} judges"
    )
    async with open_local_client(
        __file__, verify_environment=not args.no_verify, max_concurrency=args.concurrency
    ) as client:
        model_name, sampling = await store_model_specs(
            client, args.model, dim="model", max_tokens=args.max_tokens
        )
        judge_model_name, judge_sampling = await store_model_specs(
            client, args.judge, dim="judge", max_tokens=args.judge_max_tokens
        )
        inputs = {
            "sequences": sequences_array(sequences),
            "model_name": model_name,
            "sampling": sampling,
            "judge_model_name": judge_model_name,
            "judge_sampling": judge_sampling,
            "judge_instructions": Array.scalar(
                await client.store(JUDGE_INSTRUCTIONS), BoundID[TextEntity]
            ),
            "rubric": Array.scalar(await client.store(RUBRIC), BoundID[TextEntity]),
            "output_schema": Array.scalar(
                await client.store(TextEntity(text=json.dumps(OUTPUT_SCHEMA))), BoundID[TextEntity]
            ),
            "judge_samples": Array.scalar(args.judge_samples, int),
        }
        result = await run_job(
            client,
            experiment,
            inputs,
            root=f"instructions-in-force-{uuid.uuid4().hex}",
            allow_staged=args.allow_staged,
        )
        return await report_results(client, result)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        action="append",
        help="A model, as [openai/|anthropic/]MODEL[:EFFORT]; repeat to compare models",
    )
    parser.add_argument(
        "--judge",
        required=True,
        action="append",
        help="A judge, as [openai/|anthropic/]MODEL[:EFFORT]; repeat to compare judges",
    )
    parser.add_argument(
        "--judge-samples",
        type=int,
        default=3,
        help="How many times each judge judges each conversation",
    )
    parser.add_argument(
        "--judge-max-tokens",
        type=int,
        default=16384,
        help="Maximum output tokens per judge answer, including reasoning tokens",
    )
    parser.add_argument(
        "--sequence",
        action="append",
        help="Hold only this named sequence; repeat for several",
    )
    parser.add_argument(
        "--turn",
        "--question",
        action="append",
        help="Replace the dataset with one custom sequence; repeat for its user turns, in order",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=8192,
        help="Maximum output tokens per model turn, including reasoning tokens",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--allow-staged", action="store_true", help="Pin staged changes")
    parser.add_argument("--no-verify", action="store_true", help="Skip the venv check")
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.judge_samples < 1:
        parser.error("--judge-samples must be positive")
    if args.max_tokens <= 0 or args.judge_max_tokens <= 0:
        parser.error("--max-tokens and --judge-max-tokens must be positive")
    for option in ("model", "judge"):
        values = getattr(args, option)
        if len(set(values)) != len(values):
            parser.error(f"each --{option} must be different")
    try:
        providers = {
            split_provider(parse_model_spec(spec)[0])[0] for spec in args.model + args.judge
        }
    except ValueError as error:
        parser.error(str(error))
    if args.turn is not None and args.sequence is not None:
        parser.error("use --turn or --sequence, not both")
    if args.turn is not None:
        if any(not turn.strip() for turn in args.turn):
            parser.error("turns must not be empty")
        sequences = {"custom": args.turn}
    elif args.sequence is not None:
        unknown = sorted(set(args.sequence) - set(INSTRUCTION_SEQUENCES))
        if unknown:
            parser.error(f"unknown sequences {unknown}; known: {sorted(INSTRUCTION_SEQUENCES)}")
        sequences = {name: INSTRUCTION_SEQUENCES[name] for name in args.sequence}
    else:
        sequences = INSTRUCTION_SEQUENCES
    for provider in sorted(providers):
        variable = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        if not os.environ.get(variable):
            parser.error(f"set {variable} in your environment before running this example")
    try:
        status = anyio.run(run, args, sequences)
    except JobNotFinishedError:
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    cli()
