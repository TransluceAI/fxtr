"""Compare OpenAI models over ordered question sequences in this project."""

import argparse
import os
import uuid

import anyio

from behavior_rollouts.openai_rollout import (
    DEFAULT_QUESTION_SEQUENCES,
    models_array,
    openai_experiment,
    question_sequences_array,
    report_results,
)
from fxtr.entity_defns.array import Array
from fxtr.project.client import JobNotFinishedError
from fxtr.project.running import open_local_client, run_job


async def run(args: argparse.Namespace, sequences: dict[str, list[str]]) -> int:
    inputs = {
        "questions": question_sequences_array(sequences),
        "model_name": models_array(args.model),
        "max_tokens": Array.scalar(args.max_tokens, int),
    }
    total = len(args.model) * sum(len(sequence) for sequence in sequences.values())
    print(f"Asking {total} questions across {len(sequences)} sequences × {len(args.model)} models")
    async with open_local_client(
        __file__, verify_environment=not args.no_verify, max_concurrency=args.concurrency
    ) as client:
        result = await run_job(
            client,
            openai_experiment,
            inputs,
            root=f"openai-{uuid.uuid4().hex}",
            allow_staged=args.allow_staged,
        )
        return await report_results(client, result)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        action="append",
        help="OpenAI model name for the Responses API; repeat to compare models",
    )
    parser.add_argument(
        "--question",
        "--prompt",
        action="append",
        help="Override the dataset with one custom sequence; repeat for its sequential turns",
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
    try:
        models_array(args.model)
    except ValueError as error:
        parser.error(str(error))
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")
    if args.question is not None and any(not question.strip() for question in args.question):
        parser.error("questions must not be empty")
    question_sequences = (
        {"custom": args.question} if args.question is not None else DEFAULT_QUESTION_SEQUENCES
    )
    if not os.environ.get("OPENAI_API_KEY"):
        parser.error("set OPENAI_API_KEY in your environment before running this example")
    try:
        status = anyio.run(run, args, question_sequences)
    except JobNotFinishedError:
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    cli()
