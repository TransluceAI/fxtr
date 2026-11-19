"""Run the belief-reinforcement experiment: does supporting a distressed user strengthen their
interpretation beyond the evidence?"""

import argparse
import json
import os
import uuid

import anyio

from behavior_rollouts.belief_reinforcement import (
    OUTPUT_SCHEMA,
    RUBRIC,
    SYSTEM_PROMPT,
    experiment,
    report_results,
)
from behavior_rollouts.belief_reinforcement_cases import (
    SITUATIONS,
    BeliefReinforcementCase,
    matched_cases,
)
from behavior_rollouts.models import parse_model_spec, split_provider, store_model_specs
from behavior_rollouts.transcript_judging import JUDGE_INSTRUCTIONS
from behaviors.types import TextEntity
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.project.client import JobNotFinishedError
from fxtr.project.running import open_local_client, run_job


async def run(args: argparse.Namespace) -> int:
    cases = matched_cases(
        args.situation or list(SITUATIONS), args.evidence or ["weak", "mixed", "strong"]
    )
    total = len(args.model) * len(cases)
    judgments = total * len(args.judge) * args.judge_samples
    print(f"Running {total} conversations and {judgments} judgments")
    root = args.root or f"belief-reinforcement-{uuid.uuid4().hex}"
    print(f"Cache root {root}")
    async with open_local_client(
        __file__,
        verify_environment=not args.no_verify,
        max_concurrency=args.concurrency,
    ) as client:
        model_name, sampling = await store_model_specs(
            client, args.model, dim="model", max_tokens=args.max_tokens
        )
        judge_model_name, judge_sampling = await store_model_specs(
            client, args.judge, dim="judge", max_tokens=args.judge_max_tokens
        )
        inputs = {
            "cases": Array.from_items(
                [((name,), case) for name, case in cases.items()],
                type=("[case: str]", BeliefReinforcementCase),
            ),
            "model_name": model_name,
            "sampling": sampling,
            "judge_model_name": judge_model_name,
            "judge_sampling": judge_sampling,
            "system_prompt": Array.scalar(SYSTEM_PROMPT, str),
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
            root=root,
            allow_staged=args.allow_staged,
            overwrite_cache_conflicts=args.overwrite_cache_conflicts,
        )
        return await report_results(client, result)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("model", "judge"):
        parser.add_argument(
            f"--{option}",
            action="append",
            required=True,
            help="Repeat [openai/|anthropic/]MODEL[:EFFORT] to compare models",
        )
    parser.add_argument("--situation", action="append", choices=list(SITUATIONS))
    parser.add_argument("--evidence", action="append", choices=["weak", "mixed", "strong"])
    parser.add_argument("--judge-samples", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--judge-max-tokens", type=int, default=16384)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument(
        "--root", help="Reuse this cache root to expand a pilot with identical settings"
    )
    parser.add_argument("--allow-staged", action="store_true")
    parser.add_argument(
        "--overwrite-cache-conflicts",
        action="store_true",
        help="Recompute entries whose inputs changed, such as the summary of an expanded pilot",
    )
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()
    if min(args.concurrency, args.judge_samples, args.max_tokens, args.judge_max_tokens) < 1:
        parser.error("concurrency, samples and token limits must be positive")
    for name in ("model", "judge", "situation", "evidence"):
        values = getattr(args, name)
        if values is not None and len(values) != len(set(values)):
            parser.error(f"each --{name} must be different")
    try:
        providers = {
            split_provider(parse_model_spec(spec)[0])[0] for spec in args.model + args.judge
        }
    except ValueError as error:
        parser.error(str(error))
    for provider in sorted(providers):
        variable = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
        if not os.environ.get(variable):
            parser.error(f"Set {variable} before launching")
    try:
        status = anyio.run(run, args)
    except JobNotFinishedError:
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    cli()
